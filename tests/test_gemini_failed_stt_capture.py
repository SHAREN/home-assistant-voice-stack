import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import wave
import datetime


utils_path = (
    Path(__file__).resolve().parents[1]
    / "experimental"
    / "gemini_live"
    / "utils.py"
)
spec = importlib.util.spec_from_file_location("gemini_live_utils_capture_test", utils_path)
utils = importlib.util.module_from_spec(spec)
assert spec is not None and spec.loader is not None
spec.loader.exec_module(utils)


class FailedSttCaptureTest(unittest.TestCase):
    def test_structured_pcm_metrics(self) -> None:
        pcm = b"\x00\x10" * 16000
        metrics = utils.analyze_pcm_metrics(pcm, 16000)
        self.assertEqual(metrics["duration_seconds"], 1.0)
        self.assertEqual(metrics["peak"], 4096)
        self.assertAlmostEqual(metrics["rms_percent"], 12.5004, places=3)
        self.assertEqual(metrics["label"], "SPEECH")

    def test_latest_metrics_are_atomic_and_transcript_free(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "latest.json"
            result = utils.save_latest_stt_metrics(
                str(path),
                {
                    "turn_id": "turn1",
                    "outcome": "success",
                    "pcm": {"rms_percent": 1.5},
                },
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(result, str(path))
            self.assertEqual(payload["outcome"], "success")
            self.assertNotIn("transcript", json.dumps(payload).lower())
            self.assertEqual(list(Path(temp_dir).glob("*.tmp")), [])

    def test_latest_metrics_write_time_cannot_be_overridden(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "latest.json"
            before = datetime.datetime.now(datetime.timezone.utc)
            utils.save_latest_stt_metrics(
                str(path),
                {"turn_id": "turn1", "captured_at": "forged"},
            )
            after = datetime.datetime.now(datetime.timezone.utc)
            captured_at = datetime.datetime.fromisoformat(
                json.loads(path.read_text(encoding="utf-8"))["captured_at"]
            )
            self.assertNotEqual(captured_at, "forged")
            self.assertEqual(captured_at.tzinfo, datetime.timezone.utc)
            self.assertLessEqual(before, captured_at)
            self.assertLessEqual(captured_at, after)

    def test_latest_metrics_separate_stt_and_pipeline_outcomes(self) -> None:
        successful_stt = utils.build_latest_stt_metrics(
            turn_id="one",
            conversation_id="conversation",
            input_transcript="recognized",
            response_audio_received=False,
            response_audio_bytes=0,
            response_text="",
            input_audio_sent=True,
            input_pcm=b"\x01\x00",
            settings={},
        )
        self.assertEqual(successful_stt["outcome"], "stt_success")
        self.assertEqual(successful_stt["pipeline_result"], "error")
        self.assertEqual(successful_stt["schema_version"], 1)

        response_without_stt = utils.build_latest_stt_metrics(
            turn_id="two",
            conversation_id="conversation",
            input_transcript="",
            response_audio_received=True,
            response_audio_bytes=10,
            response_text="",
            input_audio_sent=True,
            input_pcm=b"\x01\x00",
            settings={},
        )
        self.assertEqual(response_without_stt["outcome"], "stt_failed")
        self.assertEqual(response_without_stt["pipeline_result"], "success")

    def test_no_audio_metrics_are_explicit(self) -> None:
        metrics = utils.build_latest_stt_metrics(
            turn_id="empty",
            conversation_id="conversation",
            input_transcript="",
            response_audio_received=False,
            response_audio_bytes=0,
            response_text="",
            input_audio_sent=False,
            input_pcm=b"",
            settings={},
        )
        self.assertEqual(metrics["outcome"], "stt_failed")
        self.assertEqual(metrics["pipeline_result"], "error")
        self.assertFalse(metrics["input_audio_sent"])
        self.assertTrue(metrics["no_audio_sent_error"])
        self.assertEqual(metrics["pcm"]["label"], "NO_AUDIO")

    def test_writes_wav_and_metadata(self) -> None:
        pcm = (b"\x10\x00" * 16000)  # 1 second, 16kHz mono 16-bit PCM
        metadata = {
            "turn_id": "deadbeef",
            "reason": "test",
            "settings": {"mic_volume": "2.0"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path, json_path = utils.save_failed_stt_capture(
                temp_dir, pcm, metadata, 30, 16000
            )

            with wave.open(wav_path, "rb") as wav_file:
                self.assertEqual(wav_file.getframerate(), 16000)
                self.assertEqual(wav_file.getnchannels(), 1)
                self.assertEqual(wav_file.getsampwidth(), 2)
                self.assertEqual(wav_file.getnframes(), 16000)

            payload = json.loads(Path(json_path).read_text(encoding="utf-8"))
            self.assertEqual(payload["turn_id"], "deadbeef")
            self.assertEqual(payload["duration_seconds"], 1.0)
            self.assertEqual(payload["settings"]["mic_volume"], "2.0")

    def test_prunes_old_capture_pairs(self) -> None:
        pcm = b"\x00\x00" * 160
        with tempfile.TemporaryDirectory() as temp_dir:
            for index in range(3):
                utils.save_failed_stt_capture(
                    temp_dir,
                    pcm,
                    {"turn_id": f"turn{index}"},
                    2,
                    16000,
                )

            self.assertEqual(len(list(Path(temp_dir).glob("*.wav"))), 2)
            self.assertEqual(len(list(Path(temp_dir).glob("*.json"))), 2)


if __name__ == "__main__":
    unittest.main()
