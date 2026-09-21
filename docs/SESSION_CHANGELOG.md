# JetsonAgent Session Changelog

This document summarizes what was implemented in this session, why those changes were made, how they work, and how to demo the product end-to-end.

## Scope of this Session

Implemented four major areas:

1. **Expanded diabetes analytics tools** in `agent/tools.py`
2. **Resilient report generation pipeline** in `agent/reporter.py`
3. **Vector knowledge base + RAG memory** via `agent/kb/store.py`
4. **Runtime performance metrics** for chat and report flows via `agent/metrics.py`

---

## 1) Diabetes Analytics Expansion

### Why

The original toolset had only basic retrieval + logging. The goal was to add data-science style diabetes analysis and make reports more informative.

### What changed

Updated `agent/tools.py` with:

- Shared reading helpers:
  - `_filtered_readings(days)`
  - `_reading_values(readings)`
  - `_reading_timestamp_iso(reading)`
- New analytics tools:
  - `get_average_glucose(days=7)`
  - `get_estimated_a1c(days=14)`
  - `get_glucose_summary(days=7, low=70, high=180)`
- Improved existing tool:
  - `get_time_in_range(...)` now validates thresholds and falls back to manual TIR computation if `glucostats` fails.
- More robust initialization:
  - LibreLink auth now degrades gracefully on startup errors rather than crashing imports.

### How it works

- Tools pull CGM readings from Libre, filter by time window, normalize numeric values, and compute summary stats.
- A1C uses ADAG conversion:
  - `A1C = (average_glucose + 46.7) / 28.7`
- Time-in-range uses `glucostats` when possible, otherwise computes ratio directly from readings.

---

## 2) Reporting Reliability + Tooling Integration

### Why

Reports needed to explicitly use new analytics tools and continue working when the LLM path fails or hangs.

### What changed

Updated `agent/reporter.py`:

- Prompt construction now explicitly asks for these tools:
  - `get_glucose_summary`
  - `get_time_in_range`
  - `get_average_glucose`
  - `get_estimated_a1c`
  - `get_latest_glucose`
- Added fallback report path:
  - `_fallback_report(...)` runs tools directly and builds a deterministic report if LLM/graph fails.
- Added timeout-protected graph invocation:
  - `_invoke_graph_with_timeout(prompt)`
- Improved report filename uniqueness:
  - Timestamp now includes microseconds to avoid collisions.

### How it works

- Main attempt: graph-based report generation.
- If timeout/error: fall back to direct tool execution.
- Always writes report to `reports/` and returns saved path.

---

## 3) Vector Knowledge Base + RAG

### Why

You asked for long-term memory over historical reports and logs, with future support for carb/insulin analysis and retrieval.

### What changed

Added KB layer:

- `agent/kb/store.py`
- `agent/kb/__init__.py`

Config/deps updates:

- `config.py`
  - `KB_QDRANT_PATH = ".kb_data/qdrant"`
  - `KB_EMBEDDING_MODEL = "all-MiniLM-L6-v2"`
- `requirements.txt`
  - `qdrant-client`
  - `sentence-transformers`
- `.gitignore`
  - `.kb_data/`

### KB design

Collections:

- `report_chunks`
- `glucose_events`
- `meal_events`
- `insulin_events`

Storage behavior:

- Uses local Qdrant when available.
- Falls back to in-memory backend if Qdrant cannot initialize.
- Embeddings use `sentence-transformers`; deterministic hash embeddings as fallback.

### New KB/RAG tools in `agent/tools.py`

- `sync_glucose_to_kb(days=7)`
- `rag_search_reports(query, since_days=180, top_k=5)`
- `rag_search_glucose_patterns(query, since_days=30, top_k=10)`
- `log_meal(carbs_g, meal_type, timestamp=None, notes="")`
- `log_insulin(units, insulin_type, timing_tag, timestamp=None, notes="")`
- `search_logged_events(query, since_days=30, event_types="meal,insulin")`
- `get_icr_summary(days=14, pairing_window_minutes=60)`

Insulin enums enforced:

- `rapid`
- `basal`
- `correction`

### I:C summary behavior

- Pairs meal events to nearby **rapid** insulin events within configurable window.
- Computes observed grams/unit ratios and returns median + mean.
- Handles sparse/unmatchable data with clear messages.

---

## 4) Runtime Metrics (Latency/TTFT/Tokens)

### Why

You requested token/throughput/context usage and TTFT metrics for both prompt and report flows.

### What changed

Added `agent/metrics.py`:

- `LLMRunMetrics` dataclass
- Usage parsers:
  - `usage_from_chunk(...)`
  - `usage_from_message(...)`
- Formatter:
  - `format_metrics(...)`

Integrated into:

- `main.py` (interactive prompts)
- `agent/reporter.py` (report generation)

### Metrics captured

- `latency_seconds`
- `time_to_first_token_seconds` (TTFT, when available)
- `input_tokens`, `output_tokens`, `total_tokens` (if provider metadata exists)
- `tokens_per_second` (computed from output tokens and generation phase)

Note: on some model/provider paths, token usage may be `n/a` if metadata is not supplied.

---

## Reporter + RAG Integration

The report flow now also pulls historical context from vector memory:

- `_build_report_prompt(...)` queries report memory (`kb_store.search_reports(...)`)
- Injects top prior-context snippets into the next report prompt
- After writing report, indexes report text chunks back into KB

This creates a memory loop: **new report -> indexed -> available for future RAG context**.

---

## Test and Runtime Validation Performed

### Unit tests

- Added/updated tests in:
  - `tests/test_tools.py`
  - `tests/test_reporter.py`
- Final result during this session:
  - `52 passed`

### Real code path runs (not only tests)

Executed real flows multiple times:

- CLI prompt path via `main.py`
- `/report` generation via CLI
- direct `generate_and_save_report(...)`
- scheduler job path via `run_scheduler._report_job()`
- KB logging/search/RAG workflows via tool invocations

Observed and fixed runtime issues encountered:

- Qdrant API mismatch (`search` -> `query_points`)
- report timeout/hanging behavior
- filename collision on rapid report generation

---

## Demo Guide (Current Product)

Use this exact sequence for a clear walkthrough.

### Setup

```bash
source venv/bin/activate
python3.12 main.py
```

### Demo flow

1. **Live glucose + analytics**
   - Ask: `What is my latest glucose and 7-day summary?`
2. **Generate report**
   - Command: `/report 2`
   - Open output file under `reports/`.
3. **RAG over historical reports**
   - Ask: `Search prior reports for severe hypoglycemia patterns.`
4. **Manual meal logging**
   - Ask: `Log meal: 65g carbs, breakfast, oatmeal.`
5. **Manual insulin logging (enum-constrained)**
   - Ask: `Log insulin: 6 units rapid, pre-meal.`
6. **Search manual logs**
   - Ask: `Find logged events about oatmeal in last 30 days.`
7. **I:C trend summary**
   - Ask: `Give me an I:C summary for 30 days.`
8. **Sync CGM to KB + glucose pattern search**
   - Ask:
     - `Sync last 2 days of glucose to the KB.`
     - `Search glucose patterns for manual readings.`

### What to point out in demo

- Reports are resilient (LLM path + deterministic fallback).
- Historical reports are indexed and reused via RAG.
- Meal/insulin logs are now queryable memory.
- I:C is observational from logged behavior.
- Metrics line appears after prompts/reports, showing latency/TTFT/tokens when available.

---

## File-by-File Change Reference

- `agent/tools.py`
  - Added KB tools, meal/insulin logging, I:C summary, RAG searches, CGM sync
  - Added analytics helpers and robustness improvements
- `agent/reporter.py`
  - Prompt upgrades, RAG context injection, timeout + fallback path, KB indexing, report metrics
- `agent/kb/store.py`
  - Vector store abstraction, Qdrant + in-memory fallback, embeddings, collections, search/index methods
- `agent/kb/__init__.py`
  - KB export
- `agent/metrics.py`
  - New runtime metrics utilities
- `main.py`
  - Interactive metrics capture and display
- `config.py`
  - KB settings + report timeout setting
- `requirements.txt`
  - Qdrant + sentence-transformers dependencies
- `.gitignore`
  - Ignore local KB persistence dir
- `tests/test_tools.py`
  - Added test coverage for KB/logging/RAG/I:C tools
- `tests/test_reporter.py`
  - Added report indexing and metrics output assertions

---

## Current Limitations / Notes

- Token usage depends on provider metadata; may display `n/a`.
- Some report runs may still fall back due to LLM timeout depending on runtime load.
- I:C output is trend/observation only and not clinical dosing advice.

---

## Suggested Next Enhancements

1. Add CLI shortcuts for manual logging (`/log-meal`, `/log-insulin`).
2. Add report/backfill command to index all existing files in `reports/`.
3. Add confidence scoring + outlier filtering for I:C summaries.
4. Add persistent JSONL metrics log for performance dashboards.
