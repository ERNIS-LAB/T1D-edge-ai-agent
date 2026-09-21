"""Tests for multi-turn benchmark support."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from benchmark.evaluator import (
    _evaluate_multi_turn_task,
    score_multi_turn_tools,
    score_per_turn_state,
    score_turn_completion,
)
from benchmark.runner import filter_tasks_by_mode, run_multi_turn_task
from benchmark.user_simulator import ScriptedSimulator, build_simulator

# ---------------------------------------------------------------------------
# Simulator tests
# ---------------------------------------------------------------------------


def test_scripted_simulator_returns_messages_in_order():
    script = [
        {"turn": 1, "user_message": "Hello"},
        {"turn": 2, "user_message": "Follow up"},
        {"turn": 3, "user_message": "Goodbye"},
    ]
    sim = ScriptedSimulator(script)

    assert sim.next_message([]) == "Hello"
    assert sim.next_message([]) == "Follow up"
    assert sim.next_message([]) == "Goodbye"
    assert sim.next_message([]) is None


def test_scripted_simulator_max_turns():
    sim = ScriptedSimulator([{"turn": 1, "user_message": "Hi"}])
    assert sim.max_turns == 1


def test_build_simulator_scripted():
    task = {
        "user_simulator": {
            "type": "scripted",
            "script": [{"turn": 1, "user_message": "Hi"}],
        }
    }
    sim = build_simulator(task)
    assert isinstance(sim, ScriptedSimulator)


def test_build_simulator_defaults_to_scripted():
    task = {"user_simulator": {"script": [{"turn": 1, "user_message": "Hi"}]}}
    sim = build_simulator(task)
    assert isinstance(sim, ScriptedSimulator)


# ---------------------------------------------------------------------------
# Multi-turn runner tests
# ---------------------------------------------------------------------------


def _make_fake_graph(responses: list[list[dict[str, Any]]]):
    """Create a fake graph that returns pre-canned messages per invocation."""
    call_count = [0]

    class FakeGraph:
        def invoke(self, state: dict[str, Any], config=None):
            idx = call_count[0]
            call_count[0] += 1

            # Build fake LangGraph-style messages from the response template
            from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

            all_messages = state.get("messages", [])
            new_messages = list(all_messages)

            for item in responses[idx]:
                if item["type"] == "ai":
                    new_messages.append(
                        AIMessage(
                            content=item.get("content", ""),
                            tool_calls=item.get("tool_calls", []),
                        )
                    )
                elif item["type"] == "tool":
                    new_messages.append(
                        ToolMessage(
                            content=item["content"],
                            tool_call_id=item["tool_call_id"],
                        )
                    )

            return {"messages": new_messages}

    return FakeGraph(), call_count


def test_run_multi_turn_task_2_turns():
    """A 2-turn task runs both turns and aggregates results."""
    task = {
        "task_id": "test_multi_001",
        "description": "Test multi-turn",
        "mode": "multi_turn",
        "max_turns": 2,
        "user_simulator": {
            "type": "scripted",
            "script": [
                {
                    "turn": 1,
                    "user_message": "Log a meal of 50g carbs.",
                },
                {
                    "turn": 2,
                    "user_message": "What did I log?",
                },
            ],
        },
    }

    responses = [
        [
            {
                "type": "ai",
                "content": "",
                "tool_calls": [
                    {"id": "c1", "name": "log_meal", "args": {"carbs_g": 50}}
                ],
            },
            {"type": "tool", "content": "Logged meal.", "tool_call_id": "c1"},
            {"type": "ai", "content": "Done."},
        ],
        [
            {
                "type": "ai",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c2",
                        "name": "search_logged_events",
                        "args": {"query": "meal"},
                    }
                ],
            },
            {"type": "tool", "content": "Found meal: 50g carbs.", "tool_call_id": "c2"},
            {"type": "ai", "content": "You logged 50g carbs."},
        ],
    ]

    graph, _ = _make_fake_graph(responses)
    trace = run_multi_turn_task(graph, task, db_path=":memory:")

    assert trace["task_id"] == "test_multi_001"
    assert trace["turn_count"] == 2
    assert len(trace["turns"]) == 2
    assert trace["turns"][0]["user_message"] == "Log a meal of 50g carbs."
    assert trace["turns"][1]["user_message"] == "What did I log?"
    assert trace["turns"][0]["final_response"] == "Done."
    assert trace["turns"][1]["final_response"] == "You logged 50g carbs."
    assert len(trace["tool_calls"]) == 2
    assert trace["tool_calls"][0]["name"] == "log_meal"
    assert trace["tool_calls"][1]["name"] == "search_logged_events"


def test_run_multi_turn_task_flat_messages_backward_compatible():
    """Multi-turn trace should include flat messages for backward compatibility."""
    task = {
        "task_id": "test_multi_002",
        "mode": "multi_turn",
        "max_turns": 1,
        "user_simulator": {
            "script": [{"turn": 1, "user_message": "Hi"}],
        },
    }

    responses = [
        [
            {"type": "ai", "content": "Hello!"},
        ]
    ]

    graph, _ = _make_fake_graph(responses)
    trace = run_multi_turn_task(graph, task, db_path=":memory:")

    assert "messages" in trace
    assert "turns" in trace
    assert len(trace["messages"]) == 1
    assert trace["messages"][0]["type"] == "ai"


def test_run_multi_turn_task_stop_condition_tool_called():
    """Task should stop early when stop_condition=tool_called is met."""
    task = {
        "task_id": "test_multi_stop",
        "mode": "multi_turn",
        "max_turns": 3,
        "user_simulator": {
            "script": [
                {
                    "turn": 1,
                    "user_message": "What was my glucose?",
                    "expected_tools": ["get_cgm_readings"],
                    "stop_condition": "tool_called",
                },
                {"turn": 2, "user_message": "This should not run."},
            ],
        },
    }

    responses = [
        [
            {
                "type": "ai",
                "content": "",
                "tool_calls": [{"id": "c1", "name": "get_cgm_readings", "args": {}}],
            },
            {"type": "tool", "content": "5.5 mmol/L", "tool_call_id": "c1"},
            {"type": "ai", "content": "Your glucose was 5.5 mmol/L."},
        ]
    ]

    graph, _ = _make_fake_graph(responses)
    trace = run_multi_turn_task(graph, task, db_path=":memory:")

    assert trace["turn_count"] == 1


def test_run_multi_turn_task_stop_condition_response_contains():
    """Task should stop early when stop_condition=response_contains is met."""
    task = {
        "task_id": "test_multi_stop2",
        "mode": "multi_turn",
        "max_turns": 3,
        "user_simulator": {
            "script": [
                {
                    "turn": 1,
                    "user_message": "What was my glucose?",
                    "stop_condition": "response_contains",
                    "stop_criteria": {"expected_substrings": ["5.5"]},
                },
                {"turn": 2, "user_message": "This should not run."},
            ],
        },
    }

    responses = [
        [
            {"type": "ai", "content": "Your glucose was 5.5 mmol/L."},
        ]
    ]

    graph, _ = _make_fake_graph(responses)
    trace = run_multi_turn_task(graph, task, db_path=":memory:")

    assert trace["turn_count"] == 1


def test_run_multi_turn_task_cgm_db_inject():
    """Pre-turn CGM DB injection should insert rows into SQLite."""
    import sqlite3
    import tempfile
    from pathlib import Path

    db_path = str(Path(tempfile.mkdtemp()) / "test.sqlite3")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE cgm_readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id TEXT NOT NULL,
            factory_timestamp_utc TEXT NOT NULL,
            device_timestamp_local TEXT,
            glucose_mmol_l REAL NOT NULL,
            trend INTEGER,
            source TEXT NOT NULL DEFAULT 'librelinkup',
            raw_json TEXT,
            inserted_at TEXT NOT NULL,
            UNIQUE(patient_id, factory_timestamp_utc, glucose_mmol_l)
        )
    """)
    conn.commit()
    conn.close()

    task = {
        "task_id": "test_inject",
        "mode": "multi_turn",
        "max_turns": 1,
        "user_simulator": {
            "script": [
                {
                    "turn": 1,
                    "user_message": "Check CGM.",
                    "inject_events": {
                        "type": "cgm_db_insert",
                        "rows": [
                            {
                                "patient_id": "540",
                                "factory_timestamp_utc": "2027-07-06T08:00:00+00:00",
                                "glucose_mmol_l": 7.2,
                            }
                        ],
                    },
                },
            ],
        },
    }

    responses = [[{"type": "ai", "content": "OK"}]]
    graph, _ = _make_fake_graph(responses)
    trace = run_multi_turn_task(graph, task, db_path=db_path)

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT glucose_mmol_l FROM cgm_readings").fetchone()
    conn.close()

    assert row is not None
    assert abs(row[0] - 7.2) < 0.01


# ---------------------------------------------------------------------------
# Multi-turn scorer tests
# ---------------------------------------------------------------------------


def test_score_turn_completion_all_turns():
    trace = {"turns": [{"turn_number": 1}, {"turn_number": 2}]}
    task = {"user_simulator": {"script": [{"turn": 1}, {"turn": 2}]}}
    score, detail = score_turn_completion(trace, task)
    assert score == 1.0
    assert "2 turn(s)" in detail


def test_score_turn_completion_partial():
    trace = {"turns": [{"turn_number": 1}]}
    task = {"user_simulator": {"script": [{"turn": 1}, {"turn": 2}, {"turn": 3}]}}
    score, detail = score_turn_completion(trace, task)
    assert score == pytest.approx(0.333, 0.01)
    assert "1/3" in detail


def test_score_turn_completion_no_script():
    trace = {"turns": []}
    task = {"user_simulator": {}}
    score, detail = score_turn_completion(trace, task)
    assert score == 1.0


def test_score_per_turn_state_kb_scroll():
    from agent.kb.store import InMemoryVectorBackend, KnowledgeBaseStore

    kb = KnowledgeBaseStore.__new__(KnowledgeBaseStore)
    kb._embedder = MagicMock()
    kb._embedder.embed.return_value = [0.1] * 384
    kb._backend = InMemoryVectorBackend()
    for collection in kb.COLLECTIONS.values():
        kb._backend.ensure_collection(collection)

    kb.add_meal_event(
        timestamp="2027-07-06T08:00:00+00:00",
        carbs_g=50.0,
        meal_type="Breakfast",
    )

    trace = {
        "turns": [
            {
                "turn_number": 1,
                "final_response": "Done.",
                "tool_calls": [{"name": "log_meal"}],
            }
        ]
    }
    task = {
        "user_simulator": {
            "script": [
                {
                    "turn": 1,
                    "state_check": {
                        "type": "kb_scroll",
                        "collection": "meal_events",
                        "since_iso": "2027-07-06T00:00:00+00:00",
                        "expected_payloads": [
                            {"carbs_g": 50.0, "meal_type": "Breakfast"}
                        ],
                    },
                }
            ]
        }
    }
    score, detail = score_per_turn_state(trace, task, kb)
    assert score == 1.0
    assert "1.0" in detail


def test_score_per_turn_state_response_contains():
    trace = {
        "turns": [
            {
                "turn_number": 1,
                "final_response": "You logged 50g of Breakfast carbs.",
            }
        ]
    }
    task = {
        "user_simulator": {
            "script": [
                {
                    "turn": 1,
                    "state_check": {
                        "type": "response_contains",
                        "expected_substrings": ["50g", "Breakfast"],
                    },
                }
            ]
        }
    }
    score, detail = score_per_turn_state(trace, task, None)
    assert score == 1.0
    assert "1.0" in detail


def test_score_multi_turn_tools_expected_only():
    trace = {
        "turns": [
            {"turn_number": 1, "tool_calls": [{"name": "log_meal"}]},
            {"turn_number": 2, "tool_calls": [{"name": "search_logged_events"}]},
        ]
    }
    task = {
        "user_simulator": {
            "script": [
                {"turn": 1, "expected_tools": ["log_meal"]},
                {"turn": 2, "expected_tools": ["search_logged_events"]},
            ]
        }
    }
    score, detail = score_multi_turn_tools(trace, task)
    assert score == 1.0


def test_score_multi_turn_tools_mixed():
    trace = {
        "turns": [
            {"turn_number": 1, "tool_calls": [{"name": "log_meal"}]},
            {"turn_number": 2, "tool_calls": [{"name": "get_cgm_readings"}]},
        ]
    }
    task = {
        "user_simulator": {
            "script": [
                {"turn": 1, "expected_tools": ["log_meal"]},
                {
                    "turn": 2,
                    "expected_tools": ["search_logged_events"],
                    "acceptable_tools": ["get_cgm_readings"],
                },
            ]
        }
    }
    score, detail = score_multi_turn_tools(trace, task)
    assert score == 0.75  # (1.0 + 0.5) / 2


# ---------------------------------------------------------------------------
# Mode filter tests
# ---------------------------------------------------------------------------


def test_filter_tasks_by_mode_multi_turn():
    tasks = [
        {"task_id": "a", "category": "lookup"},
        {"task_id": "b", "mode": "multi_turn"},
        {"task_id": "c", "multi_turn": True},
    ]
    result = filter_tasks_by_mode(tasks, "multi_turn")
    assert [t["task_id"] for t in result] == ["b", "c"]


def test_filter_tasks_by_mode_readonly_excludes_multi_turn():
    tasks = [
        {"task_id": "a", "category": "lookup"},
        {"task_id": "b", "mode": "multi_turn"},
        {"task_id": "c", "category": "logging"},
    ]
    result = filter_tasks_by_mode(tasks, "read-only")
    assert [t["task_id"] for t in result] == ["a"]


# ---------------------------------------------------------------------------
# End-to-end evaluator test
# ---------------------------------------------------------------------------


def test_evaluate_multi_turn_task_full():
    from agent.kb.store import InMemoryVectorBackend, KnowledgeBaseStore

    kb = KnowledgeBaseStore.__new__(KnowledgeBaseStore)
    kb._embedder = MagicMock()
    kb._embedder.embed.return_value = [0.1] * 384
    kb._backend = InMemoryVectorBackend()
    for collection in kb.COLLECTIONS.values():
        kb._backend.ensure_collection(collection)

    trace = {
        "task_id": "multi_test",
        "turns": [
            {
                "turn_number": 1,
                "user_message": "Log meal",
                "final_response": "Done.",
                "tool_calls": [{"name": "log_meal"}],
            },
            {
                "turn_number": 2,
                "user_message": "Recall",
                "final_response": "You had 50g Breakfast.",
                "tool_calls": [{"name": "search_logged_events"}],
            },
        ],
        "final_response": "You had 50g Breakfast.",
    }
    task = {
        "task_id": "multi_test",
        "mode": "multi_turn",
        "user_simulator": {
            "script": [
                {"turn": 1, "expected_tools": ["log_meal"]},
                {"turn": 2, "expected_tools": ["search_logged_events"]},
            ]
        },
        "scoring_weights": {
            "turn_completion": 0.3,
            "tool_correctness": 0.3,
            "per_turn_state": 0.4,
        },
    }

    result = _evaluate_multi_turn_task(trace, task, db_path=":memory:", kb_store=kb)
    assert result["mode"] == "multi_turn"
    assert "turn_completion" in result["scores"]
    assert "tool_correctness" in result["scores"]
    assert "per_turn_state" in result["scores"]
    assert result["normalized_score"] > 0.0
