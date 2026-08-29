from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from aiohttp import web


ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "apps" / "p610_mic_monitor" / "app.py"


def load_app_module():
    spec = importlib.util.spec_from_file_location("p610_mic_monitor_app", APP_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class P610MicVolumeSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = load_app_module()
        self.base = {
            "system_volume_percent": 100,
            "mic_volume": 50,
            "mic_noise_suppression": 0,
            "mic_auto_gain": 10,
        }

    def test_percentage_is_forwarded_as_integer(self) -> None:
        validated = self.app.validate_sensitivity_payload(self.base)
        self.assertEqual(validated["mic_volume"], 50)
        self.assertEqual(
            self.app.ASSIST_SATELLITE_SLUG,
            "local_assist_satellite_session_end",
        )

    def test_old_multiplier_value_is_rejected(self) -> None:
        payload = dict(self.base, mic_volume=0.5)
        with self.assertRaises(web.HTTPBadRequest):
            self.app.validate_sensitivity_payload(payload)

    def test_percentage_above_upstream_limit_is_rejected(self) -> None:
        payload = dict(self.base, mic_volume=101)
        with self.assertRaises(web.HTTPBadRequest):
            self.app.validate_sensitivity_payload(payload)


if __name__ == "__main__":
    unittest.main()
