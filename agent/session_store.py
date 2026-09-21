"""Helpers for loading/saving persisted chat sessions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from config import CHAT_SESSIONS_PATH

_ALLOWED_ROLES = {"user", "assistant", "system", "tool"}


def _sanitize_messages(raw_messages: Any) -> list[dict[str, str]]:
    if not isinstance(raw_messages, list):
        return []

    messages: list[dict[str, str]] = []
    for item in raw_messages:
        if not isinstance(item, dict):
            continue

        role = item.get("role")
        content = item.get("content")
        if not isinstance(role, str) or not isinstance(content, str):
            continue
        if role not in _ALLOWED_ROLES:
            continue

        messages.append({"role": role, "content": content})

    return messages


def load_sessions(path: str = CHAT_SESSIONS_PATH) -> dict[str, list[dict[str, str]]]:
    file_path = Path(path)
    if not file_path.exists():
        return {}

    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(data, dict):
        return {}

    sessions: dict[str, list[dict[str, str]]] = {}
    for session_id, raw_messages in data.items():
        if not isinstance(session_id, str) or not session_id.strip():
            continue
        sessions[session_id] = _sanitize_messages(raw_messages)

    return sessions


def save_sessions(sessions: dict[str, list[dict[str, str]]], path: str = CHAT_SESSIONS_PATH) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    sanitized: dict[str, list[dict[str, str]]] = {}
    for session_id, raw_messages in sessions.items():
        if not isinstance(session_id, str) or not session_id.strip():
            continue
        sanitized[session_id] = _sanitize_messages(raw_messages)

    payload = json.dumps(sanitized, ensure_ascii=False, indent=2)
    tmp_path = file_path.with_suffix(f"{file_path.suffix}.tmp")
    tmp_path.write_text(payload, encoding="utf-8")
    tmp_path.replace(file_path)
