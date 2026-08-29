from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tools.summarize_p610_calibration import diagnose_stage, load_jsonl, render_markdown, summarize


def row(
    *,
    level: int,
    effective: float,
    status: str,
    pre: float,
    post: float,
    gemini: float,
) -> dict:
    return {
        "level_percent": level,
        "effective_output_percent": effective,
        "status": status,
        "output_endpoint_id": "endpoint",
        "output_name": "Room speaker",
        "output_driver": "Driver",
        "output_master_volume_percent": 50,
        "system_input_name": "p610",
        "system_input_volume_percent": 100,
        "wake_word": "Okay Nabu",
        "wake_sensitivity": 0.6,
        "mic_volume": 50,
        "mic_auto_gain": 10,
        "mic_noise_suppression": "Off",
        "mic_muted": False,
        "phrase_sha256": "abc",
        "lva_capture_metrics": {
            "pre_webrtc": {"rms_percent": pre},
            "post_webrtc": {"rms_percent": post},
        },
        "stt_metrics": {"pcm": {"rms_percent": gemini}},
        "stt_response": {"speech": "PRIVATE TRANSCRIPT"},
        "previous_metrics_turn_id": "private-turn-id",
    }


class SummarizeP610CalibrationTests(unittest.TestCase):
    def test_summarizes_repeatable_minimum_without_private_fields(self) -> None:
        rows = [
            row(level=80, effective=40, status="transcript_present", pre=4, post=5, gemini=5),
            row(level=80, effective=40, status="transcript_present", pre=6, post=7, gemini=7),
            row(level=60, effective=30, status="transcript_present", pre=3, post=4, gemini=4),
            row(level=60, effective=30, status="stt_failed", pre=0.1, post=0.1, gemini=0.1),
        ]
        result = summarize(rows)
        self.assertEqual(result["rows"], 4)
        self.assertEqual(
            result["profiles"][0]["minimum_repeatable_transcript_effective_output_percent"],
            40.0,
        )
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("PRIVATE TRANSCRIPT", encoded)
        self.assertNotIn("private-turn-id", encoded)

    def test_stage_diagnosis_distinguishes_three_loss_points(self) -> None:
        self.assertEqual(
            diagnose_stage(row(level=1, effective=1, status="stt_failed", pre=0.1, post=0.1, gemini=0.1)),
            "near_silent_before_webrtc",
        )
        self.assertEqual(
            diagnose_stage(row(level=1, effective=1, status="stt_failed", pre=4, post=0.1, gemini=0.1)),
            "attenuated_in_webrtc",
        )
        self.assertEqual(
            diagnose_stage(row(level=1, effective=1, status="stt_failed", pre=4, post=4, gemini=0.1)),
            "loss_after_lva",
        )

    def test_markdown_contains_only_aggregate_table(self) -> None:
        result = summarize([row(level=80, effective=40, status="stt_failed", pre=4, post=4, gemini=4)])
        markdown = render_markdown(result)
        self.assertIn("| Profile | PC level | Effective |", markdown)
        self.assertIn("stt_failed_with_signal=1", markdown)
        self.assertNotIn("PRIVATE TRANSCRIPT", markdown)

    def test_loader_reports_invalid_jsonl_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "rows.jsonl"
            path.write_text('{}\n{"broken"\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "line 2"):
                load_jsonl(path)


if __name__ == "__main__":
    unittest.main()
