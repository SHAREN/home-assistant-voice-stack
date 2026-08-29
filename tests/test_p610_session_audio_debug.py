from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "addons/pipecat_assist_proxy/app/audio_debug.py"


def _load_module(data_dir: Path):
    loguru = types.ModuleType("loguru")
    loguru.logger = types.SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None)
    sys.modules["loguru"] = loguru

    frames = types.ModuleType("pipecat.frames.frames")
    class Frame: pass
    class InputAudioRawFrame(Frame):
        def __init__(self, audio: bytes, sample_rate: int, num_channels: int = 1):
            self.audio, self.sample_rate, self.num_channels = audio, sample_rate, num_channels
    class OutputAudioRawFrame(InputAudioRawFrame): pass
    frames.Frame = Frame
    frames.InputAudioRawFrame = InputAudioRawFrame
    frames.OutputAudioRawFrame = OutputAudioRawFrame
    sys.modules["pipecat"] = types.ModuleType("pipecat")
    sys.modules["pipecat.frames"] = types.ModuleType("pipecat.frames")
    sys.modules["pipecat.frames.frames"] = frames

    fp = types.ModuleType("pipecat.processors.frame_processor")
    class FrameProcessor:
        async def process_frame(self, frame, direction): pass
        async def push_frame(self, frame, direction): pass
    class FrameDirection: pass
    fp.FrameProcessor = FrameProcessor
    fp.FrameDirection = FrameDirection
    sys.modules["pipecat.processors"] = types.ModuleType("pipecat.processors")
    sys.modules["pipecat.processors.frame_processor"] = fp

    app = types.ModuleType("app")
    app.__path__ = []
    config = types.ModuleType("app.config")
    config.DATA_DIR = data_dir
    config.FlowConfig = object
    config.RuntimeConfig = object
    sys.modules["app"] = app
    sys.modules["app.config"] = config

    spec = importlib.util.spec_from_file_location("session_audio_debug_test_module", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader
    spec.loader.exec_module(module)
    return module, frames.InputAudioRawFrame, frames.OutputAudioRawFrame


def test_audio_debug_records_tracks_and_24h_ttl():
    with tempfile.TemporaryDirectory() as tmp:
        module, Input, Output = _load_module(Path(tmp))
        config = types.SimpleNamespace(audio_debug_enabled=True, audio_debug_keep_sessions=100)
        flow = types.SimpleNamespace(id="home-default", name="Home")
        session = module.create_audio_debug_session(config, flow, "gemini", "model", "conv-1")
        assert session is not None
        session.start_capture(origin_monotonic=time.monotonic(), reason="wake")
        mic = Input(b"\x01\x00" * 320, 16000, 1)
        out = Output(b"\x02\x00" * 480, 24000, 1)
        session.record_raw_mic(mic, sequential=True)
        session.input_recorder.write_frame(mic, sequential=True)
        session.output_recorder.write_frame(out)
        session.record_played_output(out)
        session.record_event("provider_interruption", {})
        session.close()

        root = Path(tmp) / "audio-debug"
        assert (root / "conv-1_mic_raw.wav").exists()
        assert (root / "conv-1_gemini_input.wav").exists()
        assert (root / "conv-1_assistant_raw.wav").exists()
        assert (root / "conv-1_assistant_played.wav").exists()
        metadata = json.loads((root / "conv-1.json").read_text(encoding="utf-8"))
        assert metadata["retention_hours"] == 24.0
        assert metadata["conversation_session_id"] == "conv-1"
        assert any(event["event"] == "provider_interruption" for event in metadata["timeline"])

        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat().replace("+00:00", "Z")
        metadata["started_at"] = old
        metadata["capture_started_at"] = old
        (root / "conv-1.json").write_text(json.dumps(metadata), encoding="utf-8")
        module.cleanup_audio_recordings()
        assert not list(root.glob("conv-1*"))


def test_main_wires_rich_debug_tracks():
    main = (ROOT / "addons/pipecat_assist_proxy/app/main.py").read_text(encoding="utf-8")
    docker = (ROOT / "addons/pipecat_assist_proxy/Dockerfile").read_text(encoding="utf-8")
    assert "record_raw_mic" in main
    assert "record_played_output" in main
    assert "provider_interruption" in main
    assert "audio-debug-24h-cleanup" in main
    assert "audio_debug_retention_hours" in main
    assert "COPY app/audio_debug.py /app/audio_debug.py" in docker
