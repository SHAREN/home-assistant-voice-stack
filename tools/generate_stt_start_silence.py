#!/usr/bin/env python3
"""Generate the deterministic silent announcement used to open STT-only tests."""

from __future__ import annotations

import argparse
import wave
from pathlib import Path


SAMPLE_RATE = 16_000
DURATION_MS = 150


def write_silence(path: Path, duration_ms: int = DURATION_MS) -> None:
    if duration_ms <= 0:
        raise ValueError("duration_ms must be positive")
    frame_count = round(SAMPLE_RATE * duration_ms / 1000)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(b"\x00\x00" * frame_count)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--duration-ms", type=int, default=DURATION_MS)
    args = parser.parse_args()
    write_silence(args.output, args.duration_ms)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
