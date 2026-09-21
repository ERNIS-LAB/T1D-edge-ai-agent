# Plan: Stateful Benchmark Tools with Isolated Knowledge Base

## Goal

Extend the JetsonAgent benchmark to support **stateful tool evaluation** — tasks that require the agent to log meals, insulin doses, glucose readings, or run enrichment workflows — while keeping every benchmark run fully isolated from production state.

The current benchmark already isolates the SQLite CGM store by patching it to a temp DB per run. This plan extends that pattern to the **knowledge base (KB/Qdrant)** and **chat sessions**, and adds benchmark-safe wrappers for the stateful tools.

---

## Current Problem

Stateful tools in `agent/tools.py` write directly to shared production state:

| Tool | Writes to | Production Path |
|---|---|---|
| `log_meal` | KB vector store | `.kb_data/qdrant` |
| `log_insulin` | KB vector store | `.kb_data/qdrant` |
| `log_glucose` | KB vector store | `.kb_data/qdrant` |
| `sync_glucose_to_kb` | KB vector store | `.kb_data/qdrant` |
| `run_deferred_log_enrichment` | KB + CGM store | `.kb_data/qdrant` + `.kb_data/cgm.sqlite3` |
| `search_logged_events` | Reads from KB | `.kb_data/qdrant` |
| `rag_search_reports` | Reads from KB | `.kb_data/qdrant` |
| `rag_search_glucose_patterns` | Reads from KB | `.kb_data/qdrant` |
| `get_icr_summary` | Reads from KB | `.kb_data/qdrant` |

Running a benchmark task that calls `log_meal` would permanently add fake benchmark meals to the production KB, polluting the user's real data and making benchmark runs non-reproducible.

---

## Solution Overview

Create an **isolated benchmark environment** per run that includes:

1. **Isolated CGM SQLite DB** — already implemented via `prepare_isolated_db()`
2. **Isolated KB vector store** — new temp-directory Qdrant backend
3. **Isolated chat sessions** — new temp JSON file
4. **Patch global singletons** — same pattern used for `cgm_store`
5. **Wrap production stateful tools** — reuse the exact production tool code by patching the global `kb_store` singleton it reads from
6. **State verification scorers** — check the isolated KB after the agent runs

**Design decision: wrap, don't rewrite.** The benchmark will not create simplified copies of `log_meal`, `log_insulin`, etc. Instead it will patch the module-level `kb_store` singleton that those tools already import, so the production tools write into the isolated KB transparently. This guarantees the benchmark tests the actual code paths a user would hit.

---

## Architecture Changes

### A. New isolation module: `benchmark_isolation.py`

A helper module that centralizes all patching and cleanup logic.

```python
class BenchmarkIsolation:
    """Manages isolated state (SQLite + KB + sessions) for a single benchmark run."""

    def __init__(self, xml_path: str):
        self.temp_dir = tempfile.mkdtemp(prefix="jetson_benchmark_")
        self.db_path = str(Path(self.temp_dir) / "benchmark_data.sqlite3")
        self.kb_path = str(Path(self.temp_dir) / "kb_qdrant")
        self.sessions_path = str(Path(self.temp_dir) / "chat_sessions.json")

        # Build DB from OhioT1DM XML
        self.counts = load_ohio_t1dm_xml(xml_path=xml_path, db_path=self.db_path)

        # Create empty isolated KB
        self.kb_store = KnowledgeBaseStore(path=self.kb_path)

        # Patch global singletons
        self._patch()

    def _patch(self):
        # Same pattern as _patch_cgm_store in benchmark_runner.py
        self._old_cgm_store = getattr(cgm_store_module, "cgm_store", None)
        self._old_tools_cgm_store = getattr(tools_module, "cgm_store", None)
        self._old_kb_store = getattr(kb_store_module, "kb_store", None)
        self._old_tools_kb_store = getattr(tools_module, "kb_store", None)
        self._old_sessions_path = getattr(session_store_module, "CHAT_SESSIONS_PATH", None)

        new_cgm_store = CGMStore(db_path=self.db_path)
        setattr(cgm_store_module, "cgm_store", new_cgm_store)
        setattr(tools_module, "cgm_store", new_cgm_store)
        setattr(kb_store_module, "kb_store", self.kb_store)
        setattr(tools_module, "kb_store", self.kb_store)
        setattr(session_store_module, "CHAT_SESSIONS_PATH", self.sessions_path)

        return new_cgm_store

    def restore(self):
        # Restore all patched globals
        setattr(cgm_store_module, "cgm_store", self._old_cgm_store)
        setattr(tools_module, "cgm_store", self._old_tools_cgm_store)
        setattr(kb_store_module, "kb_store", self._old_kb_store)
        setattr(tools_module, "kb_store", self._old_tools_kb_store)
        if self._old_sessions_path is not None:
            setattr(session_store_module, "CHAT_SESSIONS_PATH", self._old_sessions_path)

    def cleanup(self):
        self.restore()
        self.kb_store.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)
```

### B. Wrapping production stateful tools

**Approach:** Do not rewrite tool logic. Instead, expose the **existing production tools** in the benchmark graph, trusting that the patched `kb_store` singleton routes writes to the isolated temp directory.

Because production tools like `log_meal` import `kb_store` at module level:

```python
# agent/tools.py (production)
from agent.kb.store import kb_store

@tool
def log_meal(...):
    point_id = kb_store.add_meal_event(...)
```

Patching `agent.kb.store.kb_store` and `agent.tools.kb_store` to the isolated instance means the **same tool code** writes to the temp KB. No wrapper reimplementation is needed.

For the benchmark graph, simply import the production tools and bind them:

```python
from agent.tools import log_meal, log_insulin, log_glucose, search_logged_events, get_icr_summary

# After patching kb_store globally, these tools are automatically "benchmark-safe"
BENCHMARK_TOOLS = [
    get_cgm_readings,
    get_nearest_cgm_reading,
    ...,
    log_meal,          # production tool, writes to patched kb_store
    log_insulin,       # production tool, writes to patched kb_store
    log_glucose,       # production tool, writes to patched kb_store
    search_logged_events,  # production tool, reads from patched kb_store
    get_icr_summary,   # production tool, reads from patched kb_store
    log_conclusion,    # benchmark-only submission tool
]
```

**Why this works:**
- Tool names, docstrings, and argument schemas stay identical — the agent needs no retraining
- Any bug fixes or behavior changes in production tools are automatically tested
- No risk of benchmark-specific tool implementations diverging from reality

**One caveat:** `sync_glucose_to_kb` and `run_deferred_log_enrichment` are excluded from Phase 2 because they depend on live LibreLinkUp data that does not exist in the offline dataset. They can be added in Phase 5 after multi-turn support is designed.

**If a future tool bypasses the singleton and creates its own `KnowledgeBaseStore()` instance,** that tool would escape isolation. The mitigation is to audit any new stateful tool to ensure it uses the module-level `kb_store` or `cgm_store` singletons.

No wrapper reimplementation is needed. The production tools are imported directly and bound to the benchmark graph after `BenchmarkIsolation` has patched the global `kb_store`.

### C. State verification scorers

In `benchmark_evaluator.py`, add scorers that inspect the isolated KB after the run:

```python
def score_logged_state(trace: dict[str, Any], task: dict[str, Any], kb_store: Any) -> tuple[float, str]:
    """Score whether the agent actually created the expected KB state."""
    # 1. Was log_meal/log_insulin/log_glucose called?
    # 2. Can we retrieve the logged event from the isolated KB?
    # 3. Do the retrieved values match the intended task?
    ...
```

### D. Task definition additions

Each stateful task needs:

```json
{
  "task_id": "ohio_log_001",
  "prompt": "Log a breakfast of 45g carbs on July 6, 2027 at 8:00 AM.",
  "category": "logging",
  "expected_tools": ["log_meal"],
  "acceptable_tools": ["log_meal"],
  "verification": {
    "type": "kb_scroll",
    "collection": "meal_events",
    "since_iso": "2027-07-06T00:00:00+00:00",
    "expected_payloads": [
      {"carbs_g": 45.0, "meal_type": "Breakfast", "timestamp": "2027-07-06T08:00:00+00:00"}
    ]
  },
  "scoring_weights": {
    "tool_correctness": 0.2,
    "argument_correctness": 0.3,
    "structured_conclusion": 0.2,
    "logged_state": 0.3
  }
}
```

---

## Implementation Phases

### Phase 1: Extract isolation logic

**Files:** `benchmark_isolation.py` (new)

**Work items:**
- Move `_patch_cgm_store` and `_restore_cgm_store` from `benchmark_runner.py` into a reusable `BenchmarkIsolation` class
- Add KB store patching (`kb_store` singleton in `agent.kb.store` and `agent.tools`)
- Add session store patching (`CHAT_SESSIONS_PATH` in `agent.session_store`)
- Ensure `cleanup()` closes the Qdrant client and deletes the temp directory

**Exit criteria:**
- A single `with BenchmarkIsolation(xml_path)` context manager can patch and restore all three state layers
- Unit test confirms production `.kb_data` is untouched after a benchmark run that exercises the KB

### Phase 2: Benchmark-safe stateful tools

**Files:** `benchmark_runner.py`

**Work items:**
- Import `log_meal`, `log_insulin`, `log_glucose`, `search_logged_events`, `get_icr_summary` from `agent.tools` and append them to the benchmark tool list
- Ensure `BenchmarkIsolation` patches `kb_store` **before** the graph is compiled so bound tools see the isolated instance
- **Exclude** `sync_glucose_to_kb` and `run_deferred_log_enrichment` initially (they depend on live LibreLinkUp data that doesn't exist offline)
- Add a smoke test that calls `log_meal` after patching and verifies `search_logged_events` finds it in the isolated KB, not production

**Exit criteria:**
- A benchmark task can call `log_meal` and the meal is queryable via `search_logged_events` within the same run
- The production `.kb_data/qdrant` directory shows no new collections after the benchmark
- MD5 hash of `.kb_data/qdrant` is identical before and after a benchmark run

### Phase 3: State verification scorers

**Files:** `benchmark_evaluator.py`

**Work items:**
- Add `score_logged_state()` that checks whether the expected events exist in the isolated KB
- Support three verification modes:
  - `kb_scroll` — scroll a collection and match payloads by field values (exact or fuzzy)
  - `kb_search` — run a semantic query and check results contain expected text
  - `sql_query` — run a query against the isolated SQLite DB (for CGM store writes)
- Hook into `evaluate_task()` as a new weighted dimension

**Exit criteria:**
- A task that requires logging a meal scores 1.0 on `logged_state` when the meal is found in the KB
- A task that requires logging insulin scores 0.0 when no insulin event is found

### Phase 4: Stateful task suite

**Files:** `benchmark_tasks.json`

**Work items:**
- Add 3–5 logging tasks:
  - `ohio_log_001`: Log a single meal and verify it exists
  - `ohio_log_002`: Log a meal + insulin dose and verify both
  - `ohio_log_003`: Log a glucose reading, then search for it
  - `ohio_log_004`: Log multiple meals, then ask the agent to compute I:C ratio
  - `ohio_log_005`: Log a meal, then query it back with a different prompt (tests memory)
- Each task includes a `verification` block that defines the expected state

**Exit criteria:**
- All logging tasks run successfully without manual code changes
- Scores are deterministic across repeated runs

### Phase 5: Multi-turn / deferred tasks (optional)

**Files:** `benchmark_runner.py`, `benchmark_evaluator.py`

**Work items:**
- Add a multi-turn runner where the user prompt can be a sequence of turns
- For `run_deferred_log_enrichment`, the task would be:
  - Turn 1: "Log a meal of 50g carbs at 8 AM."
  - Turn 2 (after delay or manual trigger): "Run deferred enrichment."
  - Verify: the meal event in the KB now has enriched notes with CGM context

**Exit criteria:**
- Multi-turn tasks can be defined in `benchmark_tasks.json` with a `turns` array
- Each turn can specify a `delay_seconds` or a `trigger` condition

### Phase 6: Validation and documentation

**Files:** `BENCHMARK.md`, `benchmark.py` CLI

**Work items:**
- Run the full suite (read-only + stateful) against at least two models
- Compare scores and verify isolation holds (no production state changes)
- Document the new `--mode stateful` or automatic stateful detection in CLI
- Update `BENCHMARK.md` with stateful usage examples

**Exit criteria:**
- `python benchmark.py --suite ohio_t1dm --mode all` runs both read-only and stateful tasks
- Production `.kb_data` is byte-for-byte identical before and after the run
- All stateful tasks exercise the exact production tool code paths (verified by code coverage or import tracing)

---

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Qdrant file locks prevent cleanup on some OSes | Use `InMemoryVectorBackend` as fallback if Qdrant can't be torn down; always prefer Qdrant when available for realism |
| Production `kb_store` is already initialized with an `atexit` handler | The benchmark patches the module-level singleton **after** initialization; `atexit` will still call `close()` on the isolated instance, which is safe |
| `sentence-transformers` embedder is heavy and slow to init per run | Reuse the same `Embedder` instance; only the backend (`QdrantBackend`/`InMemoryVectorBackend`) needs to be recreated per run |
| Production tools bypass patched singleton | Audit all stateful tools to confirm they use the module-level `kb_store` / `cgm_store` singletons; never instantiate `KnowledgeBaseStore()` locally inside a tool |
| Stateful tasks are harder to make deterministic | Use explicit timestamps in prompts; avoid `since_days=N` in tool args by overriding the tool to accept explicit `start_iso`/`end_iso` where possible |

---

## Suggested CLI Changes

Add a `--mode` flag to the benchmark CLI:

```bash
python benchmark.py --mode read-only          # current behavior (default)
python benchmark.py --mode stateful           # only stateful tasks
python benchmark.py --mode all                # both read-only and stateful
```

Or detect automatically based on which task IDs are selected.

---

## Files to Modify

| File | Change |
|---|---|
| `benchmark_isolation.py` | **New.** Centralized patching/cleanup for CGM store, KB store, and sessions |
| `benchmark_runner.py` | Add benchmark-safe stateful tools; use `BenchmarkIsolation` instead of inline patching |
| `benchmark_evaluator.py` | Add `score_logged_state()` and `verification` support |
| `benchmark_tasks.json` | Add `ohio_log_001`–`005` stateful tasks with `verification` blocks |
| `benchmark.py` | Accept `--mode` flag; wire `BenchmarkIsolation` into `run_benchmark()` |
| `BENCHMARK.md` | Document stateful benchmark usage and verification modes |

---

## Bottom Line

Stateful benchmark support is achievable by **extending the existing singleton patching pattern** to the KB and session stores. The core idea is identical to what already works for the CGM SQLite store: create a temp directory, instantiate isolated backends, patch the module-level globals, run the agent, verify state, restore globals, and clean up.

**Design decision locked:** We will **wrap** production tools by patching the global `kb_store` and `cgm_store` singletons that they import. This is the only approach that guarantees the benchmark exercises the exact code paths a real user would trigger. Rewriting tools is explicitly ruled out because it risks divergence and hidden bugs.
