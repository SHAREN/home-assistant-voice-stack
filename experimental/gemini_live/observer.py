"""Live and persistent observer events for the physical P610 pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import time
from typing import Any


EVENT_GEMINI_LIVE_TURN = "gemini_live_turn_event"
OBSERVER_SCHEMA_VERSION = 1
HISTORY_SCHEMA_VERSION = 1
HISTORY_STORAGE_VERSION = 1
HISTORY_STORAGE_KEY = "gemini_live.p610_conversation_history"
HISTORY_DATA_KEY = "gemini_live_p610_conversation_history"
HISTORY_WS_COMMAND = "gemini_live/p610_history"
MAX_HISTORY_TURNS = 500
_LOGGER = logging.getLogger(__name__)


def _utc_timestamp() -> str:
    """Return a compact wall-clock timestamp for UI/history correlation."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _copy_turn(turn: dict[str, Any]) -> dict[str, Any]:
    """Copy one JSON-serializable history record."""
    copied = dict(turn)
    copied["phases"] = dict(turn.get("phases") or {})
    copied["tool_names"] = list(turn.get("tool_names") or [])
    return copied


class ObserverHistory:
    """Bounded local transcript history backed by Home Assistant Store."""

    def __init__(
        self,
        hass: Any,
        store: Any | None,
        loaded: dict[str, Any] | None = None,
    ) -> None:
        self.hass = hass
        self._store = store
        loaded_turns = (loaded or {}).get("turns") or []
        self._order: list[str] = []
        self._turns: dict[str, dict[str, Any]] = {}
        self._seen_conversations: set[str] = set()
        for raw in loaded_turns[-MAX_HISTORY_TURNS:]:
            if not isinstance(raw, dict):
                continue
            trace_id = str(raw.get("trace_id") or "")
            if not trace_id:
                continue
            record = _copy_turn(raw)
            self._order.append(trace_id)
            self._turns[trace_id] = record
            conversation_id = str(record.get("conversation_id") or "")
            if conversation_id:
                self._seen_conversations.add(conversation_id)

    def _data_to_save(self) -> dict[str, Any]:
        return {
            "schema_version": HISTORY_SCHEMA_VERSION,
            "turns": [_copy_turn(self._turns[trace]) for trace in self._order],
        }

    def snapshot(self, limit: int = MAX_HISTORY_TURNS) -> list[dict[str, Any]]:
        """Return chronological persistent turns for the authenticated UI."""
        safe_limit = max(1, min(int(limit), MAX_HISTORY_TURNS))
        return [
            _copy_turn(self._turns[trace])
            for trace in self._order[-safe_limit:]
        ]

    def record(self, payload: dict[str, Any]) -> None:
        """Reduce one observer event into a durable conversation turn."""
        if payload.get("schema_version") != OBSERVER_SCHEMA_VERSION:
            return
        if payload.get("source") != "p610":
            return
        trace_id = str(payload.get("trace_id") or "")
        conversation_id = str(payload.get("conversation_id") or "")
        sequence = int(payload.get("sequence") or 0)
        if not trace_id or sequence <= 0:
            return

        record = self._turns.get(trace_id)
        if record is not None and sequence <= int(record.get("last_sequence") or 0):
            return

        timestamp = str(payload.get("timestamp") or _utc_timestamp())
        if record is None:
            is_new_dialog = bool(
                conversation_id and conversation_id not in self._seen_conversations
            )
            record = {
                "trace_id": trace_id,
                "conversation_id": conversation_id,
                "started_at": timestamp,
                "updated_at": timestamp,
                "last_sequence": 0,
                "user_text": "",
                "partial_text": "",
                "assistant_text": "",
                "new_dialog": is_new_dialog,
                "dialog_ended": False,
                "end_reason": "",
                "status": "active",
                "continue_conversation": None,
                "tool_names": [],
                "phases": {},
            }
            self._turns[trace_id] = record
            self._order.append(trace_id)
            if conversation_id:
                self._seen_conversations.add(conversation_id)
            while len(self._order) > MAX_HISTORY_TURNS:
                removed = self._order.pop(0)
                self._turns.pop(removed, None)

        record["last_sequence"] = sequence
        record["updated_at"] = timestamp
        stage = str(payload.get("stage") or "")
        record["phases"][stage] = float(payload.get("elapsed_ms") or 0.0)

        text = payload.get("text")
        if stage in {"first_input_transcription", "last_transcript_update"}:
            if text:
                record["partial_text"] = str(text)
        elif stage == "final_transcript":
            if text:
                record["user_text"] = str(text)
                record["partial_text"] = ""
        elif stage == "assistant_delta":
            if text:
                record["assistant_text"] += str(text)
        elif stage in {"response_local", "conversation_result"}:
            if text and not str(text).startswith("-- gemini live --"):
                if not record["assistant_text"]:
                    record["assistant_text"] = str(text)

        if stage == "tool_call_boundary":
            tool_name = str(payload.get("tool_name") or "")
            if tool_name and tool_name not in record["tool_names"]:
                record["tool_names"].append(tool_name)

        if stage == "conversation_result":
            continue_conversation = bool(payload.get("continue_conversation"))
            record["continue_conversation"] = continue_conversation
            if not continue_conversation:
                record["dialog_ended"] = True
                record["status"] = "ended"
                record["end_reason"] = "conversation_result"
        elif stage == "stt_failed":
            record["dialog_ended"] = True
            record["status"] = "failed"
            record["end_reason"] = str(payload.get("reason") or "stt_failed")
        elif stage == "local_stop":
            record["dialog_ended"] = True
            record["status"] = "stopped"
            record["end_reason"] = "local_stop"
        elif stage == "direct_live_complete" and not record["dialog_ended"]:
            record["status"] = "complete"

        if self._store is not None:
            try:
                # Store coalesces bursts of streaming assistant_delta events.
                self._store.async_delay_save(self._data_to_save, 1.0)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("P610 history save scheduling failed", exc_info=True)


async def async_setup_observer_history(hass: Any) -> None:
    """Load local history and register an authenticated WebSocket read command."""
    if HISTORY_DATA_KEY in hass.data:
        return

    store = None
    loaded = None
    try:
        from homeassistant.helpers.storage import Store

        store = Store(
            hass,
            HISTORY_STORAGE_VERSION,
            HISTORY_STORAGE_KEY,
            private=True,
            atomic_writes=True,
        )
        loaded = await store.async_load()
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Could not load persistent P610 conversation history")

    history = ObserverHistory(hass, store, loaded)
    hass.data[HISTORY_DATA_KEY] = history

    try:
        import voluptuous as vol
        from homeassistant.components import websocket_api

        @websocket_api.websocket_command(
            {
                vol.Required("type"): HISTORY_WS_COMMAND,
                vol.Optional("limit", default=300): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=MAX_HISTORY_TURNS)
                ),
            }
        )
        @websocket_api.async_response
        async def websocket_get_p610_history(
            hass: Any, connection: Any, msg: dict[str, Any]
        ) -> None:
            current = hass.data.get(HISTORY_DATA_KEY)
            turns = current.snapshot(msg["limit"]) if current is not None else []
            connection.send_result(
                msg["id"],
                {
                    "schema_version": HISTORY_SCHEMA_VERSION,
                    "turns": turns,
                },
            )

        websocket_api.async_register_command(hass, websocket_get_p610_history)
    except Exception:  # noqa: BLE001
        # Live event subscription still works if the history endpoint cannot register.
        _LOGGER.exception("Could not register P610 history WebSocket command")


@dataclass(slots=True)
class TurnTrace:
    """Correlate one P610 pipeline turn and optionally expose local text."""

    hass: Any
    entry_id: str
    conversation_id: str
    trace_id: str
    include_text: bool = False
    started_at: float = field(default_factory=time.monotonic)
    _sequence: int = 0

    def emit(self, stage: str, *, text: str | None = None, **data: Any) -> None:
        """Emit a versioned HA event and update bounded local history."""
        self._sequence += 1
        reserved = {
            "schema_version",
            "source",
            "entry_id",
            "trace_id",
            "conversation_id",
            "sequence",
            "stage",
            "elapsed_ms",
            "timestamp",
            "text",
        }
        safe_data = {
            key: value
            for key, value in data.items()
            if key not in reserved
            and isinstance(value, (str, int, float, bool, type(None)))
        }
        payload: dict[str, Any] = {
            **safe_data,
            "schema_version": OBSERVER_SCHEMA_VERSION,
            "source": "p610",
            "entry_id": self.entry_id,
            "trace_id": self.trace_id,
            "conversation_id": self.conversation_id,
            "sequence": self._sequence,
            "stage": stage,
            "elapsed_ms": round((time.monotonic() - self.started_at) * 1000, 1),
            "timestamp": _utc_timestamp(),
        }
        if text is not None:
            payload["text_chars"] = len(text)
            if self.include_text:
                payload["text"] = text
        try:
            self.hass.bus.async_fire(EVENT_GEMINI_LIVE_TURN, payload)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("P610 observer event emission failed", exc_info=True)
        try:
            history = self.hass.data.get(HISTORY_DATA_KEY)
            if history is not None:
                history.record(payload)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("P610 history reduction failed", exc_info=True)
        _LOGGER.info(
            "[trace=%s conversation=%s] phase=%s sequence=%d elapsed_ms=%.1f",
            self.trace_id,
            self.conversation_id,
            stage,
            self._sequence,
            payload["elapsed_ms"],
        )
