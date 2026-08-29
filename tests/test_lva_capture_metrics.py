from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
METRICS_PATH = (
    ROOT
    / "apps"
    / "assist_satellite_session_end"
    / "patches"
    / "capture_metrics.py"
)


def load_metrics_module():
    spec = importlib.util.spec_from_file_location("lva_capture_metrics", METRICS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LvaCaptureMetricsTests(unittest.TestCase):
    def test_persists_raw_and_processed_metrics_without_audio_or_text(self) -> None:
        module = load_metrics_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "latest.json"
            tracker = module.CaptureMetricsTracker(str(path), 1024)
            settings = {
                "mic_volume": 50,
                "mic_auto_gain": 10,
                "mic_noise_suppression": 0,
            }
            tracker.observe(
                streaming=True,
                satellite_identity=1,
                pre_webrtc_pcm=struct.pack("<4h", 1000, -1000, 1000, -1000),
                post_webrtc_pcm=struct.pack("<4h", 500, -500, 500, -500),
                settings=settings,
            )
            tracker.observe(
                streaming=False,
                satellite_identity=1,
                pre_webrtc_pcm=b"",
                post_webrtc_pcm=b"",
                settings=settings,
            )

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["reason"], "streaming_ended")
            self.assertEqual(payload["pre_webrtc"]["pcm_bytes"], 8)
            self.assertEqual(payload["post_webrtc"]["pcm_bytes"], 8)
            self.assertAlmostEqual(payload["post_to_pre_rms_ratio"], 0.5, places=3)
            self.assertEqual(payload["settings"]["mic_volume"], 50)
            self.assertEqual(payload["boundary_tolerance_ms"], 64.0)
            serialized = json.dumps(payload).lower()
            self.assertNotIn("transcript", serialized)
            self.assertNotIn("audio_data", serialized)
            self.assertLess(path.stat().st_size, 4096)

    def test_missing_processed_pcm_is_visible(self) -> None:
        module = load_metrics_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "latest.json"
            tracker = module.CaptureMetricsTracker(str(path), 1024)
            tracker.observe(
                streaming=True,
                satellite_identity=1,
                pre_webrtc_pcm=struct.pack("<2h", 1000, -1000),
                post_webrtc_pcm=b"",
                settings={},
            )
            tracker.finish("recorder_failure")
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["post_webrtc"]["label"], "NO_AUDIO")
            self.assertEqual(payload["reason"], "recorder_failure")

    def test_new_window_atomically_replaces_previous_window(self) -> None:
        module = load_metrics_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "latest.json"
            tracker = module.CaptureMetricsTracker(str(path), 1024)
            tracker.observe(
                streaming=True,
                satellite_identity=1,
                pre_webrtc_pcm=b"",
                post_webrtc_pcm=b"",
                settings={},
            )
            first_id = tracker.window_id
            tracker.observe(
                streaming=False,
                satellite_identity=1,
                pre_webrtc_pcm=b"",
                post_webrtc_pcm=b"",
                settings={},
            )
            tracker.observe(
                streaming=True,
                satellite_identity=1,
                pre_webrtc_pcm=b"",
                post_webrtc_pcm=b"",
                settings={},
            )
            second_id = tracker.window_id
            tracker.observe(
                streaming=False,
                satellite_identity=1,
                pre_webrtc_pcm=b"",
                post_webrtc_pcm=b"",
                settings={},
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotEqual(first_id, second_id)
            self.assertEqual(payload["window_id"], second_id)
            self.assertEqual(payload["capture_sequence"], 2)
            self.assertEqual(list(Path(temp_dir).glob("*.tmp")), [])

    def test_satellite_replacement_splits_window_and_full_scale_is_clamped(self) -> None:
        module = load_metrics_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "latest.json"
            tracker = module.CaptureMetricsTracker(str(path), 1024)
            full_scale = struct.pack("<2h", -32768, 32767)
            tracker.observe(
                streaming=True,
                satellite_identity=1,
                pre_webrtc_pcm=full_scale,
                post_webrtc_pcm=full_scale,
                settings={},
            )
            tracker.observe(
                streaming=True,
                satellite_identity=2,
                pre_webrtc_pcm=full_scale,
                post_webrtc_pcm=full_scale,
                settings={},
            )
            replaced = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(replaced["reason"], "satellite_replaced")
            self.assertEqual(replaced["pre_webrtc"]["peak_percent"], 100.0)
            self.assertEqual(tracker.capture_sequence, 2)
            self.assertEqual(tracker.satellite_generation, 2)

    def test_invalid_pcm_disables_metrics_without_raising(self) -> None:
        module = load_metrics_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker = module.CaptureMetricsTracker(
                str(Path(temp_dir) / "latest.json"),
                1024,
            )
            with self.assertLogs("lva_capture_metrics", level="ERROR"):
                tracker.observe(
                    streaming=True,
                    satellite_identity=1,
                    pre_webrtc_pcm=None,
                    post_webrtc_pcm=b"",
                    settings={},
                )
            self.assertTrue(tracker.failed)
            self.assertFalse(tracker.active)

    def test_unwritable_target_fails_open_and_resets_window(self) -> None:
        module = load_metrics_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            parent_file = Path(temp_dir) / "not_a_directory"
            parent_file.write_text("x", encoding="utf-8")
            tracker = module.CaptureMetricsTracker(
                str(parent_file / "latest.json"),
                1024,
            )
            tracker.observe(
                streaming=True,
                satellite_identity=1,
                pre_webrtc_pcm=b"",
                post_webrtc_pcm=b"",
                settings={},
            )
            with self.assertLogs("lva_capture_metrics", level="ERROR"):
                tracker.finish("streaming_ended")
            self.assertTrue(tracker.failed)
            self.assertFalse(tracker.active)

    def test_empty_path_is_noop_and_settings_are_allowlisted(self) -> None:
        module = load_metrics_module()
        disabled = module.CaptureMetricsTracker("   ", 1024)
        disabled.observe(
            streaming=True,
            satellite_identity=1,
            pre_webrtc_pcm=None,
            post_webrtc_pcm=None,
            settings={"transcript": "private"},
        )
        self.assertFalse(disabled.enabled)
        self.assertFalse(disabled.failed)

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "latest.json"
            tracker = module.CaptureMetricsTracker(str(path), 1024)
            tracker.observe(
                streaming=True,
                satellite_identity=1,
                pre_webrtc_pcm=b"",
                post_webrtc_pcm=b"",
                settings={
                    "mic_volume": 50,
                    "transcript": "private",
                    "audio": b"private",
                },
            )
            tracker.finish("streaming_ended")
            settings = json.loads(path.read_text(encoding="utf-8"))["settings"]
            self.assertEqual(set(settings), set(module.SAFE_SETTING_KEYS))
            self.assertNotIn("private", json.dumps(settings))


if __name__ == "__main__":
    unittest.main()
