from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "experimental" / "gemini_live" / "observer.py"
spec = importlib.util.spec_from_file_location("gemini_live_observer_test", MODULE_PATH)
assert spec is not None and spec.loader is not None
observer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = observer
spec.loader.exec_module(observer)


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
    def async_fire(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))


class FakeStore:
    def __init__(self) -> None:
        self.saved: list[dict] = []
    def async_delay_save(self, callback, _delay: float) -> None:
        self.saved.append(callback())


class GeminiObserverEventTests(unittest.TestCase):
    def make_trace(self, *, include_text: bool):
        bus = FakeBus()
        hass = types.SimpleNamespace(bus=bus, data={})
        trace = observer.TurnTrace(hass, "entry", "conversation", "trace123", include_text=include_text, started_at=100.0)
        return trace, bus, hass

    def test_default_event_is_privacy_safe(self) -> None:
        trace, bus, _hass = self.make_trace(include_text=False)
        trace.emit("final_transcript", text="Привет.", action="accept")
        event_type, payload = bus.events[0]
        self.assertEqual(event_type, observer.EVENT_GEMINI_LIVE_TURN)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["source"], "p610")
        self.assertEqual(payload["trace_id"], "trace123")
        self.assertEqual(payload["text_chars"], 7)
        self.assertIn("timestamp", payload)
        self.assertNotIn("text", payload)
        self.assertNotIn("audio", payload)
        self.assertNotIn("tool_args", payload)

    def test_explicit_debug_event_contains_current_turn_text(self) -> None:
        trace, bus, _hass = self.make_trace(include_text=True)
        trace.emit("final_transcript", text="Привет.")
        self.assertEqual(bus.events[0][1]["text"], "Привет.")

    def test_sequence_is_monotonic_across_phases(self) -> None:
        trace, bus, _hass = self.make_trace(include_text=False)
        for stage in ("audio_stream_end", "gate", "text_send"):
            trace.emit(stage)
        self.assertEqual([event[1]["sequence"] for event in bus.events], [1, 2, 3])

    def test_reserved_fields_cannot_be_overridden(self) -> None:
        trace, bus, _hass = self.make_trace(include_text=False)
        trace.emit("gate", source="evil", trace_id="wrong", schema_version=999)
        payload = bus.events[0][1]
        self.assertEqual(payload["source"], "p610")
        self.assertEqual(payload["trace_id"], "trace123")
        self.assertEqual(payload["schema_version"], 1)

    def test_persistent_history_groups_dialog_and_terminal_state(self) -> None:
        store = FakeStore()
        history = observer.ObserverHistory(types.SimpleNamespace(), store)
        base = {"schema_version": 1, "source": "p610", "conversation_id": "c1", "timestamp": "2026-08-11T07:00:00.000Z"}
        history.record({**base, "trace_id": "t1", "sequence": 1, "stage": "stt_start", "elapsed_ms": 0})
        history.record({**base, "trace_id": "t1", "sequence": 2, "stage": "final_transcript", "elapsed_ms": 100, "text": "Какая погода?"})
        history.record({**base, "trace_id": "t1", "sequence": 3, "stage": "assistant_delta", "elapsed_ms": 200, "text": "Сейчас 21 градус."})
        history.record({**base, "trace_id": "t2", "sequence": 1, "stage": "stt_start", "elapsed_ms": 0})
        history.record({**base, "trace_id": "t2", "sequence": 2, "stage": "stt_failed", "elapsed_ms": 31000, "reason": "no_direct_live_response_audio"})
        turns = history.snapshot()
        self.assertTrue(turns[0]["new_dialog"])
        self.assertFalse(turns[1]["new_dialog"])
        self.assertEqual(turns[0]["user_text"], "Какая погода?")
        self.assertEqual(turns[0]["assistant_text"], "Сейчас 21 градус.")
        self.assertTrue(turns[1]["dialog_ended"])
        self.assertEqual(turns[1]["status"], "failed")
        self.assertTrue(store.saved)

    def test_loaded_history_prevents_false_new_dialog_marker(self) -> None:
        loaded = {"turns": [{"trace_id": "old", "conversation_id": "c1", "phases": {}, "tool_names": []}]}
        history = observer.ObserverHistory(types.SimpleNamespace(), None, loaded)
        history.record({"schema_version": 1, "source": "p610", "conversation_id": "c1", "trace_id": "new", "sequence": 1, "stage": "stt_start", "elapsed_ms": 0})
        self.assertFalse(history.snapshot()[-1]["new_dialog"])


if __name__ == "__main__": unittest.main()
