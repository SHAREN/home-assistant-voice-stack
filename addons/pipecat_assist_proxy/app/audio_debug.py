"""Rich 24-hour audio capture for Pipecat voice-session debugging."""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from pipecat.frames.frames import Frame, InputAudioRawFrame, OutputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.config import DATA_DIR, FlowConfig, RuntimeConfig

AUDIO_DEBUG_DIR = DATA_DIR / "audio-debug"
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_WAV_SAMPLE_WIDTH_BYTES = 2
_RETENTION_HOURS = max(1.0, float(os.getenv("AUDIO_DEBUG_RETENTION_HOURS", "24") or 24))
_RETENTION_SECONDS = _RETENTION_HOURS * 3600.0
_MIX_RATE = 48_000


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip()).strip("-")
    return slug or "flow"


@dataclass
class AudioDebugClock:
    origin_monotonic: float | None = None
    origin_utc: str | None = None

    def start(self, origin_monotonic: float | None = None) -> None:
        if self.origin_monotonic is not None:
            return
        self.origin_monotonic = origin_monotonic if origin_monotonic is not None else time.monotonic()
        self.origin_utc = _utc_now()


class WavAudioRecorder(FrameProcessor):
    """Mirror Pipecat audio into a wall-clock aligned WAV track."""

    def __init__(self, frame_type: type[Frame], path: Path, label: str, clock: AudioDebugClock):
        super().__init__()
        self._frame_type = frame_type
        self.path = path
        self.label = label
        self.clock = clock
        self.bytes_written = 0
        self.frames_written = 0
        self.sample_rate: int | None = None
        self.num_channels: int | None = None
        self.samples_written = 0
        self._writer: wave.Wave_write | None = None
        self._disabled = False
        self._format_warning_logged = False
        self._write_warning_logged = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, self._frame_type):
            self.write_frame(frame)
        await self.push_frame(frame, direction)

    def write_frame(
        self,
        frame: InputAudioRawFrame | OutputAudioRawFrame,
        *,
        at_monotonic: float | None = None,
        sequential: bool = False,
    ) -> None:
        if self._disabled:
            return
        audio = getattr(frame, "audio", b"")
        if not audio:
            return
        try:
            if self.clock.origin_monotonic is None:
                self.clock.start(at_monotonic)
            writer = self._ensure_writer(frame)
            if not writer:
                return
            if not sequential and self.clock.origin_monotonic is not None:
                now = at_monotonic if at_monotonic is not None else time.monotonic()
                target_samples = max(0, int((now - self.clock.origin_monotonic) * int(self.sample_rate or 1)))
                if target_samples > self.samples_written:
                    missing = target_samples - self.samples_written
                    writer.writeframesraw(b"\x00\x00" * missing * int(self.num_channels or 1))
                    self.samples_written += missing
            writer.writeframesraw(audio)
            channels = max(1, int(self.num_channels or 1))
            samples = len(audio) // (_WAV_SAMPLE_WIDTH_BYTES * channels)
            self.samples_written += samples
            self.bytes_written += len(audio)
            self.frames_written += 1
        except Exception as err:
            self._disabled = True
            if not self._write_warning_logged:
                logger.warning("Audio debug {} recorder write failed: {}", self.label, err)
                self._write_warning_logged = True

    def close(self) -> None:
        if not self._writer:
            return
        try:
            self._writer.close()
        except Exception as err:
            logger.warning("Audio debug {} recorder close failed: {}", self.label, err)
        finally:
            self._writer = None

    def info(self) -> dict[str, Any]:
        duration = (self.samples_written / self.sample_rate) if self.sample_rate else 0.0
        return {
            "filename": self.path.name,
            "bytes": self.bytes_written,
            "frames": self.frames_written,
            "sample_rate": self.sample_rate,
            "num_channels": self.num_channels,
            "samples": self.samples_written,
            "duration_seconds": round(duration, 3),
        }

    def _ensure_writer(self, frame: InputAudioRawFrame | OutputAudioRawFrame):
        sample_rate = int(getattr(frame, "sample_rate", 0) or 0)
        num_channels = int(getattr(frame, "num_channels", 0) or 0)
        if sample_rate <= 0 or num_channels <= 0:
            return None
        if self._writer:
            if (sample_rate != self.sample_rate or num_channels != self.num_channels) and not self._format_warning_logged:
                logger.warning(
                    "Audio debug {} format changed from {}Hz/{}ch to {}Hz/{}ch; continuing original WAV",
                    self.label,
                    self.sample_rate,
                    self.num_channels,
                    sample_rate,
                    num_channels,
                )
                self._format_warning_logged = True
            return self._writer
        self.path.parent.mkdir(parents=True, exist_ok=True)
        writer = wave.open(str(self.path), "wb")
        writer.setnchannels(num_channels)
        writer.setsampwidth(_WAV_SAMPLE_WIDTH_BYTES)
        writer.setframerate(sample_rate)
        self._writer = writer
        self.sample_rate = sample_rate
        self.num_channels = num_channels
        return writer


@dataclass
class AudioDebugSession:
    id: str
    metadata_path: Path
    metadata: dict[str, Any]
    clock: AudioDebugClock
    input_recorder: WavAudioRecorder
    output_recorder: WavAudioRecorder
    raw_mic_recorder: WavAudioRecorder
    played_output_recorder: WavAudioRecorder
    mix_path: Path
    timeline: list[dict[str, Any]] = field(default_factory=list)
    capture_started: bool = False
    _closed: bool = False

    def start_capture(self, *, origin_monotonic: float | None = None, reason: str = "audio") -> None:
        if self.capture_started:
            return
        self.clock.start(origin_monotonic)
        self.capture_started = True
        self.metadata["capture_started_at"] = self.clock.origin_utc or _utc_now()
        self.metadata["capture_reason"] = reason
        self.record_event("capture_started", {"reason": reason})
        self.write_metadata()

    def record_raw_mic(
        self,
        frame: InputAudioRawFrame,
        *,
        at_monotonic: float | None = None,
        sequential: bool = False,
    ) -> None:
        if not self.capture_started:
            return
        self.raw_mic_recorder.write_frame(frame, at_monotonic=at_monotonic, sequential=sequential)

    def record_played_output(self, frame: OutputAudioRawFrame, *, at_monotonic: float | None = None) -> None:
        if not self.capture_started:
            return
        self.played_output_recorder.write_frame(frame, at_monotonic=at_monotonic)

    def record_event(self, event: str, data: dict[str, Any] | None = None) -> None:
        if not self.capture_started and event != "capture_started":
            return
        now_mono = time.monotonic()
        offset_ms = None
        if self.clock.origin_monotonic is not None:
            offset_ms = round((now_mono - self.clock.origin_monotonic) * 1000.0, 1)
        item = {
            "event": event,
            "at": _utc_now(),
            "offset_ms": offset_ms,
            "data": data or {},
        }
        self.timeline.append(item)
        # Keep metadata crash-tolerant without fsyncing every 20 ms audio frame.
        if len(self.timeline) <= 4 or event in {
            "wake", "stop", "provider_interruption", "pcm_gap", "first_assistant_audio",
            "provider_error", "tts_stopped", "session_closed"
        }:
            self.write_metadata()

    def write_metadata(self) -> None:
        self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {**self.metadata, "timeline": self.timeline, "retention_hours": _RETENTION_HOURS}
        tmp = self.metadata_path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, sort_keys=True, ensure_ascii=False)
            file.write("\n")
        tmp.replace(self.metadata_path)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.input_recorder.close()
        self.output_recorder.close()
        self.raw_mic_recorder.close()
        self.played_output_recorder.close()
        if not self.capture_started:
            self.metadata_path.unlink(missing_ok=True)
            return
        self.record_event("session_closed", {})
        self.metadata["finished_at"] = _utc_now()
        self.metadata["files"] = {
            "mic_raw": self.raw_mic_recorder.info(),
            "gemini_input": self.input_recorder.info(),
            "assistant_raw": self.output_recorder.info(),
            "assistant_played": self.played_output_recorder.info(),
        }
        self._build_mix()
        if self.mix_path.exists():
            self.metadata["files"]["mix_stereo"] = _file_info(self.mix_path)
        self.write_metadata()
        cleanup_audio_recordings()

    def _build_mix(self) -> None:
        mic = self.raw_mic_recorder.path if self.raw_mic_recorder.path.exists() else self.input_recorder.path
        played = (
            self.played_output_recorder.path
            if self.played_output_recorder.path.exists()
            else self.output_recorder.path
        )
        if not mic.exists() or not played.exists():
            return
        try:
            subprocess.run(
                [
                    "ffmpeg", "-loglevel", "error", "-y",
                    "-i", str(mic), "-i", str(played),
                    "-filter_complex",
                    (
                        f"[0:a]aresample={_MIX_RATE},aformat=sample_fmts=s16:channel_layouts=mono[mic];"
                        f"[1:a]aresample={_MIX_RATE},aformat=sample_fmts=s16:channel_layouts=mono[bot];"
                        "[mic][bot]amerge=inputs=2[out]"
                    ),
                    "-map", "[out]", "-ac", "2", str(self.mix_path),
                ],
                check=True,
                timeout=20,
            )
        except Exception as err:
            self.metadata["mix_error"] = str(err)
            logger.warning("Audio debug mix build failed for {}: {}", self.id, err)


def create_audio_debug_session(
    config: RuntimeConfig,
    flow: FlowConfig,
    provider_kind: str,
    realtime_model: str,
    conversation_session_id: str = "",
) -> AudioDebugSession | None:
    if not config.audio_debug_enabled:
        return None
    session_id = conversation_session_id or f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{_slug(flow.id)}_{secrets.token_hex(4)}"
    clock = AudioDebugClock()
    metadata = {
        "id": session_id,
        "started_at": _utc_now(),
        "conversation_session_id": conversation_session_id or None,
        "conversation_log_url": (
            f"api/assist/conversations/{conversation_session_id}" if conversation_session_id else None
        ),
        "flow_id": flow.id,
        "flow_name": flow.name,
        "provider": provider_kind,
        "model": realtime_model,
        "capture_started_at": None,
    }
    root = AUDIO_DEBUG_DIR
    session = AudioDebugSession(
        id=session_id,
        metadata_path=root / f"{session_id}.json",
        metadata=metadata,
        clock=clock,
        input_recorder=WavAudioRecorder(InputAudioRawFrame, root / f"{session_id}_gemini_input.wav", "gemini_input", clock),
        output_recorder=WavAudioRecorder(OutputAudioRawFrame, root / f"{session_id}_assistant_raw.wav", "assistant_raw", clock),
        raw_mic_recorder=WavAudioRecorder(InputAudioRawFrame, root / f"{session_id}_mic_raw.wav", "mic_raw", clock),
        played_output_recorder=WavAudioRecorder(OutputAudioRawFrame, root / f"{session_id}_assistant_played.wav", "assistant_played", clock),
        mix_path=root / f"{session_id}_mix_stereo.wav",
    )
    session.write_metadata()
    cleanup_audio_recordings()
    logger.info("Audio debug armed for session {} (24h TTL)", session_id)
    return session



def audio_debug_retention_hours() -> float:
    return _RETENTION_HOURS

def audio_debug_file_path(filename: str) -> Path:
    if not _SAFE_FILENAME.fullmatch(filename) or Path(filename).name != filename:
        raise ValueError("Invalid audio debug filename")
    path = (AUDIO_DEBUG_DIR / filename).resolve()
    root = AUDIO_DEBUG_DIR.resolve()
    try:
        path.relative_to(root)
    except ValueError as err:
        raise ValueError("Invalid audio debug filename") from err
    if path.suffix.lower() not in {".wav", ".json"}:
        raise ValueError("Invalid audio debug file type")
    return path


def clear_audio_recordings() -> None:
    if not AUDIO_DEBUG_DIR.exists():
        return
    for path in AUDIO_DEBUG_DIR.iterdir():
        if path.is_file() and path.suffix.lower() in {".json", ".wav", ".tmp"}:
            path.unlink(missing_ok=True)


def cleanup_audio_recordings(keep_sessions: int | None = None) -> None:
    """Delete all debug artifacts older than the configured wall-clock TTL."""
    if not AUDIO_DEBUG_DIR.exists():
        return
    cutoff = time.time() - _RETENTION_SECONDS
    records = _metadata_records(clean=False)
    protected: set[str] = set()
    for record in records:
        metadata_file = Path(str(record.get("_metadata_file") or ""))
        started = _parse_time(record.get("capture_started_at") or record.get("started_at"))
        if started is not None and started.timestamp() >= cutoff:
            protected.add(str(record.get("id") or metadata_file.stem))
            continue
        session_id = str(record.get("id") or metadata_file.stem)
        _delete_session_files(session_id, str(metadata_file) if metadata_file else None)
    # Remove stale orphan artifacts too.
    for path in AUDIO_DEBUG_DIR.iterdir():
        if not path.is_file() or path.suffix.lower() not in {".wav", ".json", ".tmp"}:
            continue
        if path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)


def list_audio_recordings() -> list[dict[str, Any]]:
    cleanup_audio_recordings()
    records = _metadata_records(clean=False)
    return [_public_record(record) for record in records if record.get("capture_started_at")]


def _metadata_records(*, clean: bool = False) -> list[dict[str, Any]]:
    if not AUDIO_DEBUG_DIR.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in AUDIO_DEBUG_DIR.glob("*.json"):
        try:
            with path.open("r", encoding="utf-8") as file:
                record = json.load(file)
        except Exception as err:
            logger.warning("Ignoring unreadable audio debug metadata {}: {}", path.name, err)
            continue
        record["_metadata_file"] = str(path)
        record.setdefault("id", path.stem)
        records.append(record)
    return sorted(records, key=lambda item: item.get("capture_started_at") or item.get("started_at") or "", reverse=True)


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    session_id = str(record.get("id") or "")
    def pub(suffix: str):
        return _public_file(AUDIO_DEBUG_DIR / f"{session_id}_{suffix}.wav")
    return {
        "id": session_id,
        "conversation_session_id": record.get("conversation_session_id"),
        "conversation_log_url": record.get("conversation_log_url"),
        "started_at": record.get("started_at"),
        "capture_started_at": record.get("capture_started_at"),
        "finished_at": record.get("finished_at"),
        "flow_id": record.get("flow_id"),
        "flow_name": record.get("flow_name"),
        "provider": record.get("provider"),
        "model": record.get("model"),
        "retention_hours": _RETENTION_HOURS,
        "mic_raw": pub("mic_raw"),
        "gemini_input": pub("gemini_input"),
        "assistant_raw": pub("assistant_raw"),
        "assistant_played": pub("assistant_played"),
        "mix_stereo": pub("mix_stereo"),
        "timeline": _public_file(AUDIO_DEBUG_DIR / f"{session_id}.json"),
    }


def _file_info(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {"filename": path.name, "size": stat.st_size}


def _public_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    stat = path.stat()
    return {
        "filename": path.name,
        "size": stat.st_size,
        "url": f"api/assist/debug/audio/{path.name}",
    }


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _delete_session_files(session_id: str, metadata_file: str | None = None) -> None:
    if not session_id:
        return
    for suffix in (
        ".json",
        "_mic_raw.wav",
        "_gemini_input.wav",
        "_assistant_raw.wav",
        "_assistant_played.wav",
        "_mix_stereo.wav",
        "_input.wav",
        "_output.wav",
    ):
        (AUDIO_DEBUG_DIR / f"{session_id}{suffix}").unlink(missing_ok=True)
    if metadata_file:
        Path(metadata_file).unlink(missing_ok=True)
