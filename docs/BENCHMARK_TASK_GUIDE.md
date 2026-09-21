# JetsonAgent Benchmark Task Guide

A complete reference for writing benchmark tasks, with examples covering read-only lookups, stateful logging, multi-turn conversations, and nutritional queries (Spoonacular).

---

## Table of Contents

1. [Task Anatomy](#task-anatomy)
2. [Task Categories](#task-categories)
3. [Read-Only Task Examples](#read-only-task-examples)
4. [Stateful Task Examples](#stateful-task-examples)
5. [Multi-Turn Task Examples](#multi-turn-task-examples)
6. [Spoonacular / Nutrition Task Examples](#spoonacular--nutrition-task-examples)
7. [Scoring and Weights](#scoring-and-weights)
8. [Verification Types](#verification-types)
9. [How to Add a New Task](#how-to-add-a-new-task)
10. [Validation Checklist](#validation-checklist)

---

## Task Anatomy

Every task is a JSON object with the following fields:

```json
{
  "task_id": "unique_identifier",
  "prompt": "The user's question or instruction",
  "category": "lookup | summary | correlation | analysis | logging | memory | clarification",
  "expected_tools": ["tool_name"],
  "acceptable_tools": ["tool_name"],
  "data_range": {"start": "ISO", "end": "ISO"},
  "reference_query": "SQL for ground-truth",
  "expected_conclusion_keys": ["key1", "key2"],
  "scoring_weights": {
    "tool_correctness": 0.25,
    "argument_correctness": 0.25,
    "numeric_groundedness": 0.1,
    "structured_conclusion": 0.4
  },
  "multi_turn": false,
  "notes": "What this task tests"
}
```

### Required fields

| Field | Type | Description |
|---|---|---|
| `task_id` | string | Unique identifier, e.g. `ohio_001` |
| `prompt` | string | The user message sent to the agent |

### Optional fields (single-turn)

| Field | Type | Description |
|---|---|---|
| `category` | string | Used for grouping and mode filtering |
| `expected_tools` | string[] | Tools the agent *should* call |
| `acceptable_tools` | string[] | Tools that are *allowed* |
| `data_range` | object | `{"start": "ISO", "end": "ISO"}` — expected query window |
| `reference_query` | string | SQL query whose results ground-truth the answer |
| `expected_conclusion_keys` | string[] | Keys expected in `log_conclusion.findings` JSON |
| `scoring_weights` | object | How to weight each scoring dimension |
| `verification` | object | State verification for stateful tasks |
| `multi_turn` | boolean | `true` if this is a multi-turn task (legacy) |
| `mode` | string | `"multi_turn"` for multi-turn tasks (preferred) |
| `notes` | string | Human-readable description |

---

## Task Categories

| Category | Purpose | Example |
|---|---|---|
| `lookup` | Retrieve a specific value | "What was glucose at 9 AM?" |
| `summary` | Aggregate data over a range | "Total insulin on July 9" |
| `correlation` | Cross-reference multiple data sources | "Meals + glucose trend" |
| `analysis` | Threshold or pattern detection | "Any hypoglycemic episodes?" |
| `logging` | Write to KB/DB and verify | "Log a 45g carb breakfast" |
| `memory` | Recall earlier turns | "What did I log earlier?" |
| `clarification` | Handle ambiguous queries | "What was my glucose?" → needs date |
| `nutrition` | Spoonacular food lookups | "How many carbs in an apple?" |

---

## Read-Only Task Examples

### Example 1: Nearest reading lookup

```json
{
  "task_id": "ohio_001",
  "prompt": "What was the patient's glucose level at 9 AM on July 4, 2027?",
  "category": "lookup",
  "expected_tools": ["get_nearest_cgm_reading"],
  "acceptable_tools": ["get_nearest_cgm_reading", "get_cgm_readings"],
  "data_range": {
    "start": "2027-07-04T08:30:00+00:00",
    "end": "2027-07-04T09:30:00+00:00"
  },
  "reference_query": "SELECT glucose_mmol_l FROM cgm_readings WHERE factory_timestamp_utc >= '2027-07-04T08:30:00+00:00' AND factory_timestamp_utc <= '2027-07-04T09:30:00+00:00' ORDER BY ABS(strftime('%s', factory_timestamp_utc) - strftime('%s', '2027-07-04T09:00:00+00:00')) LIMIT 1",
  "expected_conclusion_keys": ["glucose_mmol_l"],
  "scoring_weights": {
    "tool_correctness": 0.25,
    "argument_correctness": 0.25,
    "numeric_groundedness": 0.1,
    "structured_conclusion": 0.4
  },
  "multi_turn": false,
  "notes": "Tests nearest-reading lookup with explicit timestamp."
}
```

### Example 2: Multi-tool correlation

```json
{
  "task_id": "ohio_003",
  "prompt": "What happened to the patient's glucose after their meals on July 4, 2027?",
  "category": "correlation",
  "expected_tools": ["get_cgm_readings", "get_meals"],
  "acceptable_tools": ["get_cgm_readings", "get_meals", "get_nearest_cgm_reading"],
  "data_range": {
    "start": "2027-07-04T00:00:00+00:00",
    "end": "2027-07-05T00:00:00+00:00"
  },
  "reference_query": "SELECT COUNT(*) AS meal_count, SUM(carbs_g) AS total_carbs FROM meal WHERE timestamp_utc >= '2027-07-04T00:00:00+00:00' AND timestamp_utc <= '2027-07-05T00:00:00+00:00'",
  "expected_conclusion_keys": ["meal_count", "total_carbs", "glucose_trend"],
  "scoring_weights": {
    "tool_correctness": 0.25,
    "argument_correctness": 0.15,
    "numeric_groundedness": 0.1,
    "structured_conclusion": 0.3,
    "judge": 0.2
  },
  "multi_turn": false,
  "notes": "Tests multi-tool correlation: meals + CGM trend."
}
```

### Example 3: Threshold-based analysis

```json
{
  "task_id": "ohio_005",
  "prompt": "Were there any hypoglycemic episodes (below 3.9 mmol/L) between July 4 and July 7, 2027?",
  "category": "analysis",
  "expected_tools": ["get_cgm_readings"],
  "acceptable_tools": ["get_cgm_readings", "get_cgm_summary_from_db", "get_cgm_spikes_from_db"],
  "data_range": {
    "start": "2027-07-04T00:00:00+00:00",
    "end": "2027-07-07T23:59:59+00:00"
  },
  "reference_query": "SELECT COUNT(*) AS hypo_count, MIN(glucose_mmol_l) AS min_glucose FROM cgm_readings WHERE factory_timestamp_utc >= '2027-07-04T00:00:00+00:00' AND factory_timestamp_utc <= '2027-07-07T23:59:59+00:00' AND glucose_mmol_l < 3.9",
  "expected_conclusion_keys": ["hypo_count", "min_glucose"],
  "scoring_weights": {
    "tool_correctness": 0.25,
    "argument_correctness": 0.15,
    "numeric_groundedness": 0.1,
    "structured_conclusion": 0.5
  },
  "multi_turn": false,
  "notes": "Tests threshold-based analysis and correct use of < 3.9."
}
```

---

## Stateful Task Examples

### Example 4: Log a single meal

```json
{
  "task_id": "ohio_log_001",
  "prompt": "Log a breakfast of 45g carbs on July 6, 2027 at 8:00 AM.",
  "category": "logging",
  "expected_tools": ["log_meal"],
  "acceptable_tools": ["log_meal", "log_conclusion"],
  "scoring_weights": {
    "tool_correctness": 0.3,
    "argument_correctness": 0.2,
    "structured_conclusion": 0.2,
    "logged_state": 0.3
  },
  "verification": {
    "type": "kb_scroll",
    "collection": "meal_events",
    "since_iso": "2027-07-06T00:00:00+00:00",
    "expected_payloads": [
      {"carbs_g": 45.0, "meal_type": "Breakfast", "timestamp": "2027-07-06T08:00:00+00:00"}
    ]
  },
  "multi_turn": false,
  "notes": "Tests basic meal logging with explicit timestamp."
}
```

### Example 5: Log meal + insulin

```json
{
  "task_id": "ohio_log_002",
  "prompt": "Log a lunch of 60g carbs with 4 units of rapid insulin on July 7, 2027 at 12:30 PM.",
  "category": "logging",
  "expected_tools": ["log_meal", "log_insulin"],
  "acceptable_tools": ["log_meal", "log_insulin", "log_conclusion"],
  "scoring_weights": {
    "tool_correctness": 0.3,
    "argument_correctness": 0.2,
    "structured_conclusion": 0.1,
    "logged_state": 0.4
  },
  "verification": {
    "type": "kb_scroll",
    "collection": "meal_events",
    "since_iso": "2027-07-07T00:00:00+00:00",
    "expected_payloads": [
      {"carbs_g": 60.0, "meal_type": "Lunch", "timestamp": "2027-07-07T12:30:00+00:00"}
    ]
  },
  "multi_turn": false,
  "notes": "Tests meal + insulin logging in a single task."
}
```

### Example 6: Log a glucose reading

```json
{
  "task_id": "ohio_log_003",
  "prompt": "Log a glucose reading of 5.5 mmol/L on July 6, 2027 at 10:00 AM.",
  "category": "logging",
  "expected_tools": ["log_glucose"],
  "acceptable_tools": ["log_glucose", "log_conclusion"],
  "scoring_weights": {
    "tool_correctness": 0.3,
    "argument_correctness": 0.2,
    "structured_conclusion": 0.2,
    "logged_state": 0.3
  },
  "verification": {
    "type": "kb_scroll",
    "collection": "glucose_events",
    "since_iso": "2027-07-06T00:00:00+00:00",
    "expected_payloads": [
      {"glucose_mmol_l": 0.3056, "timestamp": "2027-07-06T10:00:00+00:00"}
    ]
  },
  "multi_turn": false,
  "notes": "Tests glucose logging. Note: production log_glucose divides input by 18."
}
```

---

## Multi-Turn Task Examples

### Example 7: Memory across turns (log then recall)

```json
{
  "task_id": "ohio_multi_memory_001",
  "description": "Agent must remember a logged meal across turns.",
  "category": "memory",
  "mode": "multi_turn",
  "max_turns": 2,
  "user_simulator": {
    "type": "scripted",
    "script": [
      {
        "turn": 1,
        "user_message": "Log a breakfast of 50g carbs on July 6, 2027 at 8:00 AM.",
        "expected_tools": ["log_meal"],
        "state_check": {
          "type": "kb_scroll",
          "collection": "meal_events",
          "since_iso": "2027-07-06T00:00:00+00:00",
          "expected_payloads": [
            {"carbs_g": 50.0, "meal_type": "Breakfast", "timestamp": "2027-07-06T08:00:00+00:00"}
          ]
        }
      },
      {
        "turn": 2,
        "user_message": "What did I have for breakfast on July 6?",
        "expected_tools": ["search_logged_events"],
        "state_check": {
          "type": "response_contains",
          "expected_substrings": ["50g", "Breakfast"]
        }
      }
    ]
  },
  "scoring_weights": {
    "turn_completion": 0.3,
    "tool_correctness": 0.2,
    "per_turn_state": 0.3,
    "logged_state": 0.2
  },
  "multi_turn": true,
  "notes": "Tests memory across turns: log then recall."
}
```

### Example 8: Clarification required

```json
{
  "task_id": "ohio_multi_clarify_001",
  "description": "Agent must ask for clarification when the date is ambiguous.",
  "category": "clarification",
  "mode": "multi_turn",
  "max_turns": 3,
  "user_simulator": {
    "type": "scripted",
    "script": [
      {
        "turn": 1,
        "user_message": "What was my glucose in the morning?",
        "expected_tools": [],
        "stop_condition": "response_contains",
        "stop_criteria": {
          "expected_substrings": ["which date", "what date", "when"]
        }
      },
      {
        "turn": 2,
        "user_message": "July 5, 2027.",
        "expected_tools": ["get_cgm_readings"],
        "data_range": {
          "start": "2027-07-05T06:00:00+00:00",
          "end": "2027-07-05T12:00:00+00:00"
        }
      }
    ]
  },
  "scoring_weights": {
    "turn_completion": 0.3,
    "tool_correctness": 0.3,
    "per_turn_state": 0.2,
    "judge": 0.2
  },
  "multi_turn": true,
  "notes": "Tests clarification handling."
}
```

### Example 9: Deferred enrichment with CGM injection

```json
{
  "task_id": "ohio_multi_enrich_001",
  "description": "Log a meal, inject CGM data, trigger enrichment, verify notes.",
  "category": "deferred_workflow",
  "mode": "multi_turn",
  "max_turns": 3,
  "user_simulator": {
    "type": "scripted",
    "script": [
      {
        "turn": 1,
        "user_message": "Log a meal of 50g carbs at 8:00 AM on July 6, 2027.",
        "expected_tools": ["log_meal"]
      },
      {
        "turn": 2,
        "user_message": "Run deferred log enrichment now.",
        "expected_tools": ["run_deferred_log_enrichment"],
        "inject_events": {
          "type": "cgm_db_insert",
          "rows": [
            {
              "patient_id": "540",
              "factory_timestamp_utc": "2027-07-06T08:15:00+00:00",
              "glucose_mmol_l": 8.5
            },
            {
              "patient_id": "540",
              "factory_timestamp_utc": "2027-07-06T08:45:00+00:00",
              "glucose_mmol_l": 10.2
            }
          ]
        }
      },
      {
        "turn": 3,
        "user_message": "Search for that meal I logged earlier. What notes does it have?",
        "expected_tools": ["search_logged_events"],
        "state_check": {
          "type": "response_contains",
          "expected_substrings": ["CGM context", "8.5", "10.2"]
        }
      }
    ]
  },
  "scoring_weights": {
    "turn_completion": 0.2,
    "tool_correctness": 0.2,
    "per_turn_state": 0.3,
    "logged_state": 0.3
  },
  "multi_turn": true,
  "notes": "Tests deferred enrichment workflow with CGM data injection."
}
```

---

## Spoonacular / Nutrition Task Examples

> **Note:** Spoonacular tools (`search_food_nutrition`, `get_recipe_nutrition`, `get_glycemic_index`, `search_recipes_by_nutrients`) are defined in `agent/spoonacular.py`. To include them in the benchmark, you must add them to `_build_benchmark_tools()` in `benchmark_runner.py` (see [How to Add Spoonacular Tasks](#enabling-spoonacular-tools-in-the-benchmark)).

### Example 10: Food nutrition lookup

```json
{
  "task_id": "nutrition_001",
  "prompt": "How many carbs are in a medium apple?",
  "category": "nutrition",
  "expected_tools": ["search_food_nutrition"],
  "acceptable_tools": ["search_food_nutrition", "get_ingredient_nutrition"],
  "scoring_weights": {
    "tool_correctness": 0.4,
    "argument_correctness": 0.2,
    "numeric_groundedness": 0.2,
    "structured_conclusion": 0.2
  },
  "multi_turn": false,
  "notes": "Tests basic food nutrition lookup."
}
```

### Example 11: Diabetes-friendly recipe search

```json
{
  "task_id": "nutrition_002",
  "prompt": "Find a low-carb dinner recipe under 30g carbs per serving.",
  "category": "nutrition",
  "expected_tools": ["search_recipes_by_nutrients"],
  "acceptable_tools": ["search_recipes_by_nutrients", "search_food_nutrition"],
  "scoring_weights": {
    "tool_correctness": 0.4,
    "argument_correctness": 0.3,
    "structured_conclusion": 0.3
  },
  "multi_turn": false,
  "notes": "Tests recipe search with nutritional constraints."
}
```

### Example 12: Glycemic index comparison

```json
{
  "task_id": "nutrition_003",
  "prompt": "Compare the glycemic index of white rice, brown rice, and quinoa.",
  "category": "nutrition",
  "expected_tools": ["get_glycemic_index"],
  "acceptable_tools": ["get_glycemic_index", "search_food_nutrition"],
  "scoring_weights": {
    "tool_correctness": 0.4,
    "argument_correctness": 0.2,
    "numeric_groundedness": 0.2,
    "structured_conclusion": 0.2
  },
  "multi_turn": false,
  "notes": "Tests GI/GL lookup for multiple ingredients."
}
```

### Example 13: Recipe nutrition breakdown

```json
{
  "task_id": "nutrition_004",
  "prompt": "What are the macros in a chicken Caesar salad?",
  "category": "nutrition",
  "expected_tools": ["get_recipe_nutrition"],
  "acceptable_tools": ["get_recipe_nutrition", "search_food_nutrition"],
  "scoring_weights": {
    "tool_correctness": 0.4,
    "argument_correctness": 0.2,
    "numeric_groundedness": 0.2,
    "structured_conclusion": 0.2
  },
  "multi_turn": false,
  "notes": "Tests recipe-specific nutrition lookup."
}
```

### Example 14: Multi-turn meal planning

```json
{
  "task_id": "nutrition_multi_001",
  "description": "Plan a low-GI meal and verify carb count.",
  "category": "nutrition",
  "mode": "multi_turn",
  "max_turns": 2,
  "user_simulator": {
    "type": "scripted",
    "script": [
      {
        "turn": 1,
        "user_message": "Suggest a low-GI breakfast idea.",
        "expected_tools": ["search_recipes_by_nutrients"],
        "stop_condition": "response_contains",
        "stop_criteria": {
          "expected_substrings": ["oatmeal", "eggs", "yogurt", "berries"]
        }
      },
      {
        "turn": 2,
        "user_message": "How many carbs does that have?",
        "expected_tools": ["get_recipe_nutrition", "search_food_nutrition"]
      }
    ]
  },
  "scoring_weights": {
    "turn_completion": 0.3,
    "tool_correctness": 0.3,
    "per_turn_state": 0.2,
    "judge": 0.2
  },
  "multi_turn": true,
  "notes": "Tests multi-turn meal planning with nutritional follow-up."
}
```

---

## Scoring and Weights

### Single-turn scoring dimensions

| Dimension | What it measures | Default weight |
|---|---|---|
| `tool_correctness` | Did the agent call expected/acceptable tools? | 0.25 |
| `argument_correctness` | Did tool args cover the task's data range? | 0.25 |
| `numeric_groundedness` | Are numeric claims supported by DB data? | 0.1 |
| `structured_conclusion` | Did `log_conclusion` produce valid, grounded JSON? | 0.4 |
| `judge` | LLM-as-a-judge for prose quality | 0.0–0.3 |
| `logged_state` | Does the isolated KB/SQLite contain expected state? | 0.3–0.4 |

### Multi-turn scoring dimensions

| Dimension | What it measures | Default weight |
|---|---|---|
| `turn_completion` | Were all scripted turns executed? | 0.3 |
| `tool_correctness` | Per-turn tool correctness averaged across turns | 0.2 |
| `per_turn_state` | Did each turn's `state_check` pass? | 0.3 |
| `logged_state` | Post-task state verification (same as single-turn) | 0.2 |
| `judge` | LLM-as-a-judge for final response quality | 0.0–0.2 |

### Weight rules

- Weights must sum to 1.0 (or close to it) for meaningful normalized scores.
- A dimension with weight 0.0 is skipped entirely.
- `logged_state` requires `kb_store` to be passed to the evaluator (handled automatically by `benchmark.py`).
- `judge` requires `--judge-model` on the CLI.

---

## Verification Types

### `kb_scroll`

Scroll a KB collection and match payloads by field values.

```json
{
  "type": "kb_scroll",
  "collection": "meal_events",
  "since_iso": "2027-07-06T00:00:00+00:00",
  "expected_payloads": [
    {"carbs_g": 45.0, "meal_type": "Breakfast", "timestamp": "2027-07-06T08:00:00+00:00"}
  ]
}
```

### `kb_search`

Run a semantic query and check results contain expected text.

```json
{
  "type": "kb_search",
  "query": "breakfast",
  "expected_texts": ["Breakfast", "50g"],
  "since_days": 30,
  "event_types": ["meal"],
  "top_k": 10
}
```

### `sql_query`

Run a query against the isolated SQLite DB.

```json
{
  "type": "sql_query",
  "db_path": ".kb_data/cgm.sqlite3",
  "sql": "SELECT COUNT(*) AS c FROM cgm_readings WHERE glucose_mmol_l < 3.9",
  "expected_results": [{"c": 3}]
}
```

### `response_contains`

Check the agent's response text for expected substrings.

```json
{
  "type": "response_contains",
  "expected_substrings": ["50g", "Breakfast"]
}
```

---

## How to Add a New Task

### Step 1: Decide the task type

| If you want to test... | Use mode |
|---|---|
| One-shot lookup, summary, or analysis | Single-turn (default) |
| Logging state and verifying it exists | Single-turn + `verification` |
| Memory, clarification, or follow-up | `multi_turn` |
| Food/recipe nutrition (Spoonacular) | Single-turn or `multi_turn` |

### Step 2: Write the task JSON

1. Choose a unique `task_id` (prefix with your initials or domain, e.g. `nutrition_001`).
2. Write a clear, unambiguous `prompt` or `description`.
3. List `expected_tools` and `acceptable_tools`.
4. If the task uses a date range, add `data_range` and `reference_query`.
5. Set `scoring_weights` that sum to ~1.0.
6. Add `verification` or `user_simulator.script` as needed.

### Step 3: Append to `benchmark_tasks.json`

Open `benchmark_tasks.json` and append your task object to the array. Keep the JSON valid — use a linter if unsure.

### Step 4: Test the task in isolation

```bash
# Run only your new task
python benchmark.py --task-ids your_task_id --keep-db

# Inspect the trace and scores
python -c "
import json
with open('benchmark_results/traces.jsonl') as f:
    for line in f:
        trace = json.loads(line)
        if trace['task_id'] == 'your_task_id':
            print(json.dumps(trace, indent=2))
"
```

### Step 5: Verify state isolation (for stateful tasks)

```bash
# Before running, note the production KB state
python -c "from agent.kb.store import kb_store; print(len(kb_store._backend.scroll('meal_events', None, 'timestamp')))"

# Run your stateful task
python benchmark.py --task-ids your_task_id

# After running, confirm production KB is unchanged
python -c "from agent.kb.store import kb_store; print(len(kb_store._backend.scroll('meal_events', None, 'timestamp')))"
```

---

## Enabling Spoonacular Tools in the Benchmark

Spoonacular tools are **not** included in the benchmark tool list by default because they require an API key and network access. To enable them:

1. Set `SPOONACULAR_API_KEY` in `config.py`.
2. Import the tools in `benchmark_runner.py`:

```python
from agent.spoonacular import (
    search_food_nutrition,
    get_ingredient_nutrition,
    search_recipes_by_nutrients,
    get_recipe_nutrition,
    get_glycemic_index,
)
```

3. Append them to the return list in `_build_benchmark_tools()`:

```python
return [
    # ... existing tools ...
    search_food_nutrition,
    get_ingredient_nutrition,
    search_recipes_by_nutrients,
    get_recipe_nutrition,
    get_glycemic_index,
]
```

> **Caution:** Spoonacular calls are network-dependent and rate-limited. Benchmark runs that include these tools will be slower and may fail if the API is unreachable. Consider running them as a separate suite: `--mode nutrition` (requires adding `nutrition` to `filter_tasks_by_mode`).

---

## Validation Checklist

Before committing a new task, verify:

- [ ] `task_id` is unique across the entire file
- [ ] JSON is valid (no trailing commas, proper escaping)
- [ ] `expected_tools` contains only tools that exist
- [ ] `scoring_weights` sum to approximately 1.0
- [ ] `reference_query` runs successfully against the OhioT1DM DB
- [ ] For stateful tasks: `verification` is present and `logged_state` has a weight
- [ ] For multi-turn tasks: `user_simulator.script` has at least one turn
- [ ] For multi-turn tasks: each turn has `turn` number (1-indexed)
- [ ] The task runs end-to-end with `--task-ids <your_id>`
- [ ] Production `.kb_data` is unchanged after the run

---

## Quick Reference: Task Templates

### Minimal read-only task

```json
{
  "task_id": "my_lookup_001",
  "prompt": "What was the patient's weight?",
  "expected_tools": ["get_patient_info"],
  "scoring_weights": {
    "tool_correctness": 0.5,
    "structured_conclusion": 0.5
  }
}
```

### Minimal stateful task

```json
{
  "task_id": "my_log_001",
  "prompt": "Log a snack of 15g carbs now.",
  "category": "logging",
  "expected_tools": ["log_meal"],
  "scoring_weights": {
    "tool_correctness": 0.5,
    "logged_state": 0.5
  },
  "verification": {
    "type": "kb_scroll",
    "collection": "meal_events",
    "expected_payloads": [{"carbs_g": 15.0, "meal_type": "Snack"}]
  }
}
```

### Minimal multi-turn task

```json
{
  "task_id": "my_multi_001",
  "mode": "multi_turn",
  "max_turns": 2,
  "user_simulator": {
    "script": [
      {"turn": 1, "user_message": "Log 30g carbs."},
      {"turn": 2, "user_message": "What did I log?"}
    ]
  },
  "scoring_weights": {
    "turn_completion": 0.5,
    "tool_correctness": 0.5
  }
}
```
