# agent/reporter.py
# Generates a periodic glucose report with an LLM review loop:
#   1. Gather raw data deterministically
#   2. LLM writes a draft
#   3. LLM critiques the draft (accuracy, completeness, tone)
#   4. If critique requests changes, LLM rewrites
#   5. Save final report and index in KB
import json
import os
import queue
import threading
import uuid
from datetime import datetime
from typing import Any

from agent.kb.store import kb_store
from agent.llm import get_llm
from agent.metrics import LLMRunMetrics, format_metrics, now
from agent.tools import (
    get_cgm_spikes_from_db,
    get_cgm_summary_from_db,
    get_estimated_a1c,
    get_glucose_summary,
    get_latest_glucose,
    get_time_in_range,
    get_average_glucose,
)
from config import REPORTS_DIR, REPORT_LLM_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# Data gathering
# ---------------------------------------------------------------------------

def _gather_report_data(period_days: int) -> dict[str, str]:
    """Run all relevant data tools deterministically and return raw outputs."""
    data: dict[str, str] = {}

    # Primary: SQLite-backed summary
    db_summary = get_cgm_summary_from_db.invoke({"days": period_days, "low": 3.9, "high": 10.0})
    data["cgm_summary"] = str(db_summary)

    # Fallback to LibreLink API if DB is empty
    if "No CGM readings found" in data["cgm_summary"]:
        data["cgm_summary"] = str(
            get_glucose_summary.invoke({"days": period_days, "low": 3.9, "high": 10.0})
        )

    data["latest_glucose"] = str(get_latest_glucose.invoke({}))

    spikes = get_cgm_spikes_from_db.invoke(
        {
            "days": period_days,
            "min_rise_mmol_l": 2.2,
            "min_drop_mmol_l": 2.2,
            "window_minutes": 120,
        }
    )
    data["spikes_and_dips"] = str(spikes)

    # RAG context from prior reports
    rag_rows = kb_store.search_reports(
        query="recent glucose trends, severe hypo or hyperglycemia, time in range, A1C",
        since_days=180,
        top_k=3,
    )
    context_lines = []
    for row in rag_rows:
        snippet = str(row.get("content", "")).replace("\n", " ")[:220]
        context_lines.append(f"- {row.get('file_path', 'unknown')}: {snippet}")
    data["rag_context"] = (
        "\n".join(context_lines) if context_lines else "No prior report context found."
    )

    return data


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def _invoke_llm_with_timeout(
    llm: Any,
    messages: list[dict[str, str]],
    timeout_seconds: int = REPORT_LLM_TIMEOUT_SECONDS,
) -> Any:
    """Invoke an LLM with a thread-based timeout."""
    result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

    def _runner() -> None:
        try:
            result = llm.invoke(messages)
            result_queue.put(("ok", result))
        except Exception as exc:
            result_queue.put(("err", exc))

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()

    try:
        status, payload = result_queue.get(timeout=timeout_seconds)
    except queue.Empty as exc:
        raise TimeoutError(f"LLM call exceeded {timeout_seconds}s timeout") from exc

    if status == "err":
        raise payload
    return payload


def _draft_report(data: dict[str, str], period_days: int, llm: Any) -> str:
    """Generate an initial report draft from raw data."""
    messages = [
        {
            "role": "system",
            "content": (
                "You are a meticulous diabetes data analyst. Write clear, accurate, "
                "structured glucose reports based solely on the provided raw data."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Generate a detailed glucose report covering the past {period_days} days.\n\n"
                "Use ONLY the raw data below. Do not invent numbers.\n\n"
                f"--- CGM Summary ---\n{data['cgm_summary']}\n\n"
                f"--- Latest Glucose ---\n{data['latest_glucose']}\n\n"
                f"--- Spikes and Dips ---\n{data['spikes_and_dips']}\n\n"
                f"--- Prior Report Context ---\n{data['rag_context']}\n\n"
                "Structure the report with these sections:\n"
                "1. Executive Summary (avg, min, max, TIR, estimated A1C)\n"
                "2. Current Status (latest reading)\n"
                "3. Notable Patterns (spikes, dips, trends)\n"
                "4. Concerns and Recommendations\n\n"
                "Report:"
            ),
        },
    ]
    result = _invoke_llm_with_timeout(llm, messages)
    return str(getattr(result, "content", "")).strip()


def _parse_critique(text: str) -> dict[str, str]:
    """Extract verdict and feedback from critique response."""
    text = text.strip()

    # Try markdown code-block extraction
    if "```json" in text:
        text = text.split("```json")[-1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[-1].split("```")[0].strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "verdict" in parsed:
            return {
                "verdict": str(parsed.get("verdict", "REVISE")).upper(),
                "feedback": str(parsed.get("feedback", "")),
            }
    except json.JSONDecodeError:
        pass

    # Text fallback
    verdict = "COMPLETE" if "VERDICT: COMPLETE" in text.upper() else "REVISE"
    return {"verdict": verdict, "feedback": text}


def _critique_report(draft: str, data: dict[str, str], period_days: int, llm: Any) -> dict[str, str]:
    """Have the LLM review the draft for accuracy and completeness."""
    messages = [
        {
            "role": "system",
            "content": (
                "You are a senior medical editor. Review glucose reports for numerical accuracy, "
                "completeness, and appropriate clinical tone. Output ONLY a JSON object."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Review this {period_days}-day glucose report draft against the raw data.\n\n"
                f"--- Raw Data ---\n"
                f"CGM Summary:\n{data['cgm_summary']}\n\n"
                f"Latest Glucose:\n{data['latest_glucose']}\n\n"
                f"Spikes and Dips:\n{data['spikes_and_dips']}\n\n"
                f"--- Draft Report ---\n{draft}\n\n"
                "Evaluate:\n"
                "- Are all numbers accurate vs raw data?\n"
                "- Is the interpretation clinically sound?\n"
                "- Is anything important missing?\n"
                "- Is the tone appropriate?\n\n"
                'Respond with ONLY this JSON: {"verdict": "COMPLETE" or "REVISE", "feedback": "..."}'
            ),
        },
    ]
    result = _invoke_llm_with_timeout(llm, messages)
    text = str(getattr(result, "content", "")).strip()
    return _parse_critique(text)


def _revise_report(draft: str, feedback: str, data: dict[str, str], period_days: int, llm: Any) -> str:
    """Revise the report based on editorial feedback."""
    messages = [
        {
            "role": "system",
            "content": (
                "You are a diabetes data analyst revising a report based on editor feedback. "
                "Preserve accuracy and improve clarity."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Revise this {period_days}-day glucose report based on the editor feedback.\n\n"
                f"--- Raw Data ---\n"
                f"CGM Summary:\n{data['cgm_summary']}\n\n"
                f"Latest Glucose:\n{data['latest_glucose']}\n\n"
                f"Spikes and Dips:\n{data['spikes_and_dips']}\n\n"
                f"--- Current Draft ---\n{draft}\n\n"
                f"--- Editor Feedback ---\n{feedback}\n\n"
                "Produce the full revised report with the same sections:\n"
                "1. Executive Summary\n"
                "2. Current Status\n"
                "3. Notable Patterns\n"
                "4. Concerns and Recommendations\n\n"
                "Revised Report:"
            ),
        },
    ]
    result = _invoke_llm_with_timeout(llm, messages)
    return str(getattr(result, "content", "")).strip()


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------

def _fallback_report(period_days: int, reason: str) -> str:
    """Deterministic fallback when the LLM review pipeline fails."""
    tool_outputs: list[str] = []

    tool_plan = [
        ("Summary", get_glucose_summary, {"days": period_days, "low": 3.9, "high": 10.0}),
        ("Time in range", get_time_in_range, {"days": period_days, "low": 3.9, "high": 10.0}),
        ("Average glucose", get_average_glucose, {"days": period_days}),
        ("Estimated A1C", get_estimated_a1c, {"days": period_days}),
        ("Latest glucose", get_latest_glucose, {}),
    ]

    for label, tool_obj, payload in tool_plan:
        try:
            output = tool_obj.invoke(payload)
        except Exception as exc:
            output = f"Tool failed: {exc}"
        tool_outputs.append(f"- {label}: {output}")

    return (
        "Automated glucose report generated via direct tool execution "
        f"(LLM unavailable: {reason}).\n\n"
        + "\n".join(tool_outputs)
    )


# ---------------------------------------------------------------------------
# Save helper
# ---------------------------------------------------------------------------

def _save_report(report_text: str, period_days: int, run_start: float) -> str:
    """Save report to disk, index in KB, print metrics, and return path."""
    os.makedirs(REPORTS_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
    filename = os.path.join(REPORTS_DIR, f"glucose_report_{timestamp}.txt")

    with open(filename, "w") as f:
        f.write(f"Glucose Report — generated {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
        f.write(f"Period: past {period_days} days\n")
        f.write("=" * 60 + "\n\n")
        f.write(report_text)

    report_id = str(uuid.uuid4())
    kb_store.add_report_document(
        report_id=report_id,
        file_path=filename,
        content=report_text,
        period_days=period_days,
    )

    latency = now() - run_start
    metrics = LLMRunMetrics(
        latency_seconds=latency,
        time_to_first_token_seconds=None,
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
    )
    print(f"[Metrics] report {format_metrics(metrics)}", flush=True)
    print(f"[Reporter] Report saved to {filename}", flush=True)
    return filename


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_and_save_report(period_days: int, max_iterations: int = 2) -> str:
    """Generate a glucose report with an LLM review loop.

    Flow:
      1. Gather raw data deterministically
      2. LLM writes draft
      3. LLM critiques draft (up to max_iterations times)
      4. If critique requests revision, LLM rewrites
      5. Save final report and index in KB
    """
    print(f"[Agent] Gathering data for {period_days}-day report...", flush=True)
    run_start = now()

    try:
        data = _gather_report_data(period_days)
    except Exception as exc:
        print(f"[Reporter] Data gathering failed: {exc}", flush=True)
        report_text = _fallback_report(
            period_days=period_days, reason=f"data gathering failed: {exc}"
        )
        return _save_report(report_text, period_days, run_start)

    print("[Agent] Generating draft...", flush=True)
    llm = get_llm()

    try:
        draft = _draft_report(data, period_days, llm)
    except Exception as exc:
        print(f"[Reporter] Draft generation failed: {exc}", flush=True)
        report_text = _fallback_report(
            period_days=period_days, reason=f"draft generation failed: {exc}"
        )
        return _save_report(report_text, period_days, run_start)

    report_text = draft
    for iteration in range(1, max_iterations + 1):
        print(f"[Agent] Critique iteration {iteration}/{max_iterations}...", flush=True)
        try:
            critique = _critique_report(report_text, data, period_days, llm)
        except Exception as exc:
            print(f"[Reporter] Critique failed: {exc}", flush=True)
            break

        if critique.get("verdict") == "COMPLETE":
            print(f"[Agent] Critique passed on iteration {iteration}.", flush=True)
            break

        feedback = critique.get("feedback", "")
        print(
            f"[Agent] Revising based on feedback: {feedback[:120]}...",
            flush=True,
        )
        try:
            report_text = _revise_report(report_text, feedback, data, period_days, llm)
        except Exception as exc:
            print(f"[Reporter] Revision failed: {exc}", flush=True)
            break
    else:
        print("[Agent] Max critique iterations reached.", flush=True)

    return _save_report(report_text, period_days, run_start)
