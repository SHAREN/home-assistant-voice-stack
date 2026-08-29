from __future__ import annotations

import importlib.util
import tempfile
import unittest
import wave
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "tools" / "generate_stt_start_silence.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("generate_stt_start_silence", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GenerateSttStartSilenceTests(unittest.TestCase):
    def test_generates_exact_silent_pcm(self) -> None:
        tool = load_tool()
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "silence.wav"
            tool.write_silence(output)
            with wave.open(str(output), "rb") as source:
                self.assertEqual(source.getnchannels(), 1)
                self.assertEqual(source.getsampwidth(), 2)
                self.assertEqual(source.getframerate(), 16_000)
                self.assertEqual(source.getnframes(), 2_400)
                self.assertEqual(set(source.readframes(source.getnframes())), {0})


if __name__ == "__main__":
    unittest.main()
