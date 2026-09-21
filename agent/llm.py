# agent/llm.py
# Factory for the ChatOpenAI client pointed at LM Studio (or Ollama),
# or at the remote OpenCode Zen endpoint when provider="zen".
import os
import uuid

from pydantic import SecretStr
from langchain_openai import ChatOpenAI

from config import (
    LM_STUDIO_API_KEY,
    LM_STUDIO_BASE_URL,
    MODEL_NAME,
    OPENCODE_ZEN_API_KEY,
    OPENCODE_ZEN_BASE_URL,
    ZEN_DEFAULT_MODEL,
    ZEN_MODELS,
    TEMPERATURE,
)

# Aliases accepted for the remote OpenCode Zen provider.
_ZEN_ALIASES = {"zen", "opencode", "opencode-zen", "opencode_zen"}

# OpenCode Zen rejects requests without an x-opencode-session header
# (400 MissingSessionID) and uses it to route and to reuse the prompt cache. The
# judge and user simulator build a fresh client per call, so the ID has to be
# stable for the whole process rather than per client — one benchmark run is one
# session. Override with OPENCODE_SESSION_ID to share a session across processes.
_ZEN_SESSION_ID = os.environ.get("OPENCODE_SESSION_ID") or uuid.uuid4().hex


def zen_session_id() -> str:
    """The session ID sent as x-opencode-session for this process."""
    return _ZEN_SESSION_ID


def get_llm(
    model_name: str | None = None,
    timeout: int = 300,
    provider: str | None = None,
    max_retries: int = 2,
    max_tokens: int | None = None,
    session_id: str | None = None,
) -> ChatOpenAI:
    """Build a ChatOpenAI client for the selected provider.

    provider:
        None / "local" / "lm-studio" -> local LM Studio server (default)
        "zen" / "opencode"           -> remote OpenCode Zen endpoint

    When targeting the zen provider, ``model_name`` (if given) wins; otherwise the
    ZEN_DEFAULT_MODEL alias is resolved to its id so that a local model name like
    "qwen3.6-35b-a3b" isn't accidentally sent to the remote endpoint. A model_name
    passed as a short alias ("deepseek"/"kimi") is also resolved to its full id.
    """
    provider_key = (provider or "local").strip().lower()
    headers = None

    if provider_key in _ZEN_ALIASES:
        base_url = OPENCODE_ZEN_BASE_URL
        api_key = OPENCODE_ZEN_API_KEY
        # Resolve short aliases; pass any other explicit id through unchanged.
        model = ZEN_MODELS.get(
            model_name or ZEN_DEFAULT_MODEL, model_name or ZEN_MODELS[ZEN_DEFAULT_MODEL]
        )
        headers = {"x-opencode-session": session_id or _ZEN_SESSION_ID}
    else:
        base_url = LM_STUDIO_BASE_URL
        api_key = LM_STUDIO_API_KEY
        model = model_name or MODEL_NAME

    return ChatOpenAI(
        base_url=base_url,
        api_key=SecretStr(api_key),
        model=model,
        temperature=TEMPERATURE,
        timeout=timeout,
        max_retries=max_retries,
        # Caps total completion tokens (reasoning + answer). None = no cap.
        # For reasoning models this bounds runaway chain-of-thought.
        max_tokens=max_tokens,
        default_headers=headers,
    )
