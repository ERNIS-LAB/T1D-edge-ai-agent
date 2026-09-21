import os

import server


def test_chat_help_text_mentions_features_and_routes():
    help_text = server._chat_help_text()

    assert "/help" in help_text
    assert "Latest glucose lookup" in help_text
    assert "POST /api/chat" in help_text
    assert "POST /api/report" in help_text


def test_read_report_content_truncates_when_requested(tmp_path):
    report_path = tmp_path / "glucose_report_test.txt"
    report_path.write_text("abcdef", encoding="utf-8")

    content, truncated = server._read_report_content(report_path, max_chars=4)

    assert content == "abcd"
    assert truncated is True


def test_sorted_report_paths_orders_newest_first(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "REPORTS_DIR", str(tmp_path))

    old_report = tmp_path / "glucose_report_old.txt"
    old_report.write_text("old", encoding="utf-8")
    new_report = tmp_path / "glucose_report_new.txt"
    new_report.write_text("new", encoding="utf-8")

    os.utime(old_report, (1000, 1000))
    os.utime(new_report, (2000, 2000))

    report_paths = server._sorted_report_paths()

    assert [path.name for path in report_paths[:2]] == [
        "glucose_report_new.txt",
        "glucose_report_old.txt",
    ]


def test_resolve_report_path_blocks_directory_traversal(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "REPORTS_DIR", str(tmp_path))

    report_path = tmp_path / "glucose_report_safe.txt"
    report_path.write_text("ok", encoding="utf-8")

    assert server._resolve_report_path("glucose_report_safe.txt") == report_path.resolve()
    assert server._resolve_report_path("../glucose_report_safe.txt") is None


def test_append_session_turn_can_reset_existing_history(monkeypatch):
    monkeypatch.setattr(server, "_SESSIONS", {"session-1": [{"role": "user", "content": "old"}]})
    persisted: list[bool] = []
    monkeypatch.setattr(server, "_persist_sessions_locked", lambda: persisted.append(True))

    server._append_session_turn(
        session_id="session-1",
        reset_session=True,
        user_message="/help",
        assistant_message="help text",
    )

    assert server._SESSIONS["session-1"] == [
        {"role": "user", "content": "/help"},
        {"role": "assistant", "content": "help text"},
    ]
    assert persisted


def test_parse_model_ids_extracts_ids_only():
    payload = {
        "data": [
            {"id": "qwen3.5-8k:latest"},
            {"id": "mistral:latest"},
            {"name": "missing-id"},
            "not-a-dict",
        ]
    }

    model_ids = server._parse_model_ids(payload)

    assert model_ids == ["qwen3.5-8k:latest", "mistral:latest"]


def test_llm_connectivity_status_reports_model_availability(monkeypatch):
    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def read(self):
            return b'{"data":[{"id":"qwen3.5-8k:latest"},{"id":"other-model"}]}'

    monkeypatch.setattr(server, "urlopen", lambda *args, **kwargs: _FakeResponse())
    monkeypatch.setattr(server, "MODEL_NAME", "qwen3.5-8k:latest")
    monkeypatch.setattr(server, "_SERVER_DEFAULT_MODEL", None)

    status = server._llm_connectivity_status(timeout_seconds=0.01)

    assert status["ok"] is True
    assert status["configured_model_available"] is True
    assert "qwen3.5-8k:latest" in status["available_models"]


def test_llm_connectivity_status_reports_error(monkeypatch):
    monkeypatch.setattr(server, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    status = server._llm_connectivity_status(timeout_seconds=0.01)

    assert status["ok"] is False
    assert "boom" in status["error"]
