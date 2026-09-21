# JetsonAgent Benchmark

## Implementation Status

The benchmark harness is implemented and runnable. It follows the tau-bench-style evaluation pattern described in the rest of this document.

### Files Added

| File | Purpose |
|---|---|
| `benchmark_tasks.json` | 17 offline OhioT1DM task definitions (10 read-only + 5 stateful + 2 multi-turn) |
| `benchmark_runner.py` | Executes tasks, captures tool calls / outputs, and records structured traces |
| `benchmark_evaluator.py` | Deterministic scoring + state verification + multi-turn scorers + optional LLM-as-a-judge |
| `benchmark_reporter.py` | Writes JSONL traces, JSONL scores, Markdown summary, and JSON summary |
| `benchmark_isolation.py` | Centralized per-run isolation for SQLite DB, KB vector store, and sessions |
| `benchmark_user_simulator.py` | Scripted and LLM-based user simulators for multi-turn tasks |
| `benchmark.py` | Main CLI entry point |

### Quick Start

```bash
# Run the full suite against the default model (read-only tasks only by default)
python benchmark.py

# Evaluate a specific model
python benchmark.py --model qwen3.5-8k:latest

# Run only selected tasks
python benchmark.py --task-ids ohio_001 ohio_003 ohio_005

# Run only stateful (logging) tasks
python benchmark.py --mode stateful

# Run only multi-turn tasks
python benchmark.py --mode multi_turn

# Run both read-only and stateful tasks
python benchmark.py --mode all

# Enable LLM-as-a-judge for prose-heavy tasks
python benchmark.py --judge-model gpt-4.1

# Keep temp artifacts for debugging
python benchmark.py --keep-db
```

Artifacts are written to `benchmark_results/` by default:
- `traces.jsonl` — per-task execution trace (prompt, tool calls, responses, latency)
- `scores.jsonl` — per-task score cards
- `summary.md` — human-readable report
- `summary.json` — machine-readable report

### Stateful Benchmarks

The benchmark now supports **stateful tool evaluation** — tasks that require the agent to `log_meal`, `log_insulin`, or `log_glucose` — while keeping every run fully isolated from production state.

**How isolation works:**
1. `BenchmarkIsolation` creates a temp directory with an isolated SQLite DB, Qdrant KB, and chat sessions file.
2. It patches the module-level `cgm_store`, `kb_store`, and `CHAT_SESSIONS_PATH` singletons so production tools write into the temp directory transparently.
3. After the run, globals are restored and the temp directory is deleted.

**Available stateful tasks:**
- `ohio_log_001` — Log a single meal and verify it exists in the KB
- `ohio_log_002` — Log a meal + insulin dose and verify both
- `ohio_log_003` — Log a glucose reading and verify it
- `ohio_log_004` — Log multiple meals on the same day
- `ohio_log_005` — Log a snack with explicit timestamp

### Multi-Turn Benchmarks

The benchmark supports **multi-turn task evaluation** for testing memory, clarification handling, and follow-up reasoning across a scripted conversation.

**How multi-turn works:**
1. Each task defines a `user_simulator` script — a sequence of user messages with per-turn expectations.
2. The agent graph receives the full conversation history, so it can reference earlier turns.
3. Pre-turn state injection (`inject_events`) can insert CGM readings or KB entries between turns.
4. Stop conditions (`tool_called`, `response_contains`) allow early termination when a turn succeeds.

**Available multi-turn tasks:**
- `ohio_multi_memory_001` — Log a meal in Turn 1, recall it in Turn 2
- `ohio_multi_clarify_001` — Vague question in Turn 1, agent asks for date, clarification in Turn 2

**Adding a multi-turn task:**

```json
{
  "task_id": "ohio_multi_memory_001",
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
          "expected_payloads": [{"carbs_g": 50.0, "meal_type": "Breakfast"}]
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
  }
}
```

Supported per-turn features:
- `stop_condition`: `"any"`, `"tool_called"`, `"response_contains"`
- `inject_events`: `"cgm_db_insert"`, `"kb_insert"`, `"delay_seconds"`
- `state_check`: same verification types as single-turn (`kb_scroll`, `kb_search`, `sql_query`, `response_contains`)

**Adding a stateful task:**
Include a `verification` block in the task definition:

```json
{
  "verification": {
    "type": "kb_scroll",
    "collection": "meal_events",
    "since_iso": "2027-07-06T00:00:00+00:00",
    "expected_payloads": [
      {"carbs_g": 45.0, "meal_type": "Breakfast", "timestamp": "2027-07-06T08:00:00+00:00"}
    ]
  }
}
```

Supported verification types:
- `kb_scroll` — scroll a collection and match payloads by field values (exact or fuzzy)
- `kb_search` — run a semantic query and check results contain expected text
- `sql_query` — run a query against the isolated SQLite DB
- `response_contains` — check the response text for expected substrings

---
# Benchmark Plan: Adapting JetsonAgent to tau-bench Style Evaluation

## Purpose

This document lays out a practical plan for benchmarking different LLMs inside the JetsonAgent environment, with `tau-bench` as the reference shape for the evaluation harness.

The main goal is not just to compare raw model quality, but to compare how well models:

- choose tools correctly,
- use the right arguments,
- manage multi-turn context,
- reason over diabetes data,
- and produce clinically useful summaries without inventing unsupported facts.

JetsonAgent is already close to a benchmarkable system, but it needs a thin evaluation layer to make runs reproducible and comparable across models.

---

## What JetsonAgent Looks Like Today

After exploring the codebase, the current shape is:

- `main.py`: interactive CLI REPL with streaming output and saved chat sessions.
- `server.py`: HTTP API wrapping the same LangGraph agent, tools, and report generation.
- `agent/graph.py`: the main ReAct-style LangGraph agent for the live diabetes assistant.
- `agent/tools.py`: the tool surface, including live LibreLinkUp access, SQLite-backed CGM tools, KB/RAG search, logging tools, and Tavily tools.
- `agent/reporter.py`: deterministic report generation with LLM drafting plus fallback logic.
- `agent/session_store.py`: persistent chat sessions on disk.
- `agent/data/cgm_store.py`: local SQLite storage for CGM readings and enrichment state.
- `pipeline.py` and `experiment.py`: an offline OhioT1DM experiment path that already behaves like a benchmark harness.

That last point matters a lot: `pipeline.py` and `experiment.py` already provide a stable offline dataset flow, which is much closer to a benchmark than the live agent loop.

---

## How tau-bench Maps to This Project

The original `tau-bench` style is built around:

- a domain-specific agent,
- a simulated user or user policy,
- domain tools,
- and a scoring/evaluation layer that checks task success across trajectories.

JetsonAgent can fit that shape, but only after some adaptation.

The best way to think about it is:

1. **JetsonAgent as the domain agent**
   - The assistant already exists as a LangGraph tool-using agent.

2. **A benchmark harness around it**
   - A runner feeds standardized tasks/prompts into the agent.
   - The harness captures tool calls, tool outputs, final responses, runtime, and token usage.

3. **A scoring layer**
   - We define what correct behavior means for each task.
   - The score should account for both textual answer quality and tool correctness.

4. **Optional simulated user turns**
   - For multi-turn scenarios, a user simulator can ask follow-up questions or provide clarifications.
   - This is useful if we want to study dialogue quality, not just one-shot Q&A.

---

## Recommended Benchmark Strategy

My recommendation is to support **two benchmark modes**:

### 1. Offline deterministic benchmark first

This should be the first and primary benchmark.

Use the existing OhioT1DM dataset loader and experiment runner to create repeatable tasks that do not depend on live LibreLinkUp credentials or external services.

This mode is ideal for:

- comparing models fairly,
- regression testing prompt/tool behavior,
- running CI-friendly benchmark smoke tests,
- and measuring report quality on a fixed dataset snapshot.

### 2. Live-agent benchmark second

This mode would exercise the real agent against live services:

- LibreLinkUp CGM data,
- local SQLite sync state,
- KB/RAG search,
- scheduled report generation.

This is useful, but it is not ideal as the baseline benchmark because it introduces network failures, credential issues, and time-varying state.

---

## Proposed Adaptation Plan

### Phase 1: Define the benchmark contract

Create a clear contract for what a benchmark run should capture.

At minimum, each run should record:

- model name,
- model provider or endpoint,
- prompt/task id,
- input conversation,
- tool calls with arguments,
- tool results,
- final assistant response,
- runtime metrics,
- and a pass/fail or graded score.

This contract should be stable across all models so runs are comparable.

### Phase 2: Reuse the offline experiment path as the first benchmark backend

`experiment.py` already does most of the hard work:

- loads prompts from `prompts.json`,
- builds a custom agent graph,
- patches the CGM store to an experiment SQLite DB,
- executes prompts,
- captures tool calls and tool results,
- and writes results to a report file.

That means the offline benchmark should likely evolve from `experiment.py`, not from the interactive CLI.

The offline benchmark should be extended to support:

- multiple model backends,
- per-task scoring,
- repeated runs with seeds or controlled temperature,
- and machine-readable output formats like JSONL.

### Phase 3: Separate the benchmark agent from the live assistant agent

The live agent in `agent/graph.py` is optimized for normal use. For benchmarking, I would prefer a dedicated benchmark graph or adapter that:

- uses a benchmark-specific system prompt,
- exposes only the allowed tools,
- avoids accidental live sync or side effects,
- and runs with a fixed recursion limit.

This is especially important because the current production tool set includes side-effectful functions like logging, syncing, and persistence.

### Phase 4: Create task bundles

A tau-bench style setup needs tasks with clear objectives.

Good task families for JetsonAgent are:

- **snapshot analysis**: summarize CGM patterns over a fixed date range,
- **event lookup**: find the nearest glucose reading or readings around a given time,
- **meal/insulin correlation**: infer likely insulin-to-carb patterns from meals and rapid insulin events,
- **trend explanation**: identify spikes, dips, and overnight patterns,
- **report drafting**: produce structured clinical summaries,
- **knowledge-base retrieval**: retrieve prior report context or event history,
- **data integrity checks**: confirm whether a query range contains enough data and whether readings are missing.

Each task should define:

- input prompt,
- expected tool families,
- reference data range,
- and evaluation criteria.

### Phase 5: Add a scoring/evaluation layer

The most important thing for a benchmark is not just whether the model answered, but whether it did the right work.

I would score along several axes:

- **Task completion**: did the answer satisfy the prompt?
- **Tool correctness**: did the model call the right tool?
- **Argument correctness**: were date ranges, thresholds, and parameters sensible?
- **Groundedness**: did the answer stay within the data?
- **Numerical correctness**: were glucose values, TIR, A1C, and ratios accurate?
- **Efficiency**: how many tool calls were required?
- **Robustness**: did the agent recover from missing data or tool errors?

For some tasks, exact match is possible.
For others, the benchmark should allow tolerance windows for numbers and structured semantic grading for prose.

### Phase 6: Make runs reproducible

To compare LLMs fairly, the benchmark must minimize hidden variability.

That means:

- `temperature = 0` for the agent under test,
- fixed tool ordering,
- fixed prompts,
- fixed dataset snapshot,
- isolated SQLite DB per run,
- isolated KB/Qdrant path per run,
- and no dependency on chat session carryover unless the task explicitly requires multi-turn memory.

---

## Suggested Harness Architecture

A clean benchmark harness would likely have these components:

1. **Dataset loader**
   - Reuse `pipeline.py` to create a SQLite snapshot from OhioT1DM XML.

2. **Agent adapter**
   - Wrap `agent/graph.py` or a benchmark-specific graph builder.
   - Swap in the model under evaluation.

3. **Task runner**
   - Load benchmark tasks from a file or directory.
   - Execute tasks one by one.

4. **Trace collector**
   - Record tool calls, message history, response text, timing, and token usage.

5. **Evaluator**
   - Compare outputs against reference answers or rubric rules.

6. **Reporter**
   - Write a summary table and per-task trace artifacts.

The current `experiment.py` is already a good seed for items 1, 2, and 4.

---

## How to Use the Existing Codebase

### `pipeline.py`

Use this to create a reproducible SQLite snapshot from the OhioT1DM XML data.

Why it matters:

- it makes the data static,
- it keeps benchmark results repeatable,
- and it avoids dependence on live sensor connectivity.

### `experiment.py`

Use this as the initial benchmark runner.

It already:

- patches the CGM store,
- creates a custom graph,
- includes experiment-only tools,
- and captures tool usage.

This is the best starting point for a tau-bench-style harness.

### `server.py`

Use this if you want to benchmark the HTTP API rather than direct graph invocation.

Why that might be useful:

- it tests the actual deployment surface,
- it supports browser/fetch-style integration,
- and it mirrors how a client would realistically use JetsonAgent.

For pure LLM comparison, though, direct graph invocation is simpler and less noisy.

### `agent/tools.py`

Be careful here: the tool set includes both read-only and stateful tools.

For benchmarking, I would create a restricted tool profile that excludes or controls:

- live LibreLinkUp calls,
- sync operations,
- and any tool that mutates persistent shared state unless the test explicitly requires it.

### `agent/session_store.py`

Persistent chat sessions are useful for normal usage, but they can contaminate benchmarks if reused across runs.

Each benchmark task should start from a clean session unless the task is explicitly multi-turn.

---

## Alternatives Worth Considering

`tau-bench` is a strong reference point, but it is not the only useful benchmark family for JetsonAgent. After reviewing the current landscape, these are the most relevant alternatives:

| Benchmark | Why it fits JetsonAgent | Main limitation |
|---|---|---|
| `tau³-bench` / `tau-bench` | Closest match for tool-agent-user interaction, with a maintained successor that supports text, voice, and knowledge-style tasks | The built-in domains still do not match diabetes or CGM workflows directly |
| `BFCL` (Berkeley Function Calling Leaderboard) | Excellent for measuring function-calling accuracy, tool selection, multi-step calls, and argument correctness | Focuses on function calls more than domain reasoning or persistent state |
| `ToolBench` / `StableToolBench` | Good for large-scale tool-use evaluation and multi-tool planning; `StableToolBench` is more reproducible than the original RapidAPI-heavy setup | Heavier infrastructure and less aligned with a local medical assistant than a custom offline benchmark |
| `AgentBench` | Useful reference for general agent behavior across multi-step environments such as OS, DB, KG, and web tasks | The environments are broad but not medically relevant, and some setups are infrastructure-heavy |
| `GAIA` | Good for evaluating tool-augmented reasoning on unambiguous questions that require external information, files, or auxiliary tools | Less focused on explicit tool traces and stateful workflows |
| `MT-Bench` / LLM-as-a-judge | Helpful as a supplemental rubric for judging final-answer quality, clarity, and helpfulness | Not sufficient alone because it does not directly measure tool choice, tool arguments, or stateful behavior |

### Practical takeaway

For JetsonAgent, I would not choose only one benchmark family.

The best mix is usually:

1. **Offline dataset benchmark** for reproducible diabetes-specific tasks.
2. **BFCL-style scoring** for tool selection and argument correctness.
3. **LLM-as-a-judge rubric** for final-answer quality when the output is prose-heavy.
4. **tau³-bench-style harness design** if you want multi-turn user interaction and a general agent benchmark structure.

That combination gives you both reliable local evaluation and a way to compare JetsonAgent against broader agent-benchmark ideas.

---

## Additional Considerations for Viability

### 1. tau-bench itself is a moving target

The upstream `tau-bench` repository now points users toward `tau³-bench` for newer tasks and fixes.

That means the benchmark concept is sound, but the exact upstream benchmark framework may not be the best long-term dependency.

If the goal is reproducible model comparison inside JetsonAgent, I would treat tau-bench as inspiration rather than a hard dependency.

### 2. This is a medical domain

JetsonAgent is a diabetes assistant, so benchmark runs may involve sensitive medical information.

That introduces concerns around:

- privacy,
- data handling,
- prompt logging,
- and whether a hosted evaluation service is appropriate.

A local/offline benchmark is strongly preferable.

### 3. Live dependencies reduce benchmark stability

The current codebase can depend on:

- LibreLinkUp authentication and network access,
- LM Studio or Ollama availability,
- Qdrant availability,
- sentence-transformers availability,
- glucostats / pandas,
- and optional Tavily search.

A benchmark should either mock these dependencies or use stable local fallbacks.

### 4. State leakage is a real problem

The app persists:

- chat sessions,
- CGM sync state,
- KB content,
- and report files.

Benchmark runs must isolate these paths, otherwise one model can affect the next run.

### 5. Tool-calling quality matters more than chat style

For this environment, benchmark quality should focus on whether the model:

- selected the correct tool,
- supplied the correct date/time range,
- used appropriate thresholds,
- and interpreted results correctly.

A fluent answer that ignores the data is worse than a concise answer that is fully grounded.

### 6. Multi-turn evaluation may not be necessary for every task

tau-bench emphasizes user-agent interaction.

JetsonAgent can support that, but many of its strongest use cases are single-turn analytical questions.

So the benchmark should probably include both:

- **single-turn analytical tasks** for accuracy and tool use,
- **multi-turn tasks** for memory, clarification, and follow-up handling.

### 7. Numerical evaluation needs tolerance

Glucose summaries, A1C estimates, and I:C ratios should not be scored with brittle string comparison alone.

Use numeric tolerances and structured parsing wherever possible.

### 8. Some current tools are not benchmark-friendly

Tools like live CGM sync, KB mutation, and report generation can be benchmarked, but they need extra care because they change state.

Read-only variants are easier to score reliably.

---

## Practical Recommendation

If I were implementing this in the repository, I would do it in the following order:

1. **Add a benchmark runner built on `experiment.py`**.
2. **Create a fixed task set for the OhioT1DM dataset**.
3. **Run multiple model backends through the same harness**.
4. **Add a JSON/JSONL trace output format**.
5. **Add a scorer for tool correctness and answer grounding**.
6. **Only then consider a tau-bench-compatible wrapper** if you want to compare against a broader agent benchmark ecosystem.

That gives you immediate value without being blocked on upstream tau-bench design choices.

---

## Bottom Line

Yes, JetsonAgent is viable to benchmark in a tau-bench-like way.

The strongest path is to:

- use the existing offline experiment pipeline as the foundation,
- isolate the agent from live side effects,
- define clear tasks and scoring rules,
- and treat tau-bench as the harness design pattern rather than a direct drop-in dependency.

If you want, the next step after this document would be to design a concrete benchmark file format and a first runnable benchmark CLI for this repository.