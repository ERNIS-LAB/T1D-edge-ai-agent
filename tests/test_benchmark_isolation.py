"""Tests for benchmark isolation, stateful tools, and state verification scorers."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from benchmark.evaluator import evaluate_task, score_logged_state
from benchmark.isolation import BenchmarkIsolation
from benchmark.runner import build_benchmark_graph, filter_tasks_by_mode

# ---------------------------------------------------------------------------
# Isolation tests
# ---------------------------------------------------------------------------


def test_benchmark_isolation_creates_temp_db_and_kb(tmp_path, monkeypatch):
    """BenchmarkIsolation should create the temp DB and session file.

    The KB is deliberately in-memory (see BenchmarkIsolation.__init__), so it has
    no on-disk path to assert on; test_benchmark_isolation_kb_is_empty_on_start
    covers its state instead.
    """
    xml_path = "data/OhioT1DM/2020/test/540-ws-testing.xml"

    iso = BenchmarkIsolation(xml_path=xml_path)
    try:
        assert Path(iso.db_path).exists()
        assert Path(iso.sessions_path).exists()
        assert "cgm_readings" in iso.counts
    finally:
        iso.cleanup()

    assert not Path(iso.temp_dir).exists()


def test_benchmark_isolation_patches_globals(tmp_path, monkeypatch):
    """BenchmarkIsolation should patch cgm_store, kb_store, and CHAT_SESSIONS_PATH."""
    xml_path = "data/OhioT1DM/2020/test/540-ws-testing.xml"

    cgm_store_module = importlib.import_module("agent.data.cgm_store")
    tools_module = importlib.import_module("agent.tools")
    kb_store_module = importlib.import_module("agent.kb.store")
    session_store_module = importlib.import_module("agent.session_store")

    old_cgm = getattr(cgm_store_module, "cgm_store", None)
    old_kb = getattr(kb_store_module, "kb_store", None)
    old_sessions = getattr(session_store_module, "CHAT_SESSIONS_PATH", None)

    iso = BenchmarkIsolation(xml_path=xml_path)
    try:
        assert getattr(cgm_store_module, "cgm_store", None) is not old_cgm
        assert getattr(kb_store_module, "kb_store", None) is not old_kb
        assert (
            getattr(session_store_module, "CHAT_SESSIONS_PATH", None)
            == iso.sessions_path
        )
        assert getattr(tools_module, "cgm_store", None) is getattr(
            cgm_store_module, "cgm_store"
        )
        assert getattr(tools_module, "kb_store", None) is getattr(
            kb_store_module, "kb_store"
        )
    finally:
        iso.cleanup()

    # After cleanup, globals are restored
    assert getattr(cgm_store_module, "cgm_store", None) is old_cgm
    assert getattr(kb_store_module, "kb_store", None) is old_kb
    assert getattr(session_store_module, "CHAT_SESSIONS_PATH", None) == old_sessions


def test_benchmark_isolation_context_manager(tmp_path, monkeypatch):
    """BenchmarkIsolation should work as a context manager."""
    xml_path = "data/OhioT1DM/2020/test/540-ws-testing.xml"
    cgm_store_module = importlib.import_module("agent.data.cgm_store")
    old_cgm = getattr(cgm_store_module, "cgm_store", None)

    with BenchmarkIsolation(xml_path=xml_path) as iso:
        assert getattr(cgm_store_module, "cgm_store", None) is not old_cgm
        temp_dir = iso.temp_dir

    assert getattr(cgm_store_module, "cgm_store", None) is old_cgm
    assert not Path(temp_dir).exists()


def test_benchmark_isolation_kb_is_empty_on_start():
    """Fresh isolated KB should have no events before tools write to it."""
    xml_path = "data/OhioT1DM/2020/test/540-ws-testing.xml"

    with BenchmarkIsolation(xml_path=xml_path) as iso:
        meals = iso.kb_store._backend.scroll(
            "meal_events", since_cutoff_iso=None, since_field="timestamp"
        )
        insulin = iso.kb_store._backend.scroll(
            "insulin_events", since_cutoff_iso=None, since_field="timestamp"
        )
        glucose = iso.kb_store._backend.scroll(
            "glucose_events", since_cutoff_iso=None, since_field="timestamp"
        )
        assert meals == []
        assert insulin == []
        assert glucose == []


# ---------------------------------------------------------------------------
# Stateful tool wiring test
# ---------------------------------------------------------------------------


def test_log_meal_writes_to_isolated_kb():
    """log_meal (production tool) should write into the isolated KB, not production."""
    xml_path = "data/OhioT1DM/2020/test/540-ws-testing.xml"
    from agent.tools import log_meal, search_logged_events

    with BenchmarkIsolation(xml_path=xml_path) as iso:
        result = log_meal.invoke(
            {
                "carbs_g": 42,
                "meal_type": "Breakfast",
                "timestamp": "2027-07-06T08:00:00+00:00",
            }
        )
        assert "Logged meal" in result

        rows = iso.kb_store._backend.scroll(
            "meal_events", since_cutoff_iso=None, since_field="timestamp"
        )
        assert len(rows) == 1
        assert rows[0]["carbs_g"] == 42.0
        assert rows[0]["meal_type"] == "Breakfast"

        search_result = search_logged_events.invoke(
            {"query": "breakfast", "since_days": 30, "event_types": "meal"}
        )
        assert "breakfast" in search_result.lower() or "Breakfast" in search_result


# ---------------------------------------------------------------------------
# State verification scorer tests
# ---------------------------------------------------------------------------


def test_score_logged_state_kb_scroll_match():
    """score_logged_state should return 1.0 when expected payload exists."""
    from agent.kb.store import InMemoryVectorBackend, KnowledgeBaseStore

    kb = KnowledgeBaseStore.__new__(KnowledgeBaseStore)
    kb._embedder = MagicMock()
    kb._embedder.embed.return_value = [0.1] * 384
    kb._backend = InMemoryVectorBackend()
    for collection in kb.COLLECTIONS.values():
        kb._backend.ensure_collection(collection)

    kb.add_meal_event(
        timestamp="2027-07-06T08:00:00+00:00",
        carbs_g=45.0,
        meal_type="Breakfast",
    )

    task = {
        "verification": {
            "type": "kb_scroll",
            "collection": "meal_events",
            "since_iso": "2027-07-06T00:00:00+00:00",
            "expected_payloads": [
                {
                    "carbs_g": 45.0,
                    "meal_type": "Breakfast",
                    "timestamp": "2027-07-06T08:00:00+00:00",
                }
            ],
        }
    }
    trace = {"tool_calls": []}

    score, detail = score_logged_state(trace, task, kb)
    assert score == 1.0
    assert "matched 1/1" in detail


def test_score_logged_state_kb_scroll_partial_match():
    """score_logged_state should return partial score when only some payloads match."""
    from agent.kb.store import InMemoryVectorBackend, KnowledgeBaseStore

    kb = KnowledgeBaseStore.__new__(KnowledgeBaseStore)
    kb._embedder = MagicMock()
    kb._embedder.embed.return_value = [0.1] * 384
    kb._backend = InMemoryVectorBackend()
    for collection in kb.COLLECTIONS.values():
        kb._backend.ensure_collection(collection)

    kb.add_meal_event(
        timestamp="2027-07-06T08:00:00+00:00",
        carbs_g=45.0,
        meal_type="Breakfast",
    )

    task = {
        "verification": {
            "type": "kb_scroll",
            "collection": "meal_events",
            "since_iso": "2027-07-06T00:00:00+00:00",
            "expected_payloads": [
                {
                    "carbs_g": 45.0,
                    "meal_type": "Breakfast",
                    "timestamp": "2027-07-06T08:00:00+00:00",
                },
                {
                    "carbs_g": 60.0,
                    "meal_type": "Lunch",
                    "timestamp": "2027-07-06T12:00:00+00:00",
                },
            ],
        }
    }
    trace = {"tool_calls": []}

    score, detail = score_logged_state(trace, task, kb)
    assert score == 0.5
    assert "matched 1/2" in detail


def test_score_logged_state_no_verification():
    """score_logged_state should return 1.0 when there is no verification block."""
    from agent.kb.store import InMemoryVectorBackend, KnowledgeBaseStore

    kb = KnowledgeBaseStore.__new__(KnowledgeBaseStore)
    kb._embedder = MagicMock()
    kb._embedder.embed.return_value = [0.1] * 384
    kb._backend = InMemoryVectorBackend()

    task = {}
    trace = {"tool_calls": []}

    score, detail = score_logged_state(trace, task, kb)
    assert score == 1.0
    assert "skipping" in detail.lower()


# ---------------------------------------------------------------------------
# Mode filter tests
# ---------------------------------------------------------------------------


def test_filter_tasks_by_mode_readonly():
    tasks = [
        {"task_id": "a", "category": "lookup"},
        {"task_id": "b", "category": "logging"},
        {"task_id": "c", "category": "summary"},
    ]
    result = filter_tasks_by_mode(tasks, "read-only")
    assert [t["task_id"] for t in result] == ["a", "c"]


def test_filter_tasks_by_mode_stateful():
    tasks = [
        {"task_id": "a", "category": "lookup"},
        {"task_id": "b", "category": "logging"},
        {"task_id": "c", "category": "summary"},
    ]
    result = filter_tasks_by_mode(tasks, "stateful")
    assert [t["task_id"] for t in result] == ["b"]


def test_filter_tasks_by_mode_all():
    tasks = [
        {"task_id": "a", "category": "lookup"},
        {"task_id": "b", "category": "logging"},
    ]
    result = filter_tasks_by_mode(tasks, "all")
    assert [t["task_id"] for t in result] == ["a", "b"]


# ---------------------------------------------------------------------------
# End-to-end evaluator test with logged_state weight
# ---------------------------------------------------------------------------


def test_evaluate_task_with_logged_state():
    """evaluate_task should include logged_state score when weight is present."""
    from agent.kb.store import InMemoryVectorBackend, KnowledgeBaseStore

    kb = KnowledgeBaseStore.__new__(KnowledgeBaseStore)
    kb._embedder = MagicMock()
    kb._embedder.embed.return_value = [0.1] * 384
    kb._backend = InMemoryVectorBackend()
    for collection in kb.COLLECTIONS.values():
        kb._backend.ensure_collection(collection)

    kb.add_meal_event(
        timestamp="2027-07-06T08:00:00+00:00",
        carbs_g=45.0,
        meal_type="Breakfast",
    )

    task = {
        "task_id": "test_log_001",
        "category": "logging",
        "expected_tools": ["log_meal"],
        "acceptable_tools": ["log_meal"],
        "scoring_weights": {
            "tool_correctness": 0.25,
            "argument_correctness": 0.25,
            "structured_conclusion": 0.0,
            "logged_state": 0.5,
        },
        "verification": {
            "type": "kb_scroll",
            "collection": "meal_events",
            "since_iso": "2027-07-06T00:00:00+00:00",
            "expected_payloads": [
                {
                    "carbs_g": 45.0,
                    "meal_type": "Breakfast",
                    "timestamp": "2027-07-06T08:00:00+00:00",
                }
            ],
        },
    }
    trace = {
        "task_id": "test_log_001",
        "tool_calls": [
            {"name": "log_meal", "args": {"carbs_g": 45, "meal_type": "Breakfast"}}
        ],
        "final_response": "Done",
        "tool_results": {},
    }

    result = evaluate_task(trace, task, db_path=":memory:", kb_store=kb)
    assert "logged_state" in result["scores"]
    assert result["scores"]["logged_state"]["score"] == 1.0
    assert result["normalized_score"] > 0.0
