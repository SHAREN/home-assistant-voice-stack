"""Bundled deterministic audio responses that require no network access."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import struct


OFFLINE_RESPONSE_TEXT = "Нет подключения к интернету."
_OFFLINE_WAV_PATH = Path(__file__).with_name("assets") / "offline_network_ru.wav"


@lru_cache(maxsize=1)
def offline_response_wav() -> bytes:
    """Load and validate the bundled Russian offline response."""
    data = _OFFLINE_WAV_PATH.read_bytes()
    if len(data) <= 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise RuntimeError("Bundled offline response is not a valid WAV file")
    return data


@lru_cache(maxsize=1)
def silent_response_wav() -> bytes:
    """Return a zero-frame WAV so lifecycle cue need not wait on dummy silence."""
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36,
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        16000,
        32000,
        2,
        16,
        b"data",
        0,
    )
