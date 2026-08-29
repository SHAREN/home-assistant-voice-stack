"""Utility functions for audio processing."""

import datetime
import json
import logging
import math
from pathlib import Path
import struct


def set_detailed_logging(enabled: bool) -> None:
    """Set package logging verbosity for Gemini Live."""
    level = logging.DEBUG if enabled else logging.INFO
    logging.getLogger("custom_components.gemini_live").setLevel(level)

def pcm_to_wav(pcm_data: bytes, sample_rate: int = 16000) -> bytes:
    """Wrap raw 16-bit signed PCM mono audio in a WAV container."""
    num_channels = 1
    sample_width = 2  # 16-bit

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(pcm_data),
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM format code
        num_channels,
        sample_rate,
        sample_rate * num_channels * sample_width,
        num_channels * sample_width,
        sample_width * 8,
        b"data",
        len(pcm_data),
    )
    return header + pcm_data


def streaming_wav_header(sample_rate: int = 16000) -> bytes:
    """Return a WAV header whose data length is terminated by end-of-stream."""
    num_channels = 1
    sample_width = 2
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        0xFFFFFFFF,
        b"WAVE",
        b"fmt ",
        16,
        1,
        num_channels,
        sample_rate,
        sample_rate * num_channels * sample_width,
        num_channels * sample_width,
        sample_width * 8,
        b"data",
        0xFFFFFFFF,
    )


def resample_24k_to_16k(data: bytes) -> bytes:
    """Resample raw 16-bit signed PCM mono audio from 24kHz down to 16kHz using linear interpolation."""
    num_samples = len(data) // 2
    if num_samples == 0:
        return b""

    samples = struct.unpack(f"<{num_samples}h", data)
    output = []
    i = 0
    while i < num_samples - 2:
        output.append(samples[i])
        output.append((samples[i+1] + samples[i+2]) // 2)
        i += 3
    if i < num_samples:
        output.append(samples[i])

    return struct.pack(f"<{len(output)}h", *output)


def analyze_pcm_metrics(pcm_data: bytes, sample_rate: int = 16000) -> dict:
    """Return structured metrics for raw mono 16-bit PCM without retaining audio."""
    num_samples = len(pcm_data) // 2
    if num_samples == 0:
        return {
            "pcm_bytes": len(pcm_data),
            "duration_seconds": 0.0,
            "rms": 0.0,
            "rms_percent": 0.0,
            "rms_dbfs": None,
            "peak": 0,
            "peak_percent": 0.0,
            "peak_dbfs": None,
            "label": "NO_AUDIO",
        }

    samples = struct.unpack(f"<{num_samples}h", pcm_data[: num_samples * 2])
    rms = math.sqrt(sum(sample * sample for sample in samples) / num_samples)
    peak = max(abs(sample) for sample in samples)
    clipped_samples = sum(1 for sample in samples if abs(sample) >= 32760)
    zero_crossings = sum(
        1
        for left, right in zip(samples, samples[1:], strict=False)
        if (left < 0 <= right) or (right < 0 <= left)
    )
    frame_size = max(1, sample_rate // 50)
    frame_rms = []
    for offset in range(0, num_samples, frame_size):
        frame = samples[offset : offset + frame_size]
        if not frame:
            continue
        frame_rms.append(
            math.sqrt(sum(sample * sample for sample in frame) / len(frame))
        )
    sorted_frame_rms = sorted(frame_rms)

    def percentile(values: list[float], ratio: float) -> float:
        if not values:
            return 0.0
        index = min(len(values) - 1, round((len(values) - 1) * ratio))
        return values[index]

    noise_floor = percentile(sorted_frame_rms, 0.2)
    speech_level = percentile(sorted_frame_rms, 0.9)
    snr_estimate_db = (
        round(20 * math.log10(speech_level / noise_floor), 2)
        if noise_floor > 0 and speech_level > noise_floor
        else None
    )
    active_threshold = max(noise_floor * 3, 32767 * 0.005)
    active_frames = sum(1 for value in frame_rms if value >= active_threshold)
    rms_percent = rms / 32767 * 100
    peak_percent = peak / 32767 * 100
    if rms_percent < 0.5:
        label = "SILENT"
    elif rms_percent < 3.0:
        label = "VERY_QUIET"
    elif rms_percent < 10.0:
        label = "QUIET"
    else:
        label = "SPEECH"

    def dbfs(percent: float) -> float | None:
        return round(20 * math.log10(percent / 100), 2) if percent > 0 else None

    return {
        "pcm_bytes": len(pcm_data),
        "duration_seconds": round(num_samples / sample_rate, 3),
        "rms": round(rms, 3),
        "rms_percent": round(rms_percent, 4),
        "rms_dbfs": dbfs(rms_percent),
        "peak": peak,
        "peak_percent": round(peak_percent, 4),
        "peak_dbfs": dbfs(peak_percent),
        "clipped_samples": clipped_samples,
        "clipping_percent": round(clipped_samples / num_samples * 100, 4),
        "zero_crossing_rate": round(zero_crossings / max(1, num_samples - 1), 4),
        "noise_floor_rms_percent": round(noise_floor / 32767 * 100, 4),
        "speech_level_rms_percent": round(speech_level / 32767 * 100, 4),
        "snr_estimate_db": snr_estimate_db,
        "frame_count": len(frame_rms),
        "active_frame_percent": round(
            active_frames / len(frame_rms) * 100,
            2,
        ) if frame_rms else 0.0,
        "label": label,
    }


def build_latest_stt_metrics(
    *,
    turn_id: str,
    conversation_id: str,
    input_transcript: str,
    response_audio_received: bool,
    response_audio_bytes: int,
    response_text: str,
    input_audio_sent: bool,
    input_pcm: bytes,
    settings: dict,
) -> dict:
    """Build privacy-safe, STT-specific metrics for one completed turn."""
    transcript_received = bool(input_transcript.strip())
    pipeline_succeeded = response_audio_received or bool(response_text.strip())
    return {
        "schema_version": 1,
        "turn_id": turn_id,
        "conversation_id": conversation_id,
        "outcome": "stt_success" if transcript_received else "stt_failed",
        "pipeline_result": "success" if pipeline_succeeded else "error",
        "input_audio_sent": input_audio_sent,
        "no_audio_sent_error": not input_audio_sent,
        "input_transcript_received": transcript_received,
        "response_audio_received": response_audio_received,
        "response_audio_bytes": response_audio_bytes,
        "pcm": analyze_pcm_metrics(input_pcm),
        "settings": settings,
    }


def save_latest_stt_metrics(path: str, metadata: dict) -> str:
    """Atomically replace the latest privacy-safe STT metrics JSON."""
    metrics_path = Path(path)
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    turn_id = str(metadata.get("turn_id") or "unknown")
    temp_path = metrics_path.with_name(f".{metrics_path.name}.{turn_id}.tmp")
    payload = {
        **metadata,
        # Caller metadata must never override the authoritative write time.
        "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    try:
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temp_path.replace(metrics_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return str(metrics_path)


def save_failed_stt_capture(
    directory: str,
    pcm_data: bytes,
    metadata: dict,
    keep: int = 30,
    sample_rate: int = 16000,
) -> tuple[str, str]:
    """Persist one failed STT input as WAV + JSON and prune old captures."""
    capture_dir = Path(directory)
    capture_dir.mkdir(parents=True, exist_ok=True)

    captured_at = datetime.datetime.now(datetime.timezone.utc)
    turn_id = str(metadata.get("turn_id") or "unknown")
    stem = f"{captured_at.strftime('%Y%m%dT%H%M%SZ')}-{turn_id}"
    wav_path = capture_dir / f"{stem}.wav"
    json_path = capture_dir / f"{stem}.json"

    wav_path.write_bytes(pcm_to_wav(pcm_data, sample_rate))
    payload = {
        "captured_at": captured_at.isoformat(),
        "sample_rate": sample_rate,
        "channels": 1,
        "sample_width_bits": 16,
        "pcm_bytes": len(pcm_data),
        "duration_seconds": round(len(pcm_data) / (sample_rate * 2), 3),
        **metadata,
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    wav_files = sorted(
        capture_dir.glob("*.wav"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for stale_wav in wav_files[max(keep, 0) :]:
        stale_json = stale_wav.with_suffix(".json")
        stale_wav.unlink(missing_ok=True)
        stale_json.unlink(missing_ok=True)

    return str(wav_path), str(json_path)
