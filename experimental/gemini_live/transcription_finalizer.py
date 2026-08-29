"""Deterministic finalization state for Gemini input transcription."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import time


@dataclass(slots=True)
class TranscriptionFinalizer:
    """Accept only provider completion observed after the client audio boundary."""

    monotonic: Callable[[], float] = time.monotonic
    event: asyncio.Event = field(default_factory=asyncio.Event)
    transcript_parts: list[str] = field(default_factory=list)
    revision: int = 0
    audio_stream_end_pending: bool = False
    audio_stream_end_at: float | None = None
    last_transcript_update_at: float | None = None
    finalized_at: float | None = None
    final_reason: str | None = None
    failure_reason: str | None = None
    turn_complete_received: bool = False
    early_turn_complete_received: bool = False
    provider_finished_received: bool = False
    early_provider_finished_received: bool = False

    @property
    def transcript(self) -> str:
        """Return the complete transcript assembled in provider order."""
        return "".join(self.transcript_parts).strip()

    @property
    def done(self) -> bool:
        """Return whether success or failure has reached a terminal state."""
        return self.event.is_set()

    @property
    def tail_wait_ms(self) -> float | None:
        """Return provider-final wait after successful audio_stream_end."""
        if self.audio_stream_end_at is None or self.finalized_at is None:
            return None
        return max(0.0, (self.finalized_at - self.audio_stream_end_at) * 1000)

    def add_transcript_chunk(self, text: str | None) -> bool:
        """Append a non-empty provider delta without interpreting it."""
        if self.done or not text:
            return False
        self.transcript_parts.append(text)
        self.revision += 1
        self.last_transcript_update_at = self.monotonic()
        return True

    def mark_audio_stream_end_pending(self) -> None:
        """Record that source audio is exhausted and the end send is in flight."""
        if not self.done and self.audio_stream_end_at is None:
            self.audio_stream_end_pending = True

    def mark_audio_stream_end(self) -> None:
        """Record only a successfully sent client audio_stream_end boundary."""
        self.audio_stream_end_pending = False
        if self.audio_stream_end_at is None:
            self.audio_stream_end_at = self.monotonic()
        if self.provider_finished_received:
            self._finalize("provider_transcription_finished")
        elif self.turn_complete_received:
            self._finalize("provider_turn_complete")

    def mark_provider_finished(self) -> bool:
        """Finalize on the SDK's explicit transcription.finished marker."""
        if self.audio_stream_end_at is None:
            if self.audio_stream_end_pending:
                self.provider_finished_received = True
                return False
            self.early_provider_finished_received = True
            return False
        self.provider_finished_received = True
        return self._finalize("provider_transcription_finished")

    def mark_turn_complete(self) -> bool:
        """Use post-audio model turnComplete only as terminal fallback."""
        if self.audio_stream_end_at is None:
            if self.audio_stream_end_pending:
                self.turn_complete_received = True
                return False
            self.early_turn_complete_received = True
            return False
        self.turn_complete_received = True
        return self._finalize("provider_turn_complete")

    def fail(self, reason: str) -> None:
        """Wake the caller while preserving fail-closed semantics."""
        if self.done:
            return
        self.failure_reason = reason
        self.event.set()

    def _finalize(self, reason: str) -> bool:
        if self.done:
            return False
        self.final_reason = reason
        self.finalized_at = self.monotonic()
        self.event.set()
        return True
