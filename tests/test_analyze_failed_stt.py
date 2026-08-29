from __future__ import annotations

import importlib.util
import json
import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "tools" / "analyze_failed_stt.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("analyze_failed_stt", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_wav(path: Path, samples: list[int], sample_rate: int = 16_000) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(struct.pack(f"<{len(samples)}h", *samples))


class AnalyzeFailedSttTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = load_tool()

    def test_silence_has_no_active_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "silent.wav"
            write_wav(path, [0] * 16_000)
            metrics = self.tool.analyze_wav(path)
            self.assertEqual(metrics["duration_seconds"], 1.0)
            self.assertEqual(metrics["rms_percent"], 0.0)
            self.assertIsNone(metrics["rms_dbfs"])
            self.assertEqual(metrics["windows_rms_ge_0_5_percent"], 0)

    def test_brief_activity_is_visible_in_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "brief.wav"
            samples = [0] * 16_000
            for index in range(8_000, 9_600):
                samples[index] = round(0.05 * 32767 * math.sin(index / 7))
            write_wav(path, samples)
            metrics = self.tool.analyze_wav(path)
            self.assertGreater(metrics["max_window_rms_percent"], 3.0)
            self.assertEqual(metrics["windows_rms_ge_3_percent"], 1)
            self.assertAlmostEqual(metrics["max_window_at_seconds"], 0.5)

    def test_transcript_is_excluded_from_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "capture.wav"
            write_wav(path, [10] * 160)
            path.with_suffix(".json").write_text(
                json.dumps(
                    {
                        "captured_at": "2026-08-07T00:00:00+00:00",
                        "reason": "test",
                        "partial_input_transcript": "private text",
                        "settings": {"mic_volume": "50"},
                    }
                ),
                encoding="utf-8",
            )
            result = self.tool.analyze_capture(path)
            self.assertNotIn("partial_input_transcript", result)
            self.assertNotIn("private text", json.dumps(result))
            self.assertEqual(result["settings"]["mic_volume"], "50")


if __name__ == "__main__":
    unittest.main()
