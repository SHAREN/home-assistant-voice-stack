#!/usr/bin/env python3
"""Summarize P610 calibration JSONL without exposing transcripts or responses."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import statistics
from typing import Any, Iterable


PROFILE_FIELDS = (
    "output_endpoint_id",
    "output_name",
    "output_driver",
    "output_master_volume_percent",
    "system_input_name",
    "system_input_volume_percent",
    "wake_word",
    "wake_sensitivity",
    "mic_volume",
    "mic_auto_gain",
    "mic_noise_suppression",
    "mic_muted",
    "phrase_sha256",
)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _nested(row: dict[str, Any], *path: str) -> Any:
    value: Any = row
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            row = json.loads(raw_line)
        except json.JSONDecodeError as err:
            raise ValueError(f"invalid JSONL at line {line_number}: {err.msg}") from err
        if not isinstance(row, dict):
            raise ValueError(f"JSONL line {line_number} is not an object")
        rows.append(row)
    return rows


def diagnose_stage(row: dict[str, Any]) -> str:
    pre = _number(_nested(row, "lva_capture_metrics", "pre_webrtc", "rms_percent"))
    post = _number(_nested(row, "lva_capture_metrics", "post_webrtc", "rms_percent"))
    gemini = _number(_nested(row, "stt_metrics", "pcm", "rms_percent"))
    if pre is None or post is None or gemini is None:
        return "missing_metrics"
    if pre < 0.5:
        return "near_silent_before_webrtc"
    if post < 0.5:
        return "attenuated_in_webrtc"
    if gemini < 0.5:
        return "loss_after_lva"
    if row.get("status") == "transcript_present":
        return "transcript_present_with_signal"
    return "stt_failed_with_signal"


def _median(values: Iterable[float]) -> float | None:
    values_list = list(values)
    return round(statistics.median(values_list), 4) if values_list else None


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[Any, ...], dict[tuple[Any, Any], list[dict[str, Any]]]] = {}
    for row in rows:
        profile_key = tuple(row.get(field) for field in PROFILE_FIELDS)
        level_key = (row.get("level_percent"), row.get("effective_output_percent"))
        grouped.setdefault(profile_key, {}).setdefault(level_key, []).append(row)

    profiles: list[dict[str, Any]] = []
    for profile_key, level_groups in grouped.items():
        profile = dict(zip(PROFILE_FIELDS, profile_key, strict=True))
        levels: list[dict[str, Any]] = []
        repeatable_effective_levels: list[float] = []
        for (level_percent, effective_percent), level_rows in level_groups.items():
            status_counts = Counter(str(row.get("status") or "unknown") for row in level_rows)
            diagnoses = Counter(diagnose_stage(row) for row in level_rows)
            pre_values = [
                value
                for row in level_rows
                if (value := _number(_nested(row, "lva_capture_metrics", "pre_webrtc", "rms_percent")))
                is not None
            ]
            post_values = [
                value
                for row in level_rows
                if (value := _number(_nested(row, "lva_capture_metrics", "post_webrtc", "rms_percent")))
                is not None
            ]
            gemini_values = [
                value
                for row in level_rows
                if (value := _number(_nested(row, "stt_metrics", "pcm", "rms_percent")))
                is not None
            ]
            repeatable = len(level_rows) >= 2 and status_counts["transcript_present"] == len(level_rows)
            effective_number = _number(effective_percent)
            if repeatable and effective_number is not None:
                repeatable_effective_levels.append(effective_number)
            levels.append(
                {
                    "level_percent": level_percent,
                    "effective_output_percent": effective_percent,
                    "attempts": len(level_rows),
                    "transcript_present_attempts": status_counts["transcript_present"],
                    "stt_failed_attempts": status_counts["stt_failed"],
                    "infrastructure_failed_attempts": status_counts["failed"],
                    "repeatable_transcript_presence": repeatable,
                    "pre_webrtc_rms_percent_median": _median(pre_values),
                    "post_webrtc_rms_percent_median": _median(post_values),
                    "gemini_rms_percent_median": _median(gemini_values),
                    "diagnosis_counts": dict(sorted(diagnoses.items())),
                }
            )
        levels.sort(
            key=lambda item: (
                -(
                    effective
                    if (effective := _number(item["effective_output_percent"])) is not None
                    else -1
                ),
                -(
                    level
                    if (level := _number(item["level_percent"])) is not None
                    else -1
                ),
            )
        )
        profiles.append(
            {
                "profile": profile,
                "levels": levels,
                "minimum_repeatable_transcript_effective_output_percent": (
                    min(repeatable_effective_levels) if repeatable_effective_levels else None
                ),
            }
        )

    return {
        "schema_version": 1,
        "rows": len(rows),
        "profiles": profiles,
        "limitations": [
            "transcript_present does not prove that the fixed phrase was recognized correctly",
            "audible TTS and session-end cue require separate physical confirmation",
        ],
    }


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "| Profile | PC level | Effective | Attempts | STT present | STT failed | Infra failed | Pre RMS | Post RMS | Gemini RMS | Diagnosis |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for profile_index, profile in enumerate(summary["profiles"], 1):
        for level in profile["levels"]:
            diagnosis = ", ".join(
                f"{key}={value}" for key, value in level["diagnosis_counts"].items()
            )
            lines.append(
                "| {profile} | {level} | {effective} | {attempts} | {present} | {failed} | {infra} | {pre} | {post} | {gemini} | {diagnosis} |".format(
                    profile=profile_index,
                    level=level["level_percent"],
                    effective=level["effective_output_percent"],
                    attempts=level["attempts"],
                    present=level["transcript_present_attempts"],
                    failed=level["stt_failed_attempts"],
                    infra=level["infrastructure_failed_attempts"],
                    pre=level["pre_webrtc_rms_percent_median"],
                    post=level["post_webrtc_rms_percent_median"],
                    gemini=level["gemini_rms_percent_median"],
                    diagnosis=diagnosis,
                )
            )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jsonl", type=Path)
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    args = parser.parse_args()
    summary = summarize(load_jsonl(args.jsonl))
    if args.format == "json":
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_markdown(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
