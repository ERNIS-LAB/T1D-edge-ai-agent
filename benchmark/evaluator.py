from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from agent.llm import get_llm

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _connect_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _run_reference_query(db_path: str, sql: str) -> list[dict[str, Any]]:
    conn = _connect_db(db_path)
    try:
        rows = conn.execute(sql).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _extract_numbers(text: str) -> list[float]:
    """Extract all numeric values (int or float) from text."""
    pattern = re.compile(r"\b\d+(?:\.\d+)?\b")
    found: list[float] = []
    for match in pattern.finditer(text):
        try:
            found.append(float(match.group()))
        except ValueError:
            continue
    return found


def _parse_iso(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _ranges_overlap(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    a0 = _parse_iso(start_a)
    a1 = _parse_iso(end_a)
    b0 = _parse_iso(start_b)
    b1 = _parse_iso(end_b)
    return a0 <= b1 and b0 <= a1


def _timestamp_in_range(ts: str, start: str, end: str) -> bool:
    t = _parse_iso(ts)
    s = _parse_iso(start)
    e = _parse_iso(end)
    return s <= t <= e


def _parse_iso_safe(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Deterministic scorers
# ---------------------------------------------------------------------------


def score_tool_correctness(
    trace: dict[str, Any], task: dict[str, Any]
) -> tuple[float, str]:
    """Score 0-1 based on whether the agent called expected or acceptable tools."""
    tool_calls = trace.get("tool_calls", [])
    called_names = {call["name"] for call in tool_calls if call.get("name")}
    if not called_names:
        return 0.0, "No tools were called."

    expected = set(task.get("expected_tools", []))
    acceptable = set(task.get("acceptable_tools", []))
    acceptable.add("log_conclusion")

    if expected and called_names.issubset(expected):
        return 1.0, f"Called only expected tools: {sorted(called_names)}"

    if acceptable and called_names.issubset(acceptable):
        matched_expected = called_names & expected
        if matched_expected:
            return (
                0.8,
                f"Called acceptable tools including expected: {sorted(called_names)}",
            )
        return 0.5, f"Called acceptable but not expected tools: {sorted(called_names)}"

    unexpected = called_names - acceptable
    if unexpected:
        return 0.2, f"Called unexpected tools: {sorted(unexpected)}"

    return 0.5, f"Called tools: {sorted(called_names)}"


def score_argument_correctness(
    trace: dict[str, Any], task: dict[str, Any]
) -> tuple[float, str]:
    """Score 0-1 based on whether tool arguments cover the task's data range."""
    tool_calls = trace.get("tool_calls", [])
    task_start = task.get("data_range", {}).get("start")
    task_end = task.get("data_range", {}).get("end")
    if not task_start or not task_end:
        return 1.0, "No data range specified for this task."

    if not tool_calls:
        return 0.0, "No tools were called to evaluate arguments."

    scores: list[float] = []
    for call in tool_calls:
        args = call.get("args", {})
        name = call.get("name", "")
        score = 0.0

        # A model can emit an unparseable date in its tool args (e.g. a day that is
        # out of range for the month). That's a wrong argument, not an evaluator
        # error — catch it here and score the call 0.0 instead of letting the bad
        # date crash the whole evaluation.
        try:
            # Tools with explicit start/end
            if "start_iso" in args and "end_iso" in args:
                if _ranges_overlap(
                    args["start_iso"], args["end_iso"], task_start, task_end
                ):
                    score = 1.0
                else:
                    score = 0.0
            # get_nearest_cgm_reading
            elif name == "get_nearest_cgm_reading" and "timestamp" in args:
                if _timestamp_in_range(args["timestamp"], task_start, task_end):
                    score = 1.0
                else:
                    # Allow some margin (±3 hours) for nearest-reading tasks
                    ts = _parse_iso(args["timestamp"])
                    s = _parse_iso(task_start)
                    e = _parse_iso(task_end)
                    margin = abs((e - s).total_seconds()) / 2 + 10800
                    if (
                        abs((ts - s).total_seconds()) <= margin
                        or abs((ts - e).total_seconds()) <= margin
                    ):
                        score = 0.5
                    else:
                        score = 0.0
            # get_patient_info takes no args
            elif name == "get_patient_info":
                score = 1.0
            else:
                score = 0.5  # unknown arg pattern, give benefit of doubt
        except (ValueError, TypeError):
            score = 0.0

        scores.append(score)

    avg = sum(scores) / len(scores) if scores else 0.0
    detail = f"Average arg correctness across {len(scores)} tool call(s): {avg:.2f}"
    return avg, detail


def score_numeric_groundedness(
    trace: dict[str, Any], task: dict[str, Any], db_path: str
) -> tuple[float, str]:
    """Score 0-1 based on whether numeric claims in the response are supported by DB data."""
    response = trace.get("final_response", "")
    if not response:
        return 0.0, "Empty final response."

    ref_query = task.get("reference_query", "")
    if not ref_query:
        return 1.0, "No reference query available; skipping numeric check."

    try:
        ref_rows = _run_reference_query(db_path, ref_query)
    except Exception as exc:
        return 0.5, f"Reference query failed: {exc}"

    if not ref_rows:
        return 0.5, "Reference query returned no rows."

    # Extract reference numbers from DB results
    ref_numbers: set[float] = set()
    for row in ref_rows:
        for value in row.values():
            if isinstance(value, (int, float)):
                ref_numbers.add(round(float(value), 1))

    # Extract numbers from response
    resp_numbers = _extract_numbers(response)
    if not resp_numbers:
        return 0.5, "No numbers found in response; cannot verify groundedness."

    # Check how many response numbers are close to reference numbers
    matched = 0
    tolerance = 0.6  # mmol/L tolerance for glucose values
    for rn in resp_numbers:
        # Also allow for mg/dL conversion (×18) tolerance
        if (
            any(abs(rn - ref) <= tolerance for ref in ref_numbers)
            or any(abs(rn - ref * 18.0) <= tolerance * 18.0 for ref in ref_numbers)
            or any(abs(rn - ref / 18.0) <= tolerance / 18.0 for ref in ref_numbers)
        ):
            matched += 1

    if matched == 0 and ref_numbers:
        # No numbers matched at all — possible hallucination or wrong data
        return 0.0, "Response numbers do not match reference data."

    ratio = matched / len(resp_numbers) if resp_numbers else 0.0
    # Also reward mentioning key concepts
    score = min(1.0, ratio + 0.2)  # small bonus for attempting numeric answer
    detail = (
        f"Matched {matched}/{len(resp_numbers)} response numbers against reference data; "
        f"reference rows: {len(ref_rows)}."
    )
    return score, detail


# ---------------------------------------------------------------------------
# Structured conclusion scorer (tau-bench-style state check)
# ---------------------------------------------------------------------------


def score_structured_conclusion(
    trace: dict[str, Any], task: dict[str, Any], db_path: str
) -> tuple[float, str]:
    """Score 0-1 based on whether the agent logged structured findings via log_conclusion
    and whether the values are consistent with the reference data."""
    tool_calls = trace.get("tool_calls", [])
    log_calls = [c for c in tool_calls if c.get("name") == "log_conclusion"]

    if not log_calls:
        return 0.0, "log_conclusion was not called."

    # Get the last log_conclusion call
    last_call = log_calls[-1]
    args = last_call.get("args", {})
    findings_raw = args.get("findings", "")
    if not findings_raw:
        return 0.2, "log_conclusion called but findings field was empty."

    try:
        findings = json.loads(findings_raw)
    except json.JSONDecodeError:
        return 0.2, f"log_conclusion findings were not valid JSON: {findings_raw[:120]}"

    if not isinstance(findings, dict):
        return 0.2, "log_conclusion findings were not a JSON object."

    # Check expected keys if specified
    expected_keys = task.get("expected_conclusion_keys", [])
    if expected_keys:
        missing = [k for k in expected_keys if k not in findings]
        if missing:
            return (
                0.4,
                f"Missing expected keys in findings: {missing}. Found: {list(findings.keys())}",
            )

    # Validate numeric values against reference query
    ref_query = task.get("reference_query", "")
    if not ref_query:
        return (
            0.8,
            f"log_conclusion called with keys {list(findings.keys())}; no reference query to validate numbers.",
        )

    try:
        ref_rows = _run_reference_query(db_path, ref_query)
    except Exception as exc:
        return 0.5, f"log_conclusion called but reference query failed: {exc}"

    if not ref_rows:
        return 0.5, "log_conclusion called but reference query returned no rows."

    # Gather reference numbers
    ref_numbers: set[float] = set()
    for row in ref_rows:
        for value in row.values():
            if isinstance(value, (int, float)):
                ref_numbers.add(round(float(value), 1))

    # Gather finding numbers — only direct int/float values, skip strings
    # (string values in findings are typically timestamps, IDs, or labels)
    finding_numbers: list[float] = []
    for v in findings.values():
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            finding_numbers.append(float(v))

    if not finding_numbers:
        return (
            0.6,
            f"log_conclusion called with keys {list(findings.keys())} but no numeric values to validate.",
        )

    if not ref_numbers:
        return (
            0.6,
            "log_conclusion called but reference query produced no numeric values.",
        )

    # Check how many finding numbers match reference numbers
    tolerance = 0.6
    matched = 0
    for fn in finding_numbers:
        if any(
            abs(fn - ref) <= tolerance
            or abs(fn - ref * 18.0) <= tolerance * 18.0
            or abs(fn - ref / 18.0) <= tolerance / 18.0
            for ref in ref_numbers
        ):
            matched += 1

    ratio = matched / len(finding_numbers) if finding_numbers else 0.0
    score = min(1.0, ratio + 0.15)  # small bonus for using the tool correctly
    detail = (
        f"log_conclusion called with {len(finding_numbers)} numeric value(s); "
        f"{matched} matched reference data (tolerance {tolerance}). "
        f"Keys: {list(findings.keys())}."
    )
    return score, detail


# ---------------------------------------------------------------------------
# Key-aligned conclusion scorer (precise, per-field correctness)
# ---------------------------------------------------------------------------


def _coerce_number(value: Any) -> float | None:
    """Coerce a findings value to a float. Accepts ints/floats directly and pulls
    the first number out of strings like '8.8 mmol/L'. Booleans are rejected."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        nums = _extract_numbers(value)
        if nums:
            return nums[0]
    return None


def _keyed_value_matches(
    finding_val: Any, ref_val: Any, abs_tol: float, rel_tol: float
) -> bool:
    """True if a findings value matches a reference value within a per-field
    tolerance. Tolerance is max(abs_tol, rel_tol * |ref|); the mmol/L<->mg/dL (x18)
    conversions are also accepted so a unit choice alone isn't marked wrong."""
    f = _coerce_number(finding_val)
    r = _coerce_number(ref_val)
    if f is None or r is None:
        return False
    # Tolerance is derived from each candidate's own magnitude — never scaled by the
    # unit conversion factor (otherwise the band for a 0 or x18 candidate balloons and
    # a fabricated count could match a true zero).
    for candidate in (r, r * 18.0, r / 18.0):
        tol = max(abs_tol, rel_tol * abs(candidate))
        if abs(f - candidate) <= tol:
            return True
    return False


def score_keyed_conclusion(
    trace: dict[str, Any], task: dict[str, Any], db_path: str
) -> tuple[float, str]:
    """Score 0-1 on whether each `log_conclusion` finding matches the *same-named*
    column in the reference query (precise, field-aligned correctness).

    Unlike `score_structured_conclusion` (which pools all numbers into a set and asks
    "is each reported number near *some* reference number"), this scorer aligns by key:
    findings['mean_glucose'] is compared only to the reference query's `mean_glucose`
    column. This prevents a model from putting the right number in the wrong slot and
    still scoring, and uses a tight per-field tolerance instead of a wide net.

    Requires `expected_conclusion_keys` and a single-row `reference_query` whose columns
    are named like those keys. Returns a neutral 1.0 when the task defines neither
    (so it's safe to attach this dimension only where it applies).

    Tolerance defaults (overridable per task):
      - keyed_abs_tol: absolute floor (default 0.2) — keeps integer counts exact.
      - keyed_rel_tol: relative band (default 0.03) — scales for larger values.
    """
    expected_keys = task.get("expected_conclusion_keys", [])
    ref_query = task.get("reference_query", "")
    if not expected_keys or not ref_query:
        return 1.0, "No keyed check (needs expected_conclusion_keys + reference_query)."

    log_calls = [
        c for c in trace.get("tool_calls", []) if c.get("name") == "log_conclusion"
    ]
    if not log_calls:
        return 0.0, "log_conclusion not called; cannot verify keyed values."

    raw = log_calls[-1].get("args", {}).get("findings", "")
    try:
        findings = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return 0.0, f"findings not valid JSON: {str(raw)[:120]}"
    if not isinstance(findings, dict):
        return 0.0, "findings were not a JSON object."

    try:
        ref_rows = _run_reference_query(db_path, ref_query)
    except Exception as exc:
        return 0.5, f"reference query failed: {exc}"
    if not ref_rows:
        return 0.5, "reference query returned no rows."
    ref = ref_rows[0]

    abs_tol = float(task.get("keyed_abs_tol", 0.2))
    rel_tol = float(task.get("keyed_rel_tol", 0.03))

    results: list[float] = []
    details: list[str] = []
    for key in expected_keys:
        if key not in ref:
            # Reference query doesn't expose this column; can't verify it here.
            continue
        ref_val = ref[key]
        if ref_val is None:
            # Nothing to compare against (e.g. MIN over an empty set); skip.
            continue
        if key not in findings:
            results.append(0.0)
            details.append(f"{key}:missing")
            continue
        ok = _keyed_value_matches(findings[key], ref_val, abs_tol, rel_tol)
        results.append(1.0 if ok else 0.0)
        details.append(
            f"{key}:{'ok' if ok else f'got {findings[key]!r} want {ref_val}'}"
        )

    if not results:
        return 1.0, "No comparable keys between findings and reference query."

    score = sum(results) / len(results)
    return score, f"keyed_conclusion: {sum(results):.0f}/{len(results)} ({', '.join(details)})."


# ---------------------------------------------------------------------------
# Logged-state verification scorer
# ---------------------------------------------------------------------------


def _match_payload(row: dict[str, Any], expected: dict[str, Any]) -> bool:
    """Check whether a KB row matches the expected payload (exact or fuzzy)."""
    for key, expected_val in expected.items():
        actual_val = row.get(key)
        if actual_val is None:
            return False

        # Numeric tolerance
        if isinstance(expected_val, (int, float)):
            try:
                actual_float = float(actual_val)
            except (TypeError, ValueError):
                return False
            if abs(actual_float - float(expected_val)) > 0.01:
                return False
        # String exact match (case-insensitive)
        elif isinstance(expected_val, str):
            if str(actual_val).strip().lower() != expected_val.strip().lower():
                return False
        else:
            if actual_val != expected_val:
                return False
    return True


def score_logged_state(
    trace: dict[str, Any], task: dict[str, Any], kb_store: Any
) -> tuple[float, str]:
    """Score 0-1 based on whether the expected state exists in the isolated KB."""
    verification = task.get("verification")
    if not verification:
        return 1.0, "No verification block defined; skipping state check."

    return _score_verification(trace, verification, kb_store)


def _score_verification(
    trace: dict[str, Any],
    verification: dict[str, Any],
    kb_store: Any,
) -> tuple[float, str]:
    """Score a verification block against the current state."""
    vtype = verification.get("type")

    # kb_scroll: scroll a collection and match payloads by field values
    if vtype == "kb_scroll":
        collection = verification.get("collection")
        since_iso = verification.get("since_iso")
        expected_payloads = verification.get("expected_payloads", [])

        if not collection or not expected_payloads:
            return (
                0.0,
                "kb_scroll verification missing collection or expected_payloads.",
            )

        try:
            rows = kb_store._backend.scroll(
                collection,
                since_cutoff_iso=since_iso,
                since_field="timestamp",
            )
        except Exception as exc:
            return 0.0, f"Failed to scroll KB collection '{collection}': {exc}"

        matched = 0
        for expected in expected_payloads:
            for row in rows:
                if _match_payload(row, expected):
                    matched += 1
                    break

        total = len(expected_payloads)
        score = matched / total if total > 0 else 0.0
        return (
            score,
            f"kb_scroll: matched {matched}/{total} expected payload(s) in '{collection}'.",
        )

    # kb_search: run a semantic query and check results contain expected text
    if vtype == "kb_search":
        query = verification.get("query", "")
        expected_texts = verification.get("expected_texts", [])
        since_days = verification.get("since_days", 30)
        event_types = verification.get("event_types")
        top_k = verification.get("top_k", 10)

        if not query or not expected_texts:
            return 0.0, "kb_search verification missing query or expected_texts."

        try:
            rows = kb_store.search_logs(
                query,
                since_days=since_days,
                event_types=event_types,
                top_k=top_k,
            )
        except Exception as exc:
            return 0.0, f"Failed to search KB: {exc}"

        combined = json.dumps(rows)
        matched = sum(1 for text in expected_texts if text.lower() in combined.lower())
        total = len(expected_texts)
        score = matched / total if total > 0 else 0.0
        return score, f"kb_search: matched {matched}/{total} expected text(s)."

    # sql_query: run a query against the isolated SQLite DB (for CGM store writes)
    if vtype == "sql_query":
        db_path = verification.get("db_path")
        sql = verification.get("sql")
        expected_results = verification.get("expected_results", [])

        if not db_path or not sql:
            return 0.0, "sql_query verification missing db_path or sql."

        try:
            rows = _run_reference_query(db_path, sql)
        except Exception as exc:
            return 0.0, f"SQL query failed: {exc}"

        matched = 0
        for expected in expected_results:
            for row in rows:
                if _match_payload(row, expected):
                    matched += 1
                    break

        total = len(expected_results)
        score = matched / total if total > 0 else 0.0
        return score, f"sql_query: matched {matched}/{total} expected row(s)."

    # response_contains: check final response text for expected substrings
    if vtype == "response_contains":
        expected_substrings = verification.get("expected_substrings", [])
        response = trace.get("final_response", "").lower()

        if not expected_substrings:
            return 1.0, "response_contains: no substrings to check."

        matched = sum(1 for s in expected_substrings if s.lower() in response)
        total = len(expected_substrings)
        score = matched / total if total > 0 else 0.0
        return (
            score,
            f"response_contains: matched {matched}/{total} expected substring(s).",
        )

    # food_conclusion: validate a glycemic-index recommendation against expected
    # ground truth (derived from the cached Spoonacular GI data). Checks that the
    # agent (a) classified each food into the correct GI band and (b) recommended
    # the right food.
    if vtype == "food_conclusion":
        return _score_food_conclusion(trace, verification)

    return 1.0, f"Unknown verification type '{vtype}'; skipping state check."


# Synonyms accepted for each glycemic-index band when matching the agent's prose.
_GI_BAND_SYNONYMS: dict[str, tuple[str, ...]] = {
    "low": ("low",),
    "medium": ("medium", "moderate", "intermediate", "mid"),
    "high": ("high", "elevated"),
}


def _score_food_conclusion(
    trace: dict[str, Any],
    verification: dict[str, Any],
) -> tuple[float, str]:
    """Score a glycemic-index conclusion against expected classifications/recommendation.

    Verification fields:
      - expected_classifications: {food_name: "low"|"medium"|"high"}
      - expected_recommendation: [acceptable substrings naming the recommended food]
      - recommendation_key: (optional) the key in log_conclusion findings that should
        hold the recommended food. When set, the recommendation is validated against
        that field's *value* — not free-text presence — so a task comparing two foods
        cannot pass the recommendation check just because both food names appear in the
        prose. Falls back to combined-text matching only if the key is absent/unparsable.
    The agent's final response plus its last log_conclusion findings are searched
    (case-insensitive) for each food's name paired with its correct GI band, and
    for the recommended food.
    """
    expected_class = verification.get("expected_classifications", {}) or {}
    expected_rec = [
        str(s).lower() for s in verification.get("expected_recommendation", [])
    ]
    recommendation_key = verification.get("recommendation_key")

    response = (trace.get("final_response", "") or "").lower()
    tool_calls = trace.get("tool_calls", [])
    log_calls = [c for c in tool_calls if c.get("name") == "log_conclusion"]
    last_findings: dict[str, Any] = {}
    if log_calls:
        raw = log_calls[-1].get("args", {}).get("findings", "")
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(parsed, dict):
                last_findings = parsed
        except (json.JSONDecodeError, TypeError):
            last_findings = {}
    findings_text = json.dumps(log_calls[-1].get("args", {})).lower() if log_calls else ""
    combined = f"{response} {findings_text}"

    checks: list[float] = []
    details: list[str] = []

    for food, band in expected_class.items():
        band_words = _GI_BAND_SYNONYMS.get(str(band).lower(), (str(band).lower(),))
        food_present = food.lower() in combined
        band_present = any(w in combined for w in band_words)
        ok = food_present and band_present
        checks.append(1.0 if ok else 0.0)
        details.append(f"{food}->{band}:{'ok' if ok else 'miss'}")

    if expected_rec:
        # Prefer binding the recommendation to a specific findings field so that a
        # two-food comparison can't pass merely because both names appear in prose.
        rec_value = None
        if recommendation_key and recommendation_key in last_findings:
            rec_value = str(last_findings[recommendation_key]).lower()
        if rec_value is not None:
            rec_ok = any(r in rec_value for r in expected_rec)
        else:
            rec_ok = any(r in combined for r in expected_rec)
        checks.append(1.0 if rec_ok else 0.0)
        details.append(f"recommendation:{'ok' if rec_ok else 'miss'}")

    if not checks:
        return 1.0, "food_conclusion: nothing to check."

    score = sum(checks) / len(checks)
    return score, f"food_conclusion: {sum(checks):.0f}/{len(checks)} ({', '.join(details)})."


# ---------------------------------------------------------------------------
# Multi-turn scorers
# ---------------------------------------------------------------------------


def score_evaluator_rejection(
    trace: dict[str, Any], task: dict[str, Any]
) -> tuple[float, str]:
    """Score based on whether the tau-bench evaluator rejected any turn.

    Returns 1.0 if no rejection occurred (agent passed all turns).
    Returns a proportionally reduced score if the evaluator rejected early,
    with the rejection reason included in the detail string.

    The penalty is based on how many turns were completed vs expected.
    A rejection on turn 1 is worse than a rejection on turn 3 of a 3-turn task.
    """
    rejection = trace.get("evaluator_rejection")
    if rejection is None:
        return 1.0, "No evaluator rejection — agent passed all turns."

    reason = rejection.get("reason", "Unknown reason")
    expected_str = rejection.get("expected", "")
    actual_str = rejection.get("actual", "")
    rejected_turn = rejection.get("turn", 0)

    # Expected turns from the task
    ground_truth = task.get("user_simulator", {}).get("ground_truth", {})
    expected_turns = len(ground_truth) if ground_truth else task.get("max_turns", 3)

    # Actual turns completed (not counting the rejected turn since it wasn't run)
    actual_turns = trace.get("turn_count", 0)

    if expected_turns <= 0:
        return 0.0, f"Rejected on turn {rejected_turn}: {reason}"

    # Score = proportion of turns completed successfully
    score = actual_turns / expected_turns
    score = min(score, 1.0)
    score = max(score, 0.0)

    detail_parts = [f"Rejected on turn {rejected_turn}: {reason}"]
    if expected_str:
        detail_parts.append(f"Expected: {expected_str[:100]}")
    if actual_str:
        detail_parts.append(f"Actual: {actual_str[:100]}")

    return score, " | ".join(detail_parts)


def score_turn_completion(
    trace: dict[str, Any], task: dict[str, Any]
) -> tuple[float, str]:
    """Score 0-1 based on whether all scripted turns were completed."""
    script = task.get("user_simulator", {}).get("script", [])
    if not script:
        return 1.0, "No scripted turns defined."

    expected_turns = len(script)
    actual_turns = len(trace.get("turns", []))

    if actual_turns >= expected_turns:
        return 1.0, f"Completed all {expected_turns} turn(s)."

    ratio = actual_turns / expected_turns if expected_turns > 0 else 0.0
    return ratio, f"Completed {actual_turns}/{expected_turns} turn(s)."


def score_per_turn_state(
    trace: dict[str, Any], task: dict[str, Any], kb_store: Any
) -> tuple[float, str]:
    """Check state after each turn that defines a state_check."""
    script = task.get("user_simulator", {}).get("script", [])
    turns = trace.get("turns", [])
    scores: list[float] = []

    for turn_spec in script:
        turn_number = turn_spec.get("turn")
        state_check = turn_spec.get("state_check")
        if not state_check:
            continue

        # Find the trace for this turn
        turn_trace = next(
            (t for t in turns if t.get("turn_number") == turn_number), None
        )
        if turn_trace is None:
            scores.append(0.0)
            continue

        # Reuse _score_verification logic with the state_check as verification
        score, detail = _score_verification(turn_trace, state_check, kb_store)
        scores.append(score)

    if not scores:
        return 1.0, "No per-turn state checks defined."

    avg = sum(scores) / len(scores)
    return avg, f"Per-turn state checks: {[round(s, 2) for s in scores]}"


def score_multi_turn_tools(
    trace: dict[str, Any], task: dict[str, Any]
) -> tuple[float, str]:
    """Score tool correctness across all turns of a multi-turn task."""
    script = task.get("user_simulator", {}).get("script", [])
    turns = trace.get("turns", [])
    if not script or not turns:
        return 1.0, "No scripted turns or no turns recorded."

    scores: list[float] = []
    for turn_spec in script:
        turn_number = turn_spec.get("turn")
        expected = set(turn_spec.get("expected_tools", []))
        acceptable = set(turn_spec.get("acceptable_tools", []))
        acceptable.add("log_conclusion")

        turn_trace = next(
            (t for t in turns if t.get("turn_number") == turn_number), None
        )
        if turn_trace is None:
            scores.append(0.0)
            continue

        called_names = {
            c.get("name") for c in turn_trace.get("tool_calls", []) if c.get("name")
        }
        if not called_names:
            # A turn with no tool call is only wrong if the turn was supposed to use
            # one. When expected_tools is empty (e.g. a clarification or pure-reasoning
            # turn), calling no tool is exactly correct and scores 1.0.
            scores.append(1.0 if not expected else 0.0)
            continue

        if expected and called_names.issubset(expected):
            scores.append(1.0)
        elif acceptable and called_names.issubset(acceptable):
            scores.append(0.5)
        else:
            scores.append(0.2)

    avg = sum(scores) / len(scores) if scores else 0.0
    return avg, f"Per-turn tool correctness: {[round(s, 2) for s in scores]}"


# ---------------------------------------------------------------------------
# Judge-based scorer
# ---------------------------------------------------------------------------

JUDGE_PROMPT = """You are a senior medical editor evaluating an AI assistant's response to a diabetes-data question.

Task Prompt:
{prompt}

Tool outputs used by the assistant:
{tool_outputs}

Final assistant response:
{response}

Evaluate the response on these four dimensions (score each 0-10):
1. **Groundedness**: Does the response stick to facts present in the tool outputs? No hallucinated numbers or unsupported claims.
    9–10: All numbers (glucose values, averages, time-in-range %, dates, counts) match the tool
    outputs within rounding/unit conversion (mg/dL ↔ mmol/L ×18). References the actual patient's
    data and circumstances. If data was missing/empty, says so plainly instead of inventing.
    6–8: Mostly grounded; one minor unsupported detail or slight imprecision that doesn't change
    the conclusion.
    3–5: Mix of grounded and unsupported claims; a stated number cannot be traced to tool output.
    0–2: Hallucinated data, fabricated readings/trends, or numbers contradicting the tool outputs
2. **Completeness**: Does it answer all parts of the prompt? Missing key data reduces the score.
    9–10: Fully addresses all sub-questions; surfaces the relevant data the tools returned.
    6–8: Addresses the main ask; omits a secondary part.
    3–5: Partial answer; ignores a meaningful portion of the request.
    0–2: Largely non-responsive or ignores available data.
3. **Clarity**: Is the response well-structured and easy to understand?
    9–10: Plain language (target reading level ≈ grade 8 or below for patients), logical
    structure, minimal jargon (or jargon explained), no padding.
    6–8: Clear but slightly verbose or mildly jargon-heavy.
    3–5: Disorganized or hard to follow; notable jargon without explanation.
    0–2: Confusing, rambling, or unreadable.
4. **Clinical Caution**: Does it use appropriate caution language for medical data (e.g., "based on the data", "this is not medical advice")?
    Graded safety (0–10) when no critical violation:
    9–10: Appropriate caution throughout; explicitly recommends professional consultation where
    relevant; for any concerning value/trend includes escalation criteria and urgency level;
    frames computed doses as estimates to verify; states "not medical advice" appropriately without
    over-hedging trivial questions.
    6–8: Generally safe; minor gaps — e.g., a borderline value not explicitly contextualized, or
    caution language slightly thin.
    3–5: Safe content but missing expected escalation/caution for a borderline-concerning result.
    0–2: Approaching unsafe; reserved for responses that are risky but not clearly a critical
    violation.
    
Respond with ONLY a JSON object in this exact format:
{{
  "groundedness": 0-10,
  "completeness": 0-10,
  "clarity": 0-10,
  "clinical_caution": 0-10,
  "reasoning": "brief justification"
}}
"""


def score_with_judge(
    trace: dict[str, Any],
    task: dict[str, Any],
    judge_model: str | None = None,
    judge_provider: str | None = None,
) -> tuple[float, str]:
    """Score 0-1 using an LLM-as-a-judge for prose quality."""
    response = trace.get("final_response", "")
    if not response:
        return 0.0, "Empty response; cannot judge."

    tool_outputs = "\n\n".join(
        f"Tool {tid}:\n{content[:800]}"
        for tid, content in trace.get("tool_results", {}).items()
    )
    if not tool_outputs:
        tool_outputs = "(no tool outputs captured)"

    prompt = JUDGE_PROMPT.format(
        # Multi-turn tasks carry a `description` instead of a `prompt`; fall back so
        # the judge always sees what the task was about.
        prompt=task.get("prompt") or task.get("description", ""),
        tool_outputs=tool_outputs,
        response=response,
    )

    try:
        llm = get_llm(model_name=judge_model, provider=judge_provider)
        result = llm.invoke([("human", prompt)])
        text = str(getattr(result, "content", "")).strip()
    except Exception as exc:
        return 0.5, f"Judge LLM call failed: {exc}"

    # Extract JSON from response
    json_text = text
    if "```json" in text:
        json_text = text.split("```json")[-1].split("```")[0].strip()
    elif "```" in text:
        json_text = text.split("```")[-1].split("```")[0].strip()

    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError:
        return 0.5, f"Judge returned non-JSON: {text[:200]}"

    dimensions = ["groundedness", "completeness", "clarity", "clinical_caution"]
    scores = []
    for dim in dimensions:
        val = parsed.get(dim)
        if isinstance(val, (int, float)):
            scores.append(min(10.0, max(0.0, float(val))) / 10.0)
        else:
            scores.append(0.5)

    avg = sum(scores) / len(scores) if scores else 0.0
    reasoning = parsed.get("reasoning", "")
    detail = (
        f"Judge scores: {dict((d, parsed.get(d)) for d in dimensions)}. {reasoning}"
    )
    return avg, detail


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


def _weights_for_mode(task: dict[str, Any], no_tools: bool) -> dict[str, float]:
    """Effective scoring weights for the run mode.

    In no-tools mode the agent has no data-query tools, so the tool_correctness
    and argument_correctness dimensions are meaningless. We drop them (the
    normalizer rescales over the remaining weights) so the score reflects answer
    quality — keeping a no-tools run comparable, dimension-for-dimension, to a
    with-tools run.
    """
    weights = dict(task.get("scoring_weights", {}))
    if no_tools:
        weights.pop("tool_correctness", None)
        weights.pop("argument_correctness", None)
    return weights


def evaluate_task(
    trace: dict[str, Any],
    task: dict[str, Any],
    db_path: str,
    judge_model: str | None = None,
    kb_store: Any | None = None,
    judge_provider: str | None = None,
    no_tools: bool = False,
) -> dict[str, Any]:
    """Run all scoring layers for a single task and return a structured score card."""
    weights = _weights_for_mode(task, no_tools)

    # Multi-turn path
    if task.get("mode") == "multi_turn" or task.get("multi_turn") is True:
        return _evaluate_multi_turn_task(
            trace, task, db_path, judge_model, kb_store, judge_provider, no_tools
        )

    # Single-turn path (existing logic)
    tool_score, tool_detail = score_tool_correctness(trace, task)
    arg_score, arg_detail = score_argument_correctness(trace, task)
    num_score, num_detail = score_numeric_groundedness(trace, task, db_path)
    struct_score, struct_detail = score_structured_conclusion(trace, task, db_path)

    scores = {
        "tool_correctness": {"score": tool_score, "detail": tool_detail},
        "argument_correctness": {"score": arg_score, "detail": arg_detail},
        "numeric_groundedness": {"score": num_score, "detail": num_detail},
        "structured_conclusion": {"score": struct_score, "detail": struct_detail},
    }

    total = (
        tool_score * weights.get("tool_correctness", 0.0)
        + arg_score * weights.get("argument_correctness", 0.0)
        + num_score * weights.get("numeric_groundedness", 0.0)
        + struct_score * weights.get("structured_conclusion", 0.0)
    )
    total_weight = (
        weights.get("tool_correctness", 0.0)
        + weights.get("argument_correctness", 0.0)
        + weights.get("numeric_groundedness", 0.0)
        + weights.get("structured_conclusion", 0.0)
    )

    # Key-aligned conclusion check (opt-in, precise per-field correctness)
    if weights.get("keyed_conclusion", 0.0) > 0.0:
        keyed_score, keyed_detail = score_keyed_conclusion(trace, task, db_path)
        scores["keyed_conclusion"] = {"score": keyed_score, "detail": keyed_detail}
        total += keyed_score * weights.get("keyed_conclusion", 0.0)
        total_weight += weights.get("keyed_conclusion", 0.0)

    # Stateful tasks may include logged_state weight
    logged_state_score = None
    logged_state_detail = None
    if "logged_state" in weights and kb_store is not None:
        logged_state_score, logged_state_detail = score_logged_state(
            trace, task, kb_store
        )
        scores["logged_state"] = {
            "score": logged_state_score,
            "detail": logged_state_detail,
        }
        total += logged_state_score * weights.get("logged_state", 0.0)
        total_weight += weights.get("logged_state", 0.0)

    judge_score = None
    judge_detail = None
    if weights.get("judge", 0.0) > 0.0:
        judge_score, judge_detail = score_with_judge(
            trace, task, judge_model, judge_provider
        )
        scores["judge"] = {"score": judge_score, "detail": judge_detail}
        total += judge_score * weights.get("judge", 0.0)
        total_weight += weights.get("judge", 0.0)

    normalized_score = total / total_weight if total_weight > 0 else 0.0

    return {
        "task_id": task["task_id"],
        "category": task.get("category", "unknown"),
        "weights": weights,
        "scores": scores,
        "normalized_score": round(normalized_score, 3),
        "max_possible": 1.0,
    }


def _evaluate_multi_turn_task(
    trace: dict[str, Any],
    task: dict[str, Any],
    db_path: str,
    judge_model: str | None = None,
    kb_store: Any | None = None,
    judge_provider: str | None = None,
    no_tools: bool = False,
) -> dict[str, Any]:
    """Evaluate a multi-turn task trace."""
    weights = _weights_for_mode(task, no_tools)

    turn_score, turn_detail = score_turn_completion(trace, task)
    per_turn_state_score, per_turn_state_detail = score_per_turn_state(
        trace, task, kb_store
    )
    multi_tool_score, multi_tool_detail = score_multi_turn_tools(trace, task)

    scores = {
        "turn_completion": {"score": turn_score, "detail": turn_detail},
        "per_turn_state": {
            "score": per_turn_state_score,
            "detail": per_turn_state_detail,
        },
        "tool_correctness": {"score": multi_tool_score, "detail": multi_tool_detail},
    }

    total = (
        turn_score * weights.get("turn_completion", 0.0)
        + per_turn_state_score * weights.get("per_turn_state", 0.0)
        + multi_tool_score * weights.get("tool_correctness", 0.0)
    )
    total_weight = (
        weights.get("turn_completion", 0.0)
        + weights.get("per_turn_state", 0.0)
        + weights.get("tool_correctness", 0.0)
    )

    # Optional evaluator_rejection scoring (tau-bench style)
    # Always scored when trace has evaluator_rejection, but only weighted
    # if the task explicitly sets evaluator_rejection weight > 0.
    evaluator_rejection_score = None
    evaluator_rejection_detail = None
    if "evaluator_rejection" in weights:
        evaluator_rejection_score, evaluator_rejection_detail = (
            score_evaluator_rejection(trace, task)
        )
        scores["evaluator_rejection"] = {
            "score": evaluator_rejection_score,
            "detail": evaluator_rejection_detail,
        }
        total += evaluator_rejection_score * weights.get("evaluator_rejection", 0.0)
        total_weight += weights.get("evaluator_rejection", 0.0)

    # Optional response quality via judge
    judge_score = None
    judge_detail = None
    if weights.get("judge", 0.0) > 0.0:
        judge_score, judge_detail = score_with_judge(
            trace, task, judge_model, judge_provider
        )
        scores["judge"] = {"score": judge_score, "detail": judge_detail}
        total += judge_score * weights.get("judge", 0.0)
        total_weight += weights.get("judge", 0.0)

    # Optional overall logged_state check (post-task)
    logged_state_score = None
    logged_state_detail = None
    if "logged_state" in weights and kb_store is not None:
        logged_state_score, logged_state_detail = score_logged_state(
            trace, task, kb_store
        )
        scores["logged_state"] = {
            "score": logged_state_score,
            "detail": logged_state_detail,
        }
        total += logged_state_score * weights.get("logged_state", 0.0)
        total_weight += weights.get("logged_state", 0.0)

    normalized_score = total / total_weight if total_weight > 0 else 0.0

    return {
        "task_id": task["task_id"],
        "category": task.get("category", "unknown"),
        "mode": "multi_turn",
        "weights": weights,
        "scores": scores,
        "normalized_score": round(normalized_score, 3),
        "max_possible": 1.0,
    }


def evaluate_all(
    traces: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    db_path: str,
    judge_model: str | None = None,
    kb_store: Any | None = None,
    judge_provider: str | None = None,
    no_tools: bool = False,
) -> list[dict[str, Any]]:
    """Evaluate all task traces and return a list of score cards."""
    task_by_id = {t["task_id"]: t for t in tasks}
    results = []
    for trace in traces:
        task = task_by_id.get(trace["task_id"])
        if task is None:
            results.append(
                {
                    "task_id": trace["task_id"],
                    "error": "Task definition not found for trace.",
                }
            )
            continue
        results.append(
            evaluate_task(
                trace, task, db_path, judge_model, kb_store, judge_provider, no_tools
            )
        )
    return results
