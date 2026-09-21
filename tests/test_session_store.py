from pathlib import Path

from agent.session_store import load_sessions, save_sessions


def test_load_sessions_returns_empty_for_missing_file(tmp_path: Path):
    path = tmp_path / "chat_sessions.json"
    assert load_sessions(str(path)) == {}


def test_save_and_load_round_trip(tmp_path: Path):
    path = tmp_path / "chat_sessions.json"
    sessions = {
        "session-1": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
    }

    save_sessions(sessions, str(path))
    loaded = load_sessions(str(path))

    assert loaded == sessions


def test_load_sessions_sanitizes_invalid_entries(tmp_path: Path):
    path = tmp_path / "chat_sessions.json"
    path.write_text(
        '{"ok": [{"role": "user", "content": "a"}, {"role": "weird", "content": "b"}], "bad": "x"}',
        encoding="utf-8",
    )

    loaded = load_sessions(str(path))

    assert loaded == {"ok": [{"role": "user", "content": "a"}], "bad": []}
