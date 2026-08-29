#!/usr/bin/env python3
"""Summarize failed-STT WAV captures without copying audio or transcripts.

The tool reads a local directory containing the runtime WAV/JSON pairs and
prints JSON metadata only. It intentionally ignores partial transcripts.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import wave
from pathlib import Path
from typing import Any


def _dbfs(percent: float) -> float | None:
    if percent <= 0:
        return None
    return round(20 * math.log10(percent / 100), 2)


def analyze_wav(path: Path, window_ms: int = 100) -> dict[str, Any]:
    """Return aggregate and fixed-window PCM metrics for one mono 16-bit WAV."""
    with wave.open(str(path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError(f"expected mono 16-bit PCM: {path}")
        sample_rate = source.getframerate()
        frames = source.readframes(source.getnframes())

    samples = struct.unpack(f"<{len(frames) // 2}h", frames) if frames else ()
    duration = len(samples) / sample_rate if sample_rate else 0
    if samples:
        rms_percent = (
            math.sqrt(sum(value * value for value in samples) / len(samples))
            / 32767
            * 100
        )
        peak_percent = max(abs(value) for value in samples) / 32767 * 100
    else:
        rms_percent = 0.0
        peak_percent = 0.0

    window_samples = max(1, round(sample_rate * window_ms / 1000))
    windows: list[tuple[float, float]] = []
    for start in range(0, len(samples), window_samples):
        block = samples[start : start + window_samples]
        if not block:
            continue
        block_rms = (
            math.sqrt(sum(value * value for value in block) / len(block))
            / 32767
            * 100
        )
        block_peak = max(abs(value) for value in block) / 32767 * 100
        windows.append((block_rms, block_peak))

    max_window_index = (
        max(range(len(windows)), key=lambda index: windows[index][0])
        if windows
        else -1
    )
    max_window_rms = windows[max_window_index][0] if windows else 0.0

    return {
        "sample_rate": sample_rate,
        "duration_seconds": round(duration, 3),
        "rms_percent": round(rms_percent, 3),
        "rms_dbfs": _dbfs(rms_percent),
        "peak_percent": round(peak_percent, 3),
        "peak_dbfs": _dbfs(peak_percent),
        "window_ms": window_ms,
        "total_windows": len(windows),
        "max_window_rms_percent": round(max_window_rms, 3),
        "max_window_at_seconds": (
            round(max_window_index * window_ms / 1000, 3)
            if max_window_index >= 0
            else None
        ),
        "windows_rms_ge_0_5_percent": sum(rms >= 0.5 for rms, _ in windows),
        "windows_rms_ge_1_percent": sum(rms >= 1.0 for rms, _ in windows),
        "windows_rms_ge_3_percent": sum(rms >= 3.0 for rms, _ in windows),
    }


def analyze_capture(wav_path: Path, window_ms: int = 100) -> dict[str, Any]:
    """Merge safe JSON settings with WAV metrics, excluding transcript fields."""
    result: dict[str, Any] = {
        "capture": wav_path.stem,
        "metrics": analyze_wav(wav_path, window_ms),
    }
    metadata_path = wav_path.with_suffix(".json")
    if metadata_path.is_file():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        result.update(
            {
                "captured_at": metadata.get("captured_at"),
                "reason": metadata.get("reason"),
                "settings": metadata.get("settings", {}),
            }
        )
    return result


def analyze_directory(directory: Path, window_ms: int = 100) -> list[dict[str, Any]]:
    return [
        analyze_capture(wav_path, window_ms)
        for wav_path in sorted(directory.glob("*.wav"))
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path, help="Directory with failed-STT WAV/JSON pairs")
    parser.add_argument("--window-ms", type=int, default=100)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.directory.is_dir():
        print(f"not a directory: {args.directory}", file=sys.stderr)
        return 2
    if not 10 <= args.window_ms <= 1000:
        print("--window-ms must be 10..1000", file=sys.stderr)
        return 2
    print(
        json.dumps(
            analyze_directory(args.directory, args.window_ms),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
