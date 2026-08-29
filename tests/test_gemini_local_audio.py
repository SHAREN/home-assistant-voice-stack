from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest
import wave


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "experimental" / "gemini_live" / "local_audio.py"
spec = importlib.util.spec_from_file_location("gemini_live_local_audio_test", MODULE_PATH)
assert spec is not None and spec.loader is not None
local_audio = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = local_audio
spec.loader.exec_module(local_audio)


class GeminiLocalAudioTests(unittest.TestCase):
    def test_offline_response_is_bundled_pcm_without_external_calls(self) -> None:
        audio = local_audio.offline_response_wav()
        self.assertEqual(local_audio.OFFLINE_RESPONSE_TEXT, "Нет подключения к интернету.")
        self.assertTrue(audio.startswith(b"RIFF"))
        wav_path = MODULE_PATH.with_name("assets") / "offline_network_ru.wav"
        with wave.open(str(wav_path), "rb") as wav_file:
            self.assertEqual(wav_file.getnchannels(), 1)
            self.assertEqual(wav_file.getsampwidth(), 2)
            self.assertEqual(wav_file.getframerate(), 16000)
            self.assertGreater(wav_file.getnframes(), 1600)

    def test_loader_is_cached_for_offline_reuse(self) -> None:
        self.assertIs(
            local_audio.offline_response_wav(),
            local_audio.offline_response_wav(),
        )

    def test_stop_silence_has_no_one_second_payload(self) -> None:
        audio = local_audio.silent_response_wav()
        self.assertEqual(len(audio), 44)
        self.assertTrue(audio.startswith(b"RIFF"))


if __name__ == "__main__":
    unittest.main()
