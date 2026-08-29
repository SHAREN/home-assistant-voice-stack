"""Safe short-term conversation memory for Pipecat Assist.

Only completed user/assistant text pairs are cached. Tool calls, tool results,
system/developer messages, empty messages, and trailing unanswered user turns
are deliberately excluded so reconnecting a realtime session can never replay
an old Home Assistant action.
"""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass
from typing import Any

from loguru import logger


@dataclass
class _MemoryEntry:
    cached_at: float
    messages: list[dict[str, str]]


def _role(message: Any) -> str:
    if isinstance(message, dict):
        value = message.get("role")
    else:
        value = getattr(message, "role", None)
    role = str(value or "").strip().lower()
    return "assistant" if role == "model" else role


def _parts(message: Any) -> list[Any]:
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list):
            return content
        value = message.get("parts")
    else:
        value = getattr(message, "parts", None)
    return list(value or []) if value is not None else []


def _text(message: Any) -> str:
    if isinstance(message, dict):
        content = message.get("content")
    else:
        content = getattr(message, "content", None)
    if isinstance(content, str):
        return content.strip()

    chunks: list[str] = []
    for part in _parts(message):
        if isinstance(part, dict):
            value = part.get("text")
        else:
            value = getattr(part, "text", None)
        if isinstance(value, str) and value.strip():
            chunks.append(value.strip())
    return " ".join(chunks).strip()


def _contains_tool_data(message: Any) -> bool:
    if isinstance(message, dict):
        if any(
            message.get(key)
            for key in (
                "tool_calls",
                "tool_call_id",
                "function_call",
                "function_response",
                "tool_response",
            )
        ):
            return True
    else:
        if any(
            getattr(message, key, None)
            for key in (
                "tool_calls",
                "tool_call_id",
                "function_call",
                "function_response",
                "tool_response",
            )
        ):
            return True

    for part in _parts(message):
        if isinstance(part, dict):
            if any(
                part.get(key)
                for key in (
                    "function_call",
                    "function_response",
                    "tool_call",
                    "tool_response",
                )
            ):
                return True
        elif any(
            getattr(part, key, None) is not None
            for key in (
                "function_call",
                "function_response",
                "tool_call",
                "tool_response",
            )
        ):
            return True
    return False


def safe_completed_text_pairs(messages: list[Any], max_messages: int) -> list[dict[str, str]]:
    """Return bounded, completed user/assistant text pairs only."""

    completed: list[dict[str, str]] = []
    pending_user: str | None = None

    for message in messages:
        if _contains_tool_data(message):
            continue
        role = _role(message)
        text = _text(message)
        if not text:
            continue
        if role == "user":
            # A newer user turn supersedes any older unanswered fragment.
            pending_user = text
            continue
        if role == "assistant" and pending_user is not None:
            completed.extend(
                [
                    {"role": "user", "content": pending_user},
                    {"role": "assistant", "content": text},
                ]
            )
            pending_user = None

    limit = max(0, int(max_messages or 0))
    if limit <= 0:
        return []
    # Preserve pair boundaries even when max_messages is odd.
    pair_limit = max(2, limit - (limit % 2))
    return completed[-pair_limit:]


class SafeSessionMemory:
    def __init__(self) -> None:
        self._entries: dict[str, _MemoryEntry] = {}
        self._lock = threading.RLock()

    def restore(
        self,
        client_id: str,
        base_messages: list[Any],
        *,
        enabled: bool,
        reuse_seconds: int,
        max_messages: int,
    ) -> list[Any]:
        base = copy.deepcopy(list(base_messages or []))
        if not enabled or not client_id:
            return base

        now = time.time()
        with self._lock:
            entry = self._entries.get(client_id)
            if not entry:
                return base
            if reuse_seconds > 0 and now - entry.cached_at > reuse_seconds:
                self._entries.pop(client_id, None)
                logger.info("Expired safe session memory for {}", client_id)
                return base
            restored = copy.deepcopy(entry.messages[-max(0, int(max_messages or 0)) :])

        if restored:
            logger.info(
                "Restored {} safe text memory messages for {} (tool calls excluded)",
                len(restored),
                client_id,
            )
        return base + restored

    def cache(
        self,
        client_id: str,
        context: Any,
        *,
        enabled: bool,
        max_messages: int,
    ) -> None:
        if not client_id:
            return
        if not enabled:
            with self._lock:
                self._entries.pop(client_id, None)
            return

        getter = getattr(context, "get_messages", None)
        if not callable(getter):
            return
        try:
            raw_messages = list(getter() or [])
        except Exception as err:
            logger.warning("Could not read context for safe session memory: {}", err)
            return

        safe_messages = safe_completed_text_pairs(raw_messages, max_messages)
        with self._lock:
            if safe_messages:
                self._entries[client_id] = _MemoryEntry(time.time(), safe_messages)
            else:
                self._entries.pop(client_id, None)
        logger.info(
            "Cached {} safe text memory messages for {} ({} raw messages inspected)",
            len(safe_messages),
            client_id,
            len(raw_messages),
        )

    def clear(self, client_id: str | None = None) -> None:
        with self._lock:
            if client_id:
                self._entries.pop(client_id, None)
            else:
                self._entries.clear()


SESSION_MEMORY = SafeSessionMemory()
