# Plan: OhioT1DM Data Pipeline & Experiment Runner

## Context

The JetsonAgent currently ingests real-time CGM data from LibreLink sensors. We want to run controlled experiments using the OhioT1DM research dataset (patient 540) by loading the XML data into a temporary SQLite database, then running predefined prompts through the agent to evaluate its responses and tool usage. This enables reproducible evaluation of the agent's analytical capabilities without requiring a live sensor connection.

---

## Files to Create

| File | Purpose |
|------|---------|
| `pipeline.py` | Parse XML, create temp SQLite DB with all patient data |
| `experiment.py` | Run agent with predefined prompts, capture tool calls + responses |
| `prompts.json` | External prompt definitions loaded by experiment.py |

---

## Part 1: pipeline.py

### Responsibilities
- Parse `data/OhioT1DM/2020/test/540-ws-testing.xml` using `xml.etree.ElementTree`
- Create a temporary SQLite database (default: `experiment_data.sqlite3`)
- Load all clinically relevant data sections into appropriate tables
- Maintain compatibility with existing `CGMStore` schema for glucose readings

### XML Timestamp Conversion
- XML format: `DD-MM-YYYY HH:MM:SS` (e.g., `"04-07-2027 00:01:44"`)
- DB format: ISO 8601 UTC (e.g., `"2027-07-04T00:01:44+00:00"`)

### Database Schema

**`cgm_readings`** (matches existing CGMStore schema exactly):
```sql
CREATE TABLE cgm_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL,
    factory_timestamp_utc TEXT NOT NULL,
    device_timestamp_local TEXT,
    glucose_mmol_l REAL NOT NULL,
    trend INTEGER,
    source TEXT DEFAULT 'ohio_t1dm',
    raw_json TEXT,
    inserted_at TEXT,
    UNIQUE(patient_id, factory_timestamp_utc, glucose_mmol_l)
);
CREATE INDEX idx_cgm_patient_ts ON cgm_readings(patient_id, factory_timestamp_utc);
CREATE INDEX idx_cgm_ts ON cgm_readings(factory_timestamp_utc);
```

**`cgm_sync_state`** (needed for CGMStore compatibility):
```sql
CREATE TABLE cgm_sync_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- Pre-populated: active_patient_id = "540"
```

**`patient_metadata`**:
```sql
CREATE TABLE patient_metadata (
    patient_id TEXT PRIMARY KEY,
    weight REAL,
    insulin_type TEXT
);
```

**`finger_stick`**:
```sql
CREATE TABLE finger_stick (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL,
    timestamp_utc TEXT NOT NULL,
    glucose_mg_dl REAL NOT NULL,
    glucose_mmol_l REAL NOT NULL
);
```

**`basal`**:
```sql
CREATE TABLE basal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL,
    timestamp_utc TEXT NOT NULL,
    rate_units_per_hr REAL NOT NULL
);
```

**`temp_basal`**:
```sql
CREATE TABLE temp_basal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL,
    ts_begin_utc TEXT NOT NULL,
    ts_end_utc TEXT NOT NULL,
    rate_units_per_hr REAL NOT NULL
);
```

**`bolus`**:
```sql
CREATE TABLE bolus (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL,
    ts_begin_utc TEXT NOT NULL,
    ts_end_utc TEXT NOT NULL,
    bolus_type TEXT NOT NULL,
    dose_units REAL NOT NULL
);
```

**`meal`**:
```sql
CREATE TABLE meal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL,
    timestamp_utc TEXT NOT NULL,
    meal_type TEXT,
    carbs_g REAL NOT NULL
);
```

### CLI Interface
```bash
python pipeline.py data/OhioT1DM/2020/test/540-ws-testing.xml --db-path experiment_data.sqlite3
```

### Key Decisions
- Glucose values stored in mmol/L (converted from mg/dL using factor 18.0)
- Skip high-frequency sensor streams (basis_gsr, basis_skin_temperature, acceleration) -- not queryable by existing tools
- Filter out finger_stick events with value="0" (invalid readings)
- Data spans approximately July 4-14, 2027

---

## Part 2: experiment.py

### Wiring Temp DB into Agent Tools

**Approach: Monkey-patch the `cgm_store` singleton**

The existing `CGMStore` singleton is imported by reference in `agent/tools.py`. We create a new `CGMStore` instance pointing at the temp DB and replace it in both locations:

```python
import agent.data.cgm_store as cgm_store_module
import agent.tools as tools_module
from agent.data.cgm_store import CGMStore

def _patch_cgm_store(db_path: str):
    new_store = CGMStore(db_path=db_path)
    cgm_store_module.cgm_store = new_store
    tools_module.cgm_store = new_store
    return new_store
```

### Experiment-Specific Tools (4 new tools)

These query the additional tables that don't exist in the current CGMStore:

1. **`get_meals(start_iso, end_iso)`** -- Query meal table for time range
2. **`get_bolus_doses(start_iso, end_iso)`** -- Query bolus table for time range
3. **`get_basal_rates(start_iso, end_iso)`** -- Query basal + temp_basal tables
4. **`get_patient_info()`** -- Return patient metadata (id, weight, insulin_type)

### Custom Agent Graph

Build a custom graph (not reusing `build_graph()` which triggers LibreLinkUp auth) with:
- Experiment-specific system prompt telling the agent the data date range (July 4-14, 2027)
- Tool set: existing SQLite-backed CGM tools + 4 new experiment tools
- Excludes live LibreLink tools (get_latest_glucose, sync_*, etc.)

**System prompt** instructs the agent to use explicit date ranges rather than "recent N days" since the data is from a fixed time period.

### Included Existing Tools (from agent/tools.py)
- `get_cgm_readings` -- explicit date range query
- `get_nearest_cgm_reading` -- nearest reading to a timestamp
- `get_cgm_summary_from_db` -- stats calculation (will work if date range covers data)
- `get_cgm_spikes_from_db` -- spike detection

### Prompt Execution & Capture

For each prompt:
1. Invoke the graph with `graph.invoke({"messages": [HumanMessage(content=prompt)]})`
2. Iterate through returned messages to extract:
   - All `AIMessage.tool_calls` (name + args)
   - All `ToolMessage.content` (tool results)
   - Final `AIMessage.content` (agent's response)
3. Collect into structured results

### Output Format (experiment_TIMESTAMP.txt)

```
JetsonAgent Experiment Results
Generated: 2026-05-11T...
Model: qwen3.5-8k:latest
Data: data/OhioT1DM/2020/test/540-ws-testing.xml
================================================================================

--- Prompt 1 ---
Q: What was the patient's glucose level at 9 AM on July 4, 2027?

Tool Calls (2):
  -> get_cgm_readings({"start_iso": "2027-07-04T08:55:00+00:00", "end_iso": "2027-07-04T09:05:00+00:00"})
     Result: [{"timestamp": "...", "glucose_mmol_l": 8.2}]
  -> get_nearest_cgm_reading({"timestamp": "2027-07-04T09:00:00+00:00"})
     Result: ...

A: The patient's glucose at 9 AM on July 4, 2027 was approximately 8.2 mmol/L (148 mg/dL).

================================================================================
```

---

## Part 3: prompts.json

External JSON file with prompt definitions:

```json
[
  "What was the patient's glucose level at 9 AM on July 4, 2027?",
  "Show me the CGM readings between 6 PM and midnight on July 5, 2027.",
  "What happened to the patient's glucose after their meals on July 4, 2027?",
  "How many bolus doses did the patient take on July 9, 2027, and what was the total insulin?",
  "Were there any hypoglycemic episodes (below 3.9 mmol/L) between July 4 and July 7, 2027?",
  "Describe the patient's overnight glucose pattern from 10 PM July 6 to 6 AM July 7, 2027. Include basal rate information.",
  "On July 5, 2027, the patient had a very high glucose reading. What does the CGM data show and what insulin was given?",
  "Give me a complete summary of July 8, 2027: meals, insulin doses, and glucose range.",
  "What do we know about this patient's profile and insulin regimen?",
  "Looking at meals and bolus doses from July 4-7, 2027, what insulin-to-carb ratio does this patient appear to use?"
]
```

---

## Part 4: Execution Flow

```
1. python pipeline.py <xml_path> --db-path experiment_data.sqlite3
   -> Parses XML, creates temp DB, prints row counts

2. python experiment.py --db-path experiment_data.sqlite3 --output results.txt
   -> Patches cgm_store to temp DB
   -> Builds experiment graph with custom tools
   -> Runs each prompt from prompts.json
   -> Writes results to .txt file

   Optional flags:
   --xml-path   (auto-runs pipeline if DB doesn't exist)
   --model      (override MODEL_NAME from config.py)
   --prompts 1 3 5  (run specific prompts by index)
```

---

## Part 5: Verification

1. **Pipeline test**: Run `pipeline.py`, then verify with sqlite3:
   ```bash
   sqlite3 experiment_data.sqlite3 "SELECT COUNT(*) FROM cgm_readings;"
   # Expected: ~2900 rows
   sqlite3 experiment_data.sqlite3 "SELECT COUNT(*) FROM meal;"
   # Expected: ~27 rows
   sqlite3 experiment_data.sqlite3 "SELECT COUNT(*) FROM bolus;"
   # Expected: ~89 rows
   ```

2. **Single prompt test**: Run `experiment.py --prompts 1` to verify end-to-end with one prompt before running all 10.

3. **Output validation**: Check the .txt output file contains prompt, tool calls with args/results, and final response for each entry.

---

## Critical Files to Modify/Reference

| File | Role |
|------|------|
| `agent/data/cgm_store.py` | CGMStore class, schema to replicate, singleton to patch |
| `agent/tools.py` | Tool definitions, cgm_store import, identify SQLite tools to reuse |
| `agent/graph.py` | Graph builder pattern to replicate for experiment graph |
| `agent/llm.py` | `get_llm()` factory used by experiment graph |
| `config.py` | MODEL_NAME and other config values |
| `data/OhioT1DM/2020/test/540-ws-testing.xml` | Source data |
