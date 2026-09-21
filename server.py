"""HTTP API entry point for DiabetesAgent.

This server is designed to be consumed by browser/client `fetch()` calls.
It provides endpoints for:
- Chatting with the LangGraph agent (including `/help` command support)
- Generating reports and optionally returning report content
- Listing/retrieving generated report files
- Reading/writing/searching the local vector knowledge base
- Syncing and querying the local SQLite CGM time-series database

Run:
    python3.12 server.py

Example fetch:
    fetch("http://localhost:8000/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: "What is my latest glucose?" })
    })
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import Request, urlopen

from agent.data.cgm_store import cgm_store
from agent.graph import build_graph
from agent.kb.store import kb_store, coerce_iso
from agent.metrics import LLMRunMetrics, format_metrics, now, usage_from_message
from agent.reporter import generate_and_save_report
from agent.session_store import load_sessions, save_sessions
from agent.tools import (
    VALID_INSULIN_TYPES,
    get_icr_summary,
    run_deferred_log_enrichment,
    sync_cgm_to_db,
    sync_glucose_to_kb,
    sync_latest_cgm_to_db,
)
from config import (
    CGM_ENRICHMENT_MAX_ATTEMPTS,
    CGM_ENRICHMENT_POLL_MINUTES,
    CGM_LOG_ENRICHMENT_WINDOW_MINUTES,
    CGM_SYNC_BACKFILL_DAYS,
    CGM_SYNC_ENABLED,
    CGM_SYNC_INTERVAL_MINUTES,
    MODEL_NAME,
    REPORTS_DIR,
    LM_STUDIO_BASE_URL,
)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_CORS_ORIGIN = "*"


_GRAPH_CACHE: dict[str, Any] = {}
_GRAPH_LOCK = threading.Lock()
_SESSIONS: dict[str, list[dict[str, str]]] = load_sessions()
_SESSIONS_LOCK = threading.Lock()
_SERVER_DEFAULT_MODEL: str | None = None
_SERVER_CORS_ORIGIN: str = DEFAULT_CORS_ORIGIN
_CGM_SYNC_STOP = threading.Event()
_CGM_SYNC_THREAD: threading.Thread | None = None
_CGM_ENRICH_STOP = threading.Event()
_CGM_ENRICH_THREAD: threading.Thread | None = None
_CGM_WORKER_STATUS_LOCK = threading.Lock()
_CGM_SYNC_RUNTIME: dict[str, Any] = {
    "running": False,
    "last_run_started_at": None,
    "last_run_completed_at": None,
    "last_result": None,
    "next_run_at": None,
    "last_mode": None,
}
_CGM_ENRICH_RUNTIME: dict[str, Any] = {
    "running": False,
    "last_run_started_at": None,
    "last_run_completed_at": None,
    "last_result": None,
    "next_run_at": None,
}


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _set_sync_runtime(**updates: Any) -> None:
    with _CGM_WORKER_STATUS_LOCK:
        _CGM_SYNC_RUNTIME.update(updates)


def _set_enrich_runtime(**updates: Any) -> None:
    with _CGM_WORKER_STATUS_LOCK:
        _CGM_ENRICH_RUNTIME.update(updates)


def _worker_status_payload() -> dict[str, Any]:
    now_dt = datetime.now(timezone.utc)
    with _CGM_WORKER_STATUS_LOCK:
        sync_state = dict(_CGM_SYNC_RUNTIME)
        enrich_state = dict(_CGM_ENRICH_RUNTIME)

    def with_countdown(state: dict[str, Any]) -> dict[str, Any]:
        next_run = state.get("next_run_at")
        seconds_until: float | None = None
        if isinstance(next_run, str) and next_run:
            try:
                next_dt = datetime.fromisoformat(next_run)
                if next_dt.tzinfo is None:
                    next_dt = next_dt.replace(tzinfo=timezone.utc)
                seconds_until = max(0.0, (next_dt.astimezone(timezone.utc) - now_dt).total_seconds())
            except Exception:
                seconds_until = None
        state["seconds_until_next_run"] = seconds_until
        return state

    return {
        "sync_worker": with_countdown(sync_state),
        "enrichment_worker": with_countdown(enrich_state),
        "sync_interval_minutes": CGM_SYNC_INTERVAL_MINUTES,
        "enrichment_poll_minutes": CGM_ENRICHMENT_POLL_MINUTES,
        "enabled": CGM_SYNC_ENABLED,
    }


def _key_for_model(model_name: str | None) -> str:
    return model_name or "__default__"


def _get_graph(model_name: str | None = None):
    key = _key_for_model(model_name)
    with _GRAPH_LOCK:
        if key not in _GRAPH_CACHE:
            _GRAPH_CACHE[key] = build_graph(model_name=model_name)
        return _GRAPH_CACHE[key]


def _safe_str_content(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    return str(content)


def _to_metrics_dict(metrics: LLMRunMetrics) -> dict[str, Any]:
    return {
        "latency_seconds": metrics.latency_seconds,
        "time_to_first_token_seconds": metrics.time_to_first_token_seconds,
        "input_tokens": metrics.input_tokens,
        "output_tokens": metrics.output_tokens,
        "total_tokens": metrics.total_tokens,
        "tokens_per_second": metrics.tokens_per_second,
        "summary": format_metrics(metrics),
    }


def _persist_sessions_locked() -> None:
    save_sessions(_SESSIONS)


def _append_session_turn(
    *,
    session_id: str | None,
    reset_session: bool,
    user_message: str,
    assistant_message: str,
) -> None:
    if not session_id:
        return

    with _SESSIONS_LOCK:
        if reset_session:
            _SESSIONS[session_id] = []
        history = list(_SESSIONS.get(session_id, []))
        messages_payload = history + [{"role": "user", "content": user_message}]
        _SESSIONS[session_id] = messages_payload + [{"role": "assistant", "content": assistant_message}]
        _persist_sessions_locked()


def _chat_help_text() -> str:
    return (
        "DiabetesAgent chat help\n\n"
        "Slash commands:\n"
        "- /help : show this help message\n\n"
        "Agent features:\n"
        "- Latest glucose lookup + trend awareness\n"
        "- Historical glucose analysis (summary, spikes, time in range, estimated A1C)\n"
        "- Logging: glucose, meals, and insulin doses\n"
        "- Semantic search over historical reports and logged events\n"
        "- CGM sync + enrichment workflows\n"
        "- I:C ratio summary from meal/insulin history\n"
        "- Multi-turn chat memory via session_id\n\n"
        "Useful API endpoints:\n"
        "- POST /api/chat\n"
        "- POST /api/report  (supports include_content=true)\n"
        "- GET  /api/reports\n"
        "- GET  /api/reports/latest?include_content=true\n"
        "- GET  /api/reports/<file_name>?include_content=true\n"
        "- POST /api/cgm/sync\n"
        "- GET  /api/cgm/worker-status\n"
        "- POST /api/kb/icr-summary\n"
    )


def _reports_root() -> Path:
    return Path(REPORTS_DIR).resolve()


def _sorted_report_paths(limit: int | None = None) -> list[Path]:
    root = _reports_root()
    if not root.exists():
        return []

    report_paths = [p for p in root.glob("glucose_report_*.txt") if p.is_file()]
    report_paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if limit is not None:
        return report_paths[: max(0, limit)]
    return report_paths


def _report_metadata(report_path: Path) -> dict[str, Any]:
    stat = report_path.stat()
    modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    return {
        "file_name": report_path.name,
        "report_path": str(report_path),
        "size_bytes": stat.st_size,
        "modified_at": modified_at,
    }


def _read_report_content(report_path: Path, *, max_chars: int | None = None) -> tuple[str, bool]:
    content = report_path.read_text(encoding="utf-8")
    if max_chars is not None and len(content) > max_chars:
        return content[:max_chars], True
    return content, False


def _resolve_report_path(file_name: str) -> Path | None:
    if not file_name or "/" in file_name or "\\" in file_name:
        return None

    root = _reports_root()
    candidate = (root / file_name).resolve()
    if candidate.parent != root:
        return None
    if not candidate.exists() or not candidate.is_file():
        return None
    return candidate


def _query_bool_value(value: str, *, field_name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"`{field_name}` must be a boolean (true/false)")


def _parse_model_ids(models_payload: Any) -> list[str]:
    if not isinstance(models_payload, dict):
        return []

    rows = models_payload.get("data")
    if not isinstance(rows, list):
        return []

    model_ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = row.get("id")
        if isinstance(model_id, str) and model_id.strip():
            model_ids.append(model_id.strip())
    return model_ids


def _llm_connectivity_status(timeout_seconds: float = 1.5) -> dict[str, Any]:
    models_url = LM_STUDIO_BASE_URL.rstrip("/") + "/models"
    configured_model = _SERVER_DEFAULT_MODEL or MODEL_NAME

    request = Request(models_url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        return {
            "ok": False,
            "models_url": models_url,
            "configured_model": configured_model,
            "error": f"HTTP {exc.code}: {exc.reason}",
        }
    except URLError as exc:
        return {
            "ok": False,
            "models_url": models_url,
            "configured_model": configured_model,
            "error": str(exc.reason or exc),
        }
    except Exception as exc:  # pragma: no cover - defensive runtime guard
        return {
            "ok": False,
            "models_url": models_url,
            "configured_model": configured_model,
            "error": str(exc),
        }

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "models_url": models_url,
            "configured_model": configured_model,
            "error": "Invalid JSON returned from /models",
        }

    model_ids = _parse_model_ids(payload)
    model_available = configured_model in model_ids if model_ids else None
    return {
        "ok": True,
        "models_url": models_url,
        "configured_model": configured_model,
        "configured_model_available": model_available,
        "model_count": len(model_ids),
        "available_models": model_ids,
    }


def _run_cgm_sync(days: int) -> str:
    """Run a full-window CGM sync from Libre logbook into SQLite."""
    try:
        return str(sync_cgm_to_db.invoke({"days": int(days)}))
    except Exception as exc:  # pragma: no cover - defensive runtime guard
        cgm_store.set_sync_state("last_sync_error", str(exc))
        return f"CGM full sync failed: {exc}"


def _run_cgm_latest_sync() -> str:
    """Poll latest CGM value and upsert into SQLite."""
    try:
        return str(sync_latest_cgm_to_db.invoke({}))
    except Exception as exc:  # pragma: no cover - defensive runtime guard
        cgm_store.set_sync_state("last_sync_error", str(exc))
        return f"CGM latest sync failed: {exc}"


def _run_deferred_enrichment(limit: int = 25) -> str:
    try:
        return str(
            run_deferred_log_enrichment.invoke(
                {"limit": int(limit), "max_attempts": int(CGM_ENRICHMENT_MAX_ATTEMPTS)}
            )
        )
    except Exception as exc:  # pragma: no cover - defensive runtime guard
        return f"Deferred enrichment failed: {exc}"


def _cgm_sync_worker(stop_event: threading.Event) -> None:
    _set_sync_runtime(running=True, next_run_at=None)

    # Initial catch-up at server start.
    _set_sync_runtime(last_run_started_at=_utc_iso_now(), last_mode="full")
    first_result = _run_cgm_sync(CGM_SYNC_BACKFILL_DAYS)
    _set_sync_runtime(last_run_completed_at=_utc_iso_now(), last_result=first_result)
    print(f"[CGM Sync] startup full sync: {first_result}")

    # Then poll latest CGM value every N minutes.
    _set_sync_runtime(last_run_started_at=_utc_iso_now(), last_mode="latest")
    latest_result = _run_cgm_latest_sync()
    completed_iso = _utc_iso_now()
    next_iso = (datetime.now(timezone.utc) + timedelta(minutes=CGM_SYNC_INTERVAL_MINUTES)).isoformat()
    _set_sync_runtime(
        last_run_completed_at=completed_iso,
        last_result=latest_result,
        next_run_at=next_iso,
    )
    print(f"[CGM Sync] startup latest: {latest_result}")

    interval_seconds = max(5, int(CGM_SYNC_INTERVAL_MINUTES * 60))
    while not stop_event.wait(interval_seconds):
        _set_sync_runtime(last_run_started_at=_utc_iso_now(), last_mode="latest", next_run_at=None)
        result = _run_cgm_latest_sync()
        completed_iso = _utc_iso_now()
        next_iso = (datetime.now(timezone.utc) + timedelta(minutes=CGM_SYNC_INTERVAL_MINUTES)).isoformat()
        _set_sync_runtime(
            last_run_completed_at=completed_iso,
            last_result=result,
            next_run_at=next_iso,
        )
        print(f"[CGM Sync] periodic latest: {result}")
        # Tiny sleep guard to avoid a tight loop in edge cases.
        time.sleep(0.01)

    _set_sync_runtime(running=False, next_run_at=None)


def _cgm_enrichment_worker(stop_event: threading.Event) -> None:
    _set_enrich_runtime(running=True, next_run_at=None)

    # Run once quickly on startup to process already-due jobs.
    _set_enrich_runtime(last_run_started_at=_utc_iso_now())
    first = _run_deferred_enrichment(limit=50)
    next_iso = (datetime.now(timezone.utc) + timedelta(minutes=CGM_ENRICHMENT_POLL_MINUTES)).isoformat()
    _set_enrich_runtime(last_run_completed_at=_utc_iso_now(), last_result=first, next_run_at=next_iso)
    print(f"[CGM Enrich] startup: {first}")

    poll_seconds = max(5, int(CGM_ENRICHMENT_POLL_MINUTES * 60))
    while not stop_event.wait(poll_seconds):
        _set_enrich_runtime(last_run_started_at=_utc_iso_now(), next_run_at=None)
        result = _run_deferred_enrichment(limit=50)
        next_iso = (datetime.now(timezone.utc) + timedelta(minutes=CGM_ENRICHMENT_POLL_MINUTES)).isoformat()
        _set_enrich_runtime(last_run_completed_at=_utc_iso_now(), last_result=result, next_run_at=next_iso)
        print(f"[CGM Enrich] periodic: {result}")
        time.sleep(0.01)

    _set_enrich_runtime(running=False, next_run_at=None)


def _start_cgm_sync_worker() -> None:
    global _CGM_SYNC_THREAD

    if not CGM_SYNC_ENABLED:
        _set_sync_runtime(running=False, next_run_at=None)
        print("[CGM Sync] disabled via config")
        return
    if _CGM_SYNC_THREAD is not None and _CGM_SYNC_THREAD.is_alive():
        return

    _CGM_SYNC_STOP.clear()
    _CGM_SYNC_THREAD = threading.Thread(
        target=_cgm_sync_worker,
        args=(_CGM_SYNC_STOP,),
        daemon=True,
        name="cgm-sync-worker",
    )
    _CGM_SYNC_THREAD.start()


def _start_cgm_enrichment_worker() -> None:
    global _CGM_ENRICH_THREAD

    if not CGM_SYNC_ENABLED:
        _set_enrich_runtime(running=False, next_run_at=None)
        print("[CGM Enrich] disabled via config")
        return
    if _CGM_ENRICH_THREAD is not None and _CGM_ENRICH_THREAD.is_alive():
        return

    _CGM_ENRICH_STOP.clear()
    _CGM_ENRICH_THREAD = threading.Thread(
        target=_cgm_enrichment_worker,
        args=(_CGM_ENRICH_STOP,),
        daemon=True,
        name="cgm-enrichment-worker",
    )
    _CGM_ENRICH_THREAD.start()


def _stop_cgm_sync_worker() -> None:
    global _CGM_SYNC_THREAD
    thread = _CGM_SYNC_THREAD
    if thread is None:
        _set_sync_runtime(running=False, next_run_at=None)
        return
    _CGM_SYNC_STOP.set()
    thread.join(timeout=2)
    _CGM_SYNC_THREAD = None
    _set_sync_runtime(running=False, next_run_at=None)


def _stop_cgm_enrichment_worker() -> None:
    global _CGM_ENRICH_THREAD
    thread = _CGM_ENRICH_THREAD
    if thread is None:
        _set_enrich_runtime(running=False, next_run_at=None)
        return
    _CGM_ENRICH_STOP.set()
    thread.join(timeout=2)
    _CGM_ENRICH_THREAD = None
    _set_enrich_runtime(running=False, next_run_at=None)


def _active_patient_id() -> str | None:
    value = cgm_store.get_sync_state("active_patient_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _cgm_context_note_for_timestamp(
    timestamp_iso: str,
    *,
    window_minutes: int = CGM_LOG_ENRICHMENT_WINDOW_MINUTES,
) -> tuple[str, bool]:
    context = cgm_store.get_post_log_context(
        timestamp_iso=timestamp_iso,
        window_minutes=window_minutes,
        patient_id=_active_patient_id(),
    )
    if not context:
        return (
            f"CGM context pending (0–{window_minutes} min post-log window).",
            False,
        )

    note = (
        f"CGM context (0–{window_minutes} min post-log): "
        f"{context['reading_count']} readings, "
        f"start {context['start_glucose_mmol_l']:.1f} mmol/L @ {context['start_timestamp']}, "
        f"end {context['end_glucose_mmol_l']:.1f} mmol/L @ {context['end_timestamp']}, "
        f"min {context['min_glucose_mmol_l']:.1f}, max {context['max_glucose_mmol_l']:.1f}, "
        f"delta {context['delta_mmol_l']:+.1f} mmol/L, "
        f"peak rise {context['peak_rise_mmol_l']:+.1f} mmol/L, "
        f"peak drop {context['min_glucose_mmol_l'] - context['start_glucose_mmol_l']:+.1f} mmol/L."
    )
    return note, True


def _enriched_notes(notes: str, context_note: str) -> str:
    notes_clean = notes.strip()
    if notes_clean:
        return f"{notes_clean}\n\n{context_note}"
    return context_note


def _invoke_agent(
    *,
    message: str,
    session_id: str | None,
    model_name: str | None,
    reset_session: bool,
) -> dict[str, Any]:
    graph = _get_graph(model_name=model_name or _SERVER_DEFAULT_MODEL)

    if session_id:
        with _SESSIONS_LOCK:
            if reset_session:
                _SESSIONS[session_id] = []
                _persist_sessions_locked()
            history = list(_SESSIONS.get(session_id, []))
    else:
        history = []

    messages_payload = history + [{"role": "user", "content": message}]

    run_start = now()
    result = graph.invoke(
        {"messages": messages_payload},
        config={"recursion_limit": 25},
    )
    latency = now() - run_start

    final_message = result["messages"][-1]
    response_text = _safe_str_content(final_message)
    input_tokens, output_tokens, total_tokens = usage_from_message(final_message)
    metrics = LLMRunMetrics(
        latency_seconds=latency,
        time_to_first_token_seconds=None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )

    if session_id:
        with _SESSIONS_LOCK:
            _SESSIONS[session_id] = messages_payload + [{"role": "assistant", "content": response_text}]
            _persist_sessions_locked()

    tool_call_names: list[str] = []
    for msg in result.get("messages", []):
        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            continue
        for tc in tool_calls:
            if isinstance(tc, dict):
                name = tc.get("name")
                if isinstance(name, str):
                    tool_call_names.append(name)

    return {
        "session_id": session_id,
        "model": model_name or _SERVER_DEFAULT_MODEL or MODEL_NAME,
        "response": response_text,
        "tool_calls": tool_call_names,
        "metrics": _to_metrics_dict(metrics),
    }


class DiabetesAgentHTTPRequestHandler(BaseHTTPRequestHandler):
    server_version = "DiabetesAgentHTTP/1.0"

    def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", _SERVER_CORS_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        length_header = self.headers.get("Content-Length", "0")
        try:
            length = int(length_header)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length header") from exc

        raw = self.rfile.read(length) if length > 0 else b"{}"
        if not raw:
            return {}

        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Request body must be valid JSON") from exc

        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def _normalized_path(self) -> str:
        parsed = urlsplit(self.path)
        return parsed.path.rstrip("/") or "/"

    def _query_params(self) -> dict[str, list[str]]:
        return parse_qs(urlsplit(self.path).query, keep_blank_values=False)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send_json(200, {"ok": True})

    def do_GET(self) -> None:  # noqa: N802
        path = self._normalized_path()

        if path == "/health":
            backend_name = type(getattr(kb_store, "_backend", object())).__name__
            self._send_json(
                200,
                {
                    "ok": True,
                    "service": "DiabetesAgent API",
                    "model": _SERVER_DEFAULT_MODEL or MODEL_NAME,
                    "llm_base_url": LM_STUDIO_BASE_URL,
                    "llm_connectivity": _llm_connectivity_status(),
                    "kb_backend": backend_name,
                    "cgm_reading_count": cgm_store.count_readings(),
                    "cgm_sync_enabled": CGM_SYNC_ENABLED,
                    "cgm_sync_interval_minutes": CGM_SYNC_INTERVAL_MINUTES,
                    "cgm_enrichment_poll_minutes": CGM_ENRICHMENT_POLL_MINUTES,
                    "cgm_worker_status": _worker_status_payload(),
                },
            )
            return

        if path == "/api/cgm/sync-status":
            self._send_json(
                200,
                {
                    "ok": True,
                    "status": cgm_store.get_sync_status(),
                    "worker": _worker_status_payload(),
                },
            )
            return

        if path == "/api/cgm/worker-status":
            self._send_json(200, {"ok": True, "status": _worker_status_payload()})
            return

        if path == "/api/cgm/readings":
            query = self._query_params()
            start_values = query.get("start", [])
            end_values = query.get("end", [])
            limit_values = query.get("limit", [])

            if not start_values or not end_values:
                self._send_json(
                    400,
                    {
                        "ok": False,
                        "error": "`start` and `end` query params are required (ISO datetime).",
                    },
                )
                return

            try:
                start_iso = start_values[0]
                end_iso = end_values[0]
                limit = int(limit_values[0]) if limit_values else 500
                rows = cgm_store.get_readings(start_iso=start_iso, end_iso=end_iso, limit=limit)
            except Exception as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return

            self._send_json(200, {"ok": True, "count": len(rows), "readings": rows})
            return

        if path == "/api/reports":
            query = self._query_params()
            limit_values = query.get("limit", [])
            limit = 20
            if limit_values:
                try:
                    limit = int(limit_values[0])
                except ValueError:
                    self._send_json(400, {"ok": False, "error": "`limit` must be an integer"})
                    return
                if limit <= 0:
                    self._send_json(400, {"ok": False, "error": "`limit` must be a positive integer"})
                    return

            report_paths = _sorted_report_paths(limit=limit)
            reports = [_report_metadata(report_path) for report_path in report_paths]
            self._send_json(200, {"ok": True, "reports": reports, "count": len(reports)})
            return

        if path == "/api/reports/latest":
            query = self._query_params()
            include_content_values = query.get("include_content", [])
            max_chars_values = query.get("max_chars", [])

            include_content = False
            if include_content_values:
                try:
                    include_content = _query_bool_value(
                        include_content_values[0], field_name="include_content"
                    )
                except ValueError as exc:
                    self._send_json(400, {"ok": False, "error": str(exc)})
                    return

            max_chars: int | None = None
            if max_chars_values:
                try:
                    max_chars = int(max_chars_values[0])
                except ValueError:
                    self._send_json(400, {"ok": False, "error": "`max_chars` must be an integer"})
                    return
                if max_chars <= 0:
                    self._send_json(
                        400,
                        {"ok": False, "error": "`max_chars` must be a positive integer"},
                    )
                    return

            report_paths = _sorted_report_paths(limit=1)
            if not report_paths:
                self._send_json(404, {"ok": False, "error": "No reports available"})
                return

            latest = report_paths[0]
            response: dict[str, Any] = {"ok": True, "report": _report_metadata(latest)}
            if include_content:
                content, truncated = _read_report_content(latest, max_chars=max_chars)
                response["report_content"] = content
                response["report_content_truncated"] = truncated
            self._send_json(200, response)
            return

        if path.startswith("/api/reports/"):
            query = self._query_params()
            include_content_values = query.get("include_content", [])
            max_chars_values = query.get("max_chars", [])

            include_content = False
            if include_content_values:
                try:
                    include_content = _query_bool_value(
                        include_content_values[0], field_name="include_content"
                    )
                except ValueError as exc:
                    self._send_json(400, {"ok": False, "error": str(exc)})
                    return

            max_chars: int | None = None
            if max_chars_values:
                try:
                    max_chars = int(max_chars_values[0])
                except ValueError:
                    self._send_json(400, {"ok": False, "error": "`max_chars` must be an integer"})
                    return
                if max_chars <= 0:
                    self._send_json(
                        400,
                        {"ok": False, "error": "`max_chars` must be a positive integer"},
                    )
                    return

            file_name = unquote(path.removeprefix("/api/reports/")).strip()
            report_path = _resolve_report_path(file_name)
            if report_path is None:
                self._send_json(404, {"ok": False, "error": f"Unknown report: {file_name}"})
                return

            response = {"ok": True, "report": _report_metadata(report_path)}
            if include_content:
                content, truncated = _read_report_content(report_path, max_chars=max_chars)
                response["report_content"] = content
                response["report_content_truncated"] = truncated
            self._send_json(200, response)
            return

        if path == "/api/sessions":
            with _SESSIONS_LOCK:
                session_ids = sorted(_SESSIONS.keys())
            self._send_json(200, {"ok": True, "sessions": session_ids, "count": len(session_ids)})
            return

        if path.startswith("/api/sessions/"):
            session_id = path.removeprefix("/api/sessions/").strip()
            if not session_id:
                self._send_json(400, {"ok": False, "error": "Session id is required"})
                return

            with _SESSIONS_LOCK:
                messages = _SESSIONS.get(session_id)

            if messages is None:
                self._send_json(404, {"ok": False, "error": f"Unknown session: {session_id}"})
                return

            self._send_json(
                200,
                {
                    "ok": True,
                    "session_id": session_id,
                    "messages": messages,
                    "message_count": len(messages),
                },
            )
            return

        self._send_json(404, {"ok": False, "error": f"Unknown route: {path}"})

    def do_POST(self) -> None:  # noqa: N802
        path = self._normalized_path()

        try:
            payload = self._read_json_body()

            if path == "/api/chat":
                message = payload.get("message")
                if not isinstance(message, str) or not message.strip():
                    raise ValueError("`message` (non-empty string) is required")

                provided_session = payload.get("session_id")
                if provided_session is None:
                    session_id = str(uuid.uuid4())
                    new_session = True
                elif isinstance(provided_session, str) and provided_session.strip():
                    session_id = provided_session
                    new_session = False
                else:
                    raise ValueError("`session_id` must be a non-empty string when provided")

                model_name = payload.get("model")
                if model_name is not None and not isinstance(model_name, str):
                    raise ValueError("`model` must be a string when provided")

                reset_session = bool(payload.get("reset_session", False))
                cleaned_message = message.strip()

                if cleaned_message.lower() == "/help":
                    response_text = _chat_help_text()
                    _append_session_turn(
                        session_id=session_id,
                        reset_session=reset_session,
                        user_message=cleaned_message,
                        assistant_message=response_text,
                    )
                    result = {
                        "session_id": session_id,
                        "model": model_name or _SERVER_DEFAULT_MODEL or MODEL_NAME,
                        "response": response_text,
                        "tool_calls": [],
                        "metrics": _to_metrics_dict(
                            LLMRunMetrics(
                                latency_seconds=0.0,
                                time_to_first_token_seconds=None,
                                input_tokens=None,
                                output_tokens=None,
                                total_tokens=None,
                            )
                        ),
                        "command": "help",
                    }
                    result["new_session"] = new_session
                    self._send_json(200, {"ok": True, **result})
                    return

                result = _invoke_agent(
                    message=cleaned_message,
                    session_id=session_id,
                    model_name=model_name,
                    reset_session=reset_session,
                )
                result["new_session"] = new_session
                self._send_json(200, {"ok": True, **result})
                return

            if path == "/api/report":
                days = payload.get("days", 7)
                if not isinstance(days, int) or days <= 0:
                    raise ValueError("`days` must be a positive integer")

                include_content = payload.get("include_content", False)
                if not isinstance(include_content, bool):
                    raise ValueError("`include_content` must be a boolean when provided")

                max_chars = payload.get("max_chars")
                if max_chars is not None and (not isinstance(max_chars, int) or max_chars <= 0):
                    raise ValueError("`max_chars` must be a positive integer when provided")

                report_path = generate_and_save_report(period_days=days)
                response: dict[str, Any] = {"ok": True, "report_path": report_path, "days": days}

                if include_content:
                    report_file = Path(report_path)
                    report_content, truncated = _read_report_content(report_file, max_chars=max_chars)
                    response["report_content"] = report_content
                    response["report_content_truncated"] = truncated

                self._send_json(200, response)
                return

            if path == "/api/cgm/sync":
                mode = payload.get("mode", "latest")
                if not isinstance(mode, str):
                    raise ValueError("`mode` must be a string when provided")

                normalized_mode = mode.strip().lower()
                if normalized_mode not in {"latest", "full"}:
                    raise ValueError("`mode` must be 'latest' or 'full'")

                if normalized_mode == "full":
                    days = payload.get("days", CGM_SYNC_BACKFILL_DAYS)
                    if not isinstance(days, int) or days <= 0:
                        raise ValueError("`days` must be a positive integer")
                    result_text = _run_cgm_sync(days)
                else:
                    result_text = _run_cgm_latest_sync()

                self._send_json(
                    200,
                    {
                        "ok": True,
                        "mode": normalized_mode,
                        "result": result_text,
                        "status": cgm_store.get_sync_status(),
                    },
                )
                return

            if path == "/api/cgm/enrichment/run":
                limit = payload.get("limit", 50)
                if not isinstance(limit, int) or limit <= 0:
                    raise ValueError("`limit` must be a positive integer")
                result_text = _run_deferred_enrichment(limit=limit)
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "result": result_text,
                        "status": cgm_store.get_sync_status(),
                    },
                )
                return

            if path == "/api/kb/search/reports":
                query = payload.get("query")
                if not isinstance(query, str) or not query.strip():
                    raise ValueError("`query` (non-empty string) is required")
                since_days = payload.get("since_days", 180)
                top_k = payload.get("top_k", 5)
                if not isinstance(since_days, int) or since_days <= 0:
                    raise ValueError("`since_days` must be a positive integer")
                if not isinstance(top_k, int) or top_k <= 0:
                    raise ValueError("`top_k` must be a positive integer")

                rows = kb_store.search_reports(query=query, since_days=since_days, top_k=top_k)
                self._send_json(200, {"ok": True, "matches": rows, "count": len(rows)})
                return

            if path == "/api/kb/search/glucose":
                query = payload.get("query")
                if not isinstance(query, str) or not query.strip():
                    raise ValueError("`query` (non-empty string) is required")
                since_days = payload.get("since_days", 30)
                top_k = payload.get("top_k", 10)
                if not isinstance(since_days, int) or since_days <= 0:
                    raise ValueError("`since_days` must be a positive integer")
                if not isinstance(top_k, int) or top_k <= 0:
                    raise ValueError("`top_k` must be a positive integer")

                rows = kb_store.search_glucose(query=query, since_days=since_days, top_k=top_k)
                self._send_json(200, {"ok": True, "matches": rows, "count": len(rows)})
                return

            if path == "/api/kb/search/logs":
                query = payload.get("query")
                if not isinstance(query, str) or not query.strip():
                    raise ValueError("`query` (non-empty string) is required")

                since_days = payload.get("since_days", 30)
                top_k = payload.get("top_k", 10)
                event_types = payload.get("event_types", ["meal", "insulin"])

                if not isinstance(since_days, int) or since_days <= 0:
                    raise ValueError("`since_days` must be a positive integer")
                if not isinstance(top_k, int) or top_k <= 0:
                    raise ValueError("`top_k` must be a positive integer")
                if not isinstance(event_types, list) or not all(isinstance(t, str) for t in event_types):
                    raise ValueError("`event_types` must be a list of strings")

                rows = kb_store.search_logs(
                    query=query,
                    since_days=since_days,
                    event_types=event_types,
                    top_k=top_k,
                )
                self._send_json(200, {"ok": True, "matches": rows, "count": len(rows)})
                return

            if path == "/api/kb/log/glucose":
                value = payload.get("value")
                if not isinstance(value, (int, float)):
                    raise ValueError("`value` must be numeric (mmol/L)")

                timestamp = payload.get("timestamp")
                source = payload.get("source", "manual")
                if timestamp is not None and not isinstance(timestamp, str):
                    raise ValueError("`timestamp` must be an ISO datetime string when provided")
                if not isinstance(source, str) or not source.strip():
                    raise ValueError("`source` must be a non-empty string")

                point_id = kb_store.add_glucose_event(
                    timestamp=timestamp,
                    glucose_mmol_l=float(value),
                    source=source.strip(),
                )
                self._send_json(200, {"ok": True, "point_id": point_id})
                return

            if path == "/api/kb/log/meal":
                carbs_g = payload.get("carbs_g")
                meal_type = payload.get("meal_type")
                timestamp = payload.get("timestamp")
                notes = payload.get("notes", "")

                if not isinstance(carbs_g, (int, float)):
                    raise ValueError("`carbs_g` must be numeric")
                if not isinstance(meal_type, str) or not meal_type.strip():
                    raise ValueError("`meal_type` must be a non-empty string")
                if timestamp is not None and not isinstance(timestamp, str):
                    raise ValueError("`timestamp` must be an ISO datetime string when provided")
                if not isinstance(notes, str):
                    raise ValueError("`notes` must be a string")

                iso_timestamp = coerce_iso(timestamp)
                context_note, available = _cgm_context_note_for_timestamp(iso_timestamp)
                enriched = _enriched_notes(notes, context_note)

                point_id = kb_store.add_meal_event(
                    timestamp=iso_timestamp,
                    carbs_g=float(carbs_g),
                    meal_type=meal_type.strip(),
                    notes=enriched,
                )
                if not available:
                    cgm_store.enqueue_log_enrichment(
                        event_type="meal",
                        point_id=point_id,
                        timestamp_iso=iso_timestamp,
                        base_notes=notes,
                        window_minutes=CGM_LOG_ENRICHMENT_WINDOW_MINUTES,
                    )
                self._send_json(200, {"ok": True, "point_id": point_id, "notes": enriched})
                return

            if path == "/api/kb/log/insulin":
                units = payload.get("units")
                insulin_type = payload.get("insulin_type")
                timing_tag = payload.get("timing_tag")
                timestamp = payload.get("timestamp")
                notes = payload.get("notes", "")

                if not isinstance(units, (int, float)):
                    raise ValueError("`units` must be numeric")
                if not isinstance(insulin_type, str) or not insulin_type.strip():
                    raise ValueError("`insulin_type` must be a non-empty string")
                if not isinstance(timing_tag, str) or not timing_tag.strip():
                    raise ValueError("`timing_tag` must be a non-empty string")
                if timestamp is not None and not isinstance(timestamp, str):
                    raise ValueError("`timestamp` must be an ISO datetime string when provided")
                if not isinstance(notes, str):
                    raise ValueError("`notes` must be a string")

                normalized_type = insulin_type.strip().lower()
                if normalized_type not in VALID_INSULIN_TYPES:
                    raise ValueError(
                        f"`insulin_type` must be one of: {', '.join(sorted(VALID_INSULIN_TYPES))}"
                    )

                iso_timestamp = coerce_iso(timestamp)
                context_note, available = _cgm_context_note_for_timestamp(iso_timestamp)
                enriched = _enriched_notes(notes, context_note)

                point_id = kb_store.add_insulin_event(
                    timestamp=iso_timestamp,
                    units=float(units),
                    insulin_type=normalized_type,
                    timing_tag=timing_tag.strip(),
                    notes=enriched,
                )
                if not available:
                    cgm_store.enqueue_log_enrichment(
                        event_type="insulin",
                        point_id=point_id,
                        timestamp_iso=iso_timestamp,
                        base_notes=notes,
                        window_minutes=CGM_LOG_ENRICHMENT_WINDOW_MINUTES,
                    )
                self._send_json(200, {"ok": True, "point_id": point_id, "notes": enriched})
                return

            if path == "/api/kb/sync-glucose":
                days = payload.get("days", 7)
                if not isinstance(days, int) or days <= 0:
                    raise ValueError("`days` must be a positive integer")
                result_text = sync_glucose_to_kb.invoke({"days": days})
                self._send_json(200, {"ok": True, "result": result_text})
                return

            if path == "/api/kb/icr-summary":
                days = payload.get("days", 14)
                pairing_window_minutes = payload.get("pairing_window_minutes", 60)
                if not isinstance(days, int) or days <= 0:
                    raise ValueError("`days` must be a positive integer")
                if not isinstance(pairing_window_minutes, int) or pairing_window_minutes <= 0:
                    raise ValueError("`pairing_window_minutes` must be a positive integer")

                result_text = get_icr_summary.invoke(
                    {"days": days, "pairing_window_minutes": pairing_window_minutes}
                )
                self._send_json(200, {"ok": True, "result": result_text})
                return

            self._send_json(404, {"ok": False, "error": f"Unknown route: {path}"})

        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive runtime guard
            self._send_json(
                500,
                {
                    "ok": False,
                    "error": "Internal server error",
                    "detail": str(exc),
                    "traceback": traceback.format_exc(),
                },
            )

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep logs concise and easy to grep
        print(f"[HTTP] {self.address_string()} - {fmt % args}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run DiabetesAgent HTTP API server")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Bind host (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Bind port (default: {DEFAULT_PORT})")
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "Default model override for /api/chat when request does not provide `model` "
            f"(default config model: {MODEL_NAME})"
        ),
    )
    parser.add_argument(
        "--cors-origin",
        default=DEFAULT_CORS_ORIGIN,
        help=(
            "CORS Access-Control-Allow-Origin value for browser fetch clients "
            f"(default: {DEFAULT_CORS_ORIGIN})"
        ),
    )
    return parser.parse_args()


def main() -> None:
    global _SERVER_DEFAULT_MODEL
    global _SERVER_CORS_ORIGIN

    args = _parse_args()
    _SERVER_DEFAULT_MODEL = args.model
    _SERVER_CORS_ORIGIN = args.cors_origin

    # Warm default graph at startup for faster first request.
    _get_graph(model_name=_SERVER_DEFAULT_MODEL)
    _start_cgm_sync_worker()
    _start_cgm_enrichment_worker()

    server = ThreadingHTTPServer((args.host, args.port), DiabetesAgentHTTPRequestHandler)

    print("DiabetesAgent API server started")
    print(f"  URL        : http://{args.host}:{args.port}")
    print(f"  Model      : {_SERVER_DEFAULT_MODEL or MODEL_NAME}")
    print(f"  CORS origin: {_SERVER_CORS_ORIGIN}")
    print("  Endpoints  :")
    print("    GET  /health")
    print("    GET  /api/cgm/sync-status")
    print("    GET  /api/cgm/worker-status")
    print("    GET  /api/cgm/readings?start=<iso>&end=<iso>&limit=500")
    print("    GET  /api/reports?limit=20")
    print("    GET  /api/reports/latest?include_content=true")
    print("    GET  /api/reports/<file_name>?include_content=true")
    print("    GET  /api/sessions")
    print("    GET  /api/sessions/<session_id>")
    print("    POST /api/chat  (supports /help command)")
    print("    POST /api/report  (supports include_content=true)")
    print("    POST /api/cgm/sync")
    print("    POST /api/cgm/enrichment/run")
    print("    POST /api/kb/search/reports")
    print("    POST /api/kb/search/glucose")
    print("    POST /api/kb/search/logs")
    print("    POST /api/kb/log/glucose")
    print("    POST /api/kb/log/meal")
    print("    POST /api/kb/log/insulin")
    print("    POST /api/kb/sync-glucose")
    print("    POST /api/kb/icr-summary")
    print("Press Ctrl-C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
    finally:
        _stop_cgm_sync_worker()
        _stop_cgm_enrichment_worker()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
