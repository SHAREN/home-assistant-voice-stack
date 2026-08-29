from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STT_SOURCE = ROOT / "experimental" / "gemini_live" / "stt.py"


class GeminiSttMetricsFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = STT_SOURCE.read_text(encoding="utf-8")

    def test_no_audio_branch_reaches_metrics_finalizer(self) -> None:
        branch_start = self.source.index("if not audio_sent:")
        metrics_start = self.source.index(
            "latest_metrics = build_latest_stt_metrics(", branch_start
        )
        branch_to_metrics = self.source[branch_start:metrics_start]
        self.assertNotIn("return SpeechResult", branch_to_metrics)

    def test_metrics_use_terminal_audio_sent_state(self) -> None:
        self.assertIn("input_audio_sent=audio_sent", self.source)
        self.assertNotIn("no_audio_sent_error =", self.source)


if __name__ == "__main__":
    unittest.main()
