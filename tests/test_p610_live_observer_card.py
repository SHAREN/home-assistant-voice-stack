from __future__ import annotations
from pathlib import Path
import re
import unittest
ROOT = Path(__file__).resolve().parents[1]
CARD = ROOT / "experimental" / "gemini_live" / "www" / "p610-live-observer-card.js"
INIT = ROOT / "experimental" / "gemini_live" / "__init__.py"
CONST = ROOT / "experimental" / "gemini_live" / "const.py"

class P610LiveObserverCardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.card_source = CARD.read_text(encoding="utf-8")
        cls.init_source = INIT.read_text(encoding="utf-8")
        cls.const_source = CONST.read_text(encoding="utf-8")

    def test_card_is_display_only(self) -> None:
        for forbidden in ("getUserMedia", "RTCPeerConnection", "MediaStream", "<audio", ".play(", "callService"):
            self.assertNotIn(forbidden, self.card_source)

    def test_card_loads_authenticated_history_then_stays_live(self) -> None:
        self.assertIn("hass.connection.subscribeEvents", self.card_source)
        self.assertIn("hass.connection.sendMessagePromise", self.card_source)
        self.assertIn('"gemini_live_turn_event"', self.card_source)
        self.assertIn('"gemini_live/p610_history"', self.card_source)
        self.assertIn("Новый диалог · wake", self.card_source)
        self.assertIn("Диалог завершён", self.card_source)
        self.assertIn("disconnectedCallback", self.card_source)

    def test_frontend_and_history_are_registered_once(self) -> None:
        self.assertIn("_async_setup_frontend_once", self.init_source)
        self.assertIn("async_setup_observer_history", self.init_source)
        self.assertIn("p610-live-observer-card.js", self.init_source)

    def test_cache_busting_matches_reducer_version(self) -> None:
        version_match = re.search(r'OBSERVER_CARD_VERSION\s*=\s*"([^"]+)"', self.const_source)
        self.assertIsNotNone(version_match)
        self.assertIn("p610-live-observer-state.mjs?v=" + version_match.group(1), self.card_source)

if __name__ == "__main__": unittest.main()
