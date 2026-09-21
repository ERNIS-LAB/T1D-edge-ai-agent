# config.py
# Central configuration for the DiabetesAgent.
# All other modules import from here — never hard-code URLs, model names, or
# credentials elsewhere.
#
# Every value below can be overridden with an environment variable of the same
# name. Copy `.env.example` to `.env` and fill in what you need; the file is
# git-ignored and loaded automatically when `python-dotenv` is installed.
#
# Secrets (API keys, LibreLinkUp login) intentionally default to empty. The
# features that need them fail with an explicit message rather than silently
# falling back, so a missing key is never mistaken for a broken feature.

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

try:  # optional dependency — config still works without it
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass


def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --- Agent LLM endpoint (any OpenAI-compatible server) ---------------------
# Defaults target LM Studio on localhost. Ollama, vLLM, llama.cpp's server, and
# the OpenAI API itself all work — point the base URL at the right host and set
# MODEL_NAME to an id that server actually serves. The URL must end at "/v1";
# the client appends "/chat/completions" itself.
#
#   LM Studio  http://127.0.0.1:1234/v1
#   Ollama     http://127.0.0.1:11434/v1
#   vLLM       http://127.0.0.1:8000/v1
LM_STUDIO_BASE_URL = _env_str("LM_STUDIO_BASE_URL", "http://127.0.0.1:1234/v1")
# Local servers ignore the key but the OpenAI client requires a non-empty value.
LM_STUDIO_API_KEY = _env_str("LM_STUDIO_API_KEY", "lm-studio")
MODEL_NAME = _env_str("MODEL_NAME", "qwen3.6-35b-a3b")
TEMPERATURE = _env_float("TEMPERATURE", 0.0)  # 0 = deterministic, best for tool-calling

# --- Secondary remote endpoint (OpenAI-compatible) -------------------------
# Used by the benchmark when a role (agent / judge / chat simulator) is pointed
# at the "zen" provider instead of the local server — typically to run a large
# judge model remotely while the agent under test runs locally.
OPENCODE_ZEN_BASE_URL = _env_str("OPENCODE_ZEN_BASE_URL", "https://opencode.ai/zen/go/v1")
OPENCODE_ZEN_API_KEY = _env_str("OPENCODE_ZEN_API_KEY")
# Short alias -> exact model id, selected via the benchmark's --zen-model flag.
ZEN_MODELS = {
    "deepseek": _env_str("ZEN_MODEL_DEEPSEEK", "deepseek-v4-pro"),
    "kimi": _env_str("ZEN_MODEL_KIMI", "kimi-k2.6"),
}
ZEN_DEFAULT_MODEL = _env_str("ZEN_DEFAULT_MODEL", "deepseek")

# --- LibreLinkUp (live CGM data source) ------------------------------------
# Only needed to sync real CGM readings from a LibreLink account. The benchmark
# and the OhioT1DM workflows do not use this.
LIBRE_EMAIL = _env_str("LIBRE_EMAIL")
LIBRE_PASSWORD = _env_str("LIBRE_PASSWORD")

# --- External search (optional) --------------------------------------------
TAVILY_API_KEY = _env_str("TAVILY_API_KEY")

# --- Spoonacular (food / nutrition API, optional) --------------------------
# The benchmark reads from a committed response cache, so food tasks run
# offline without a key. A key is only needed for live lookups.
SPOONACULAR_API_KEY = _env_str("SPOONACULAR_API_KEY")
SPOONACULAR_BASE_URL = _env_str("SPOONACULAR_BASE_URL", "https://api.spoonacular.com")

# --- Scheduled reporting ----------------------------------------------------
# Cron expression: "minute hour day-of-month month day-of-week".
REPORT_CRON = _env_str("REPORT_CRON", "0 9 * * MON")
REPORT_PERIOD_DAYS = _env_int("REPORT_PERIOD_DAYS", 7)
REPORTS_DIR = _env_str("REPORTS_DIR", "reports")  # created automatically
REPORT_LLM_TIMEOUT_SECONDS = _env_int("REPORT_LLM_TIMEOUT_SECONDS", 45)

# --- Knowledge base / RAG ---------------------------------------------------
KB_QDRANT_PATH = _env_str("KB_QDRANT_PATH", ".kb_data/qdrant")
CHAT_SESSIONS_PATH = _env_str("CHAT_SESSIONS_PATH", ".kb_data/chat_sessions.json")
# Local embedding model. If unavailable, the app falls back to deterministic
# lightweight embeddings so the system stays functional offline.
KB_EMBEDDING_MODEL = _env_str("KB_EMBEDDING_MODEL", "all-MiniLM-L6-v2")

# --- Insulin ratio persistence ---------------------------------------------
INSULIN_RATIOS_PATH = _env_str("INSULIN_RATIOS_PATH", ".kb_data/insulin_ratios.json")

# --- CGM SQLite time-series store ------------------------------------------
CGM_DB_PATH = _env_str("CGM_DB_PATH", ".kb_data/cgm.sqlite3")
# Requires LIBRE_EMAIL / LIBRE_PASSWORD; harmless to leave on without them.
CGM_SYNC_ENABLED = _env_bool("CGM_SYNC_ENABLED", True)
CGM_SYNC_INTERVAL_MINUTES = _env_int("CGM_SYNC_INTERVAL_MINUTES", 5)
CGM_SYNC_BACKFILL_DAYS = _env_int("CGM_SYNC_BACKFILL_DAYS", 14)
CGM_LOG_ENRICHMENT_WINDOW_MINUTES = _env_int("CGM_LOG_ENRICHMENT_WINDOW_MINUTES", 90)
CGM_ENRICHMENT_POLL_MINUTES = _env_int("CGM_ENRICHMENT_POLL_MINUTES", 10)
CGM_ENRICHMENT_MAX_ATTEMPTS = _env_int("CGM_ENRICHMENT_MAX_ATTEMPTS", 24)
