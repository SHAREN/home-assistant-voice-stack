"""Regression tests for deterministic Gemini transcription finalization."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "experimental" / "gemini_live" / "transcription_finalizer.py"
spec = importlib.util.spec_from_file_location(
    "gemini_live_transcription_finalizer_test",
    MODULE_PATH,
)
assert spec is not None and spec.loader is not None
finalizer_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = finalizer_module
spec.loader.exec_module(finalizer_module)
TranscriptionFinalizer = finalizer_module.TranscriptionFinalizer


class FakeClock:
    """Small controllable monotonic clock."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TranscriptionFinalizerTests(unittest.TestCase):
    def make_finalizer(self) -> tuple[TranscriptionFinalizer, FakeClock]:
        clock = FakeClock()
        return TranscriptionFinalizer(monotonic=clock), clock

    def test_one_chunk_provider_finished_after_audio_end_finalizes_early(self) -> None:
        finalizer, clock = self.make_finalizer()
        finalizer.mark_audio_stream_end()
        clock.advance(0.296)
        self.assertTrue(finalizer.add_transcript_chunk("Расскажи историю."))
        self.assertTrue(finalizer.mark_provider_finished())

        self.assertTrue(finalizer.done)
        self.assertEqual(finalizer.transcript, "Расскажи историю.")
        self.assertEqual(
            finalizer.final_reason,
            "provider_transcription_finished",
        )
        self.assertAlmostEqual(finalizer.tail_wait_ms or 0.0, 296.0)
        self.assertFalse(finalizer.turn_complete_received)

    def test_provider_finished_while_source_open_is_ignored(self) -> None:
        finalizer, clock = self.make_finalizer()
        finalizer.add_transcript_chunk("Привет.")
        self.assertFalse(finalizer.mark_provider_finished())
        self.assertFalse(finalizer.done)
        self.assertFalse(finalizer.provider_finished_received)

        clock.advance(0.1)
        finalizer.mark_audio_stream_end()
        self.assertFalse(finalizer.done)
        self.assertIsNone(finalizer.final_reason)

    def test_provider_finished_during_end_send_waits_for_send_success(self) -> None:
        finalizer, clock = self.make_finalizer()
        finalizer.add_transcript_chunk("Привет.")
        finalizer.mark_audio_stream_end_pending()
        self.assertFalse(finalizer.mark_provider_finished())
        self.assertFalse(finalizer.done)
        self.assertTrue(finalizer.provider_finished_received)

        clock.advance(0.1)
        finalizer.mark_audio_stream_end()
        self.assertTrue(finalizer.done)
        self.assertEqual(
            finalizer.final_reason,
            "provider_transcription_finished",
        )
        self.assertAlmostEqual(finalizer.tail_wait_ms or 0.0, 0.0)

    def test_early_finished_cannot_truncate_a_late_second_chunk(self) -> None:
        finalizer, _ = self.make_finalizer()
        finalizer.add_transcript_chunk("Выключи ")
        self.assertFalse(finalizer.mark_provider_finished())
        self.assertFalse(finalizer.provider_finished_received)
        finalizer.add_transcript_chunk("свет.")
        finalizer.mark_audio_stream_end_pending()
        finalizer.mark_audio_stream_end()
        self.assertFalse(finalizer.done)

        self.assertTrue(finalizer.mark_provider_finished())
        self.assertEqual(finalizer.transcript, "Выключи свет.")

    def test_deferred_finished_cannot_survive_failed_end_send(self) -> None:
        finalizer, _ = self.make_finalizer()
        finalizer.add_transcript_chunk("Выключи свет.")
        finalizer.mark_audio_stream_end_pending()
        finalizer.mark_provider_finished()
        finalizer.fail("audio_stream_end_send_failed")

        self.assertTrue(finalizer.done)
        self.assertEqual(
            finalizer.failure_reason,
            "audio_stream_end_send_failed",
        )
        self.assertIsNone(finalizer.final_reason)

    def test_multi_chunk_transcript_is_not_truncated(self) -> None:
        finalizer, _ = self.make_finalizer()
        finalizer.mark_audio_stream_end()
        for chunk in ("Включи ", "YouTube ", "на телевизоре."):
            finalizer.add_transcript_chunk(chunk)
        finalizer.mark_provider_finished()

        self.assertEqual(finalizer.revision, 3)
        self.assertEqual(finalizer.transcript, "Включи YouTube на телевизоре.")

    def test_late_second_chunk_is_included_before_provider_finished(self) -> None:
        finalizer, clock = self.make_finalizer()
        finalizer.mark_audio_stream_end()
        finalizer.add_transcript_chunk("Выключи ")
        clock.advance(0.8)
        finalizer.add_transcript_chunk("свет.")
        clock.advance(0.1)
        finalizer.mark_provider_finished()

        self.assertEqual(finalizer.transcript, "Выключи свет.")
        self.assertAlmostEqual(finalizer.tail_wait_ms or 0.0, 900.0)

    def test_long_utterance_preserves_all_provider_chunks(self) -> None:
        finalizer, _ = self.make_finalizer()
        finalizer.mark_audio_stream_end()
        chunks = [f"часть-{index} " for index in range(100)]
        for chunk in chunks:
            finalizer.add_transcript_chunk(chunk)
        finalizer.mark_provider_finished()

        self.assertEqual(finalizer.revision, len(chunks))
        self.assertEqual(finalizer.transcript, "".join(chunks).strip())

    def test_finished_false_partial_does_not_finalize(self) -> None:
        finalizer, _ = self.make_finalizer()
        finalizer.mark_audio_stream_end()
        finalizer.add_transcript_chunk("частичный текст")

        self.assertFalse(finalizer.done)
        self.assertIsNone(finalizer.final_reason)

    def test_empty_provider_finished_is_terminal_but_not_usable(self) -> None:
        finalizer, _ = self.make_finalizer()
        finalizer.mark_audio_stream_end()
        finalizer.mark_provider_finished()

        self.assertTrue(finalizer.done)
        self.assertEqual(finalizer.transcript, "")
        self.assertEqual(
            finalizer.final_reason,
            "provider_transcription_finished",
        )

    def test_early_turn_complete_is_ignored(self) -> None:
        finalizer, _ = self.make_finalizer()
        finalizer.add_transcript_chunk("Привет.")

        self.assertFalse(finalizer.mark_turn_complete())
        self.assertFalse(finalizer.done)
        self.assertTrue(finalizer.early_turn_complete_received)

    def test_post_audio_turn_complete_remains_compatibility_fallback(self) -> None:
        finalizer, clock = self.make_finalizer()
        finalizer.mark_audio_stream_end()
        finalizer.add_transcript_chunk("Привет.")
        clock.advance(0.7)

        self.assertTrue(finalizer.mark_turn_complete())
        self.assertEqual(finalizer.final_reason, "provider_turn_complete")
        self.assertTrue(finalizer.turn_complete_received)
        self.assertAlmostEqual(finalizer.tail_wait_ms or 0.0, 700.0)

    def test_turn_complete_during_end_send_waits_for_send_success(self) -> None:
        finalizer, _ = self.make_finalizer()
        finalizer.add_transcript_chunk("Привет.")
        finalizer.mark_audio_stream_end_pending()
        self.assertFalse(finalizer.mark_turn_complete())
        self.assertFalse(finalizer.done)

        finalizer.mark_audio_stream_end()
        self.assertTrue(finalizer.done)
        self.assertEqual(finalizer.final_reason, "provider_turn_complete")

    def test_failure_is_terminal_and_cannot_become_success(self) -> None:
        finalizer, _ = self.make_finalizer()
        finalizer.mark_audio_stream_end()
        finalizer.add_transcript_chunk("текст")
        finalizer.fail("unexpected_tool_call")

        self.assertTrue(finalizer.done)
        self.assertEqual(finalizer.failure_reason, "unexpected_tool_call")
        self.assertFalse(finalizer.mark_provider_finished())
        self.assertIsNone(finalizer.final_reason)

    def test_partial_timeout_remains_fail_closed(self) -> None:
        finalizer, _ = self.make_finalizer()
        finalizer.mark_audio_stream_end()
        finalizer.add_transcript_chunk("Включи")
        finalizer.fail("incomplete_transcription")

        self.assertEqual(finalizer.failure_reason, "incomplete_transcription")
        self.assertIsNone(finalizer.final_reason)
        self.assertEqual(finalizer.transcript, "Включи")


if __name__ == "__main__":
    unittest.main()
