import importlib.util
from pathlib import Path
import sys
import types
import unittest


package = types.ModuleType("gemini_live_runtime_package")
package.__path__ = [
    str(
        Path(__file__).resolve().parents[1]
        / "experimental"
        / "gemini_live"
    )
]
sys.modules.setdefault("gemini_live_runtime_package", package)

homeassistant = types.ModuleType("homeassistant")
core = types.ModuleType("homeassistant.core")
core.HomeAssistant = type("HomeAssistant", (), {})
homeassistant.core = core
sys.modules.setdefault("homeassistant", homeassistant)
sys.modules.setdefault("homeassistant.core", core)

runtime_path = (
    Path(__file__).resolve().parents[1]
    / "experimental"
    / "gemini_live"
    / "runtime.py"
)
spec = importlib.util.spec_from_file_location(
    "gemini_live_runtime_package.runtime",
    runtime_path,
)
runtime = importlib.util.module_from_spec(spec)
assert spec is not None and spec.loader is not None
sys.modules[spec.name] = runtime
spec.loader.exec_module(runtime)


class TurnStoreStreamingAudioTest(unittest.TestCase):
    def test_unique_placeholder_matches_without_transcript(self) -> None:
        store = runtime.TurnStore()
        text = runtime.TextStream()
        audio = runtime.AudioStream()
        marker = "-- gemini live -- 6a738153"

        store.add_streaming_audio(marker, text, audio)

        self.assertIs(store.take_streaming_audio(marker), audio)
        self.assertIsNone(store.take_streaming_audio(marker))

    def test_transcript_prefix_still_matches(self) -> None:
        store = runtime.TurnStore()
        text = runtime.TextStream()
        text.add_chunk("Точный голосовой ответ")
        audio = runtime.AudioStream()

        store.add_streaming_audio("-- gemini live -- another", text, audio)

        self.assertIs(store.take_streaming_audio("Точный голосовой"), audio)

    def test_buffered_audio_keeps_trace_for_tts_boundary(self) -> None:
        store = runtime.TurnStore()
        trace = object()
        store.add_audio("Локальный ответ", b"RIFFaudio", trace)
        entry = store.take_audio_entry("Локальный ответ")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.audio, b"RIFFaudio")
        self.assertIs(entry.trace, trace)

    def test_trace_handoff_preserves_original_foreign_text(self) -> None:
        store = runtime.TurnStore()
        trace = object()
        store.add_trace("cid", "Arbeitet ihr jetzt?", trace)
        self.assertIs(store.take_trace("cid", "Arbeitet ihr jetzt?"), trace)
        self.assertIsNone(store.take_trace("cid", "Arbeitet ihr jetzt?"))


if __name__ == "__main__":
    unittest.main()
