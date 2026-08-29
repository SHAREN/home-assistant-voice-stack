#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import audioop
from fractions import Fraction
import json
import os
from pathlib import Path
import time
import uuid
import wave

import aiohttp
import av
from aiortc import AudioStreamTrack, RTCPeerConnection, RTCSessionDescription
from av import AudioFrame, AudioResampler

HA_HOST = os.getenv("PIPECAT_HA_HOST", "homeassistant.local")
STATUS_URL = os.getenv("PIPECAT_STATUS_URL", f"http://{HA_HOST}:7861/api/assist/status")
PROMPT_PATH = Path(
    os.getenv("PIPECAT_E2E_PROMPT", str(Path(__file__).with_name("e2e_prompt.mp3")))
)
OUTPUT_PATH = Path(
    os.getenv("PIPECAT_E2E_OUTPUT", str(Path(__file__).with_name("e2e_response.wav")))
)
SAMPLE_RATE = 48_000
FRAME_SAMPLES = 960  # 20 ms


def decode_prompt_pcm(path: Path) -> bytes:
    container = av.open(str(path))
    resampler = AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    pcm = bytearray()
    for frame in container.decode(audio=0):
        for converted in resampler.resample(frame):
            pcm.extend(converted.to_ndarray().tobytes())
    for converted in resampler.resample(None):
        pcm.extend(converted.to_ndarray().tobytes())
    container.close()
    if not pcm:
        raise RuntimeError("Prompt audio decoded to zero samples")
    return bytes(pcm)


class PromptAudioTrack(AudioStreamTrack):
    kind = "audio"

    def __init__(self, pcm: bytes, backend_ready: asyncio.Event) -> None:
        super().__init__()
        self._pcm = pcm
        self._backend_ready = backend_ready
        self._position = 0
        self._pts = 0
        self._clock_started: float | None = None
        self._ready_at_pts: int | None = None
        # Leave a short pause after the assistant has actually stopped speaking.
        self._pre_speech_samples = int(1.5 * SAMPLE_RATE)
        self.speech_started = asyncio.Event()
        self.speech_finished = asyncio.Event()

    async def recv(self) -> AudioFrame:
        if self._clock_started is None:
            self._clock_started = time.monotonic()

        due = self._clock_started + (self._pts / SAMPLE_RATE)
        delay = due - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

        payload = b"\x00" * (FRAME_SAMPLES * 2)

        if self._backend_ready.is_set():
            if self._ready_at_pts is None:
                self._ready_at_pts = self._pts
            if self._pts - self._ready_at_pts >= self._pre_speech_samples:
                if self._position < len(self._pcm):
                    self.speech_started.set()
                    end = min(self._position + FRAME_SAMPLES * 2, len(self._pcm))
                    chunk = self._pcm[self._position:end]
                    self._position = end
                    payload = chunk.ljust(FRAME_SAMPLES * 2, b"\x00")
                else:
                    self.speech_finished.set()

        frame = AudioFrame(format="s16", layout="mono", samples=FRAME_SAMPLES)
        frame.planes[0].update(payload)
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = Fraction(1, SAMPLE_RATE)
        self._pts += FRAME_SAMPLES
        return frame


async def wait_for(predicate, timeout: float, label: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(f"Timed out waiting for {label}")


async def main() -> None:
    if not PROMPT_PATH.is_file():
        raise FileNotFoundError(
            f"Voice prompt not found: {PROMPT_PATH}. "
            "Set PIPECAT_E2E_PROMPT to a Russian speech audio file."
        )
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    prompt_pcm = decode_prompt_pcm(PROMPT_PATH)
    prompt_seconds = len(prompt_pcm) / 2 / SAMPLE_RATE

    backend_ready = asyncio.Event()
    response_started = asyncio.Event()
    response_audio_seconds = 0.0
    response_peak_rms = 0
    received_frames = 0
    data_messages: list[str] = []
    bot_started_speaking = False
    remote_track_seen = False
    connection_states: list[str] = []

    loop = asyncio.get_running_loop()
    pc = RTCPeerConnection()
    input_track = PromptAudioTrack(prompt_pcm, backend_ready)
    pc.addTransceiver(input_track, direction="sendrecv")
    channel = pc.createDataChannel("signalling")

    output_wave = wave.open(str(OUTPUT_PATH), "wb")
    output_wave.setnchannels(1)
    output_wave.setsampwidth(2)
    output_wave.setframerate(SAMPLE_RATE)

    @channel.on("open")
    def on_channel_open() -> None:
        channel.send(
            json.dumps(
                {
                    "label": "rtvi-ai",
                    "id": uuid.uuid4().hex[:8],
                    "type": "client-ready",
                    "data": {
                        "version": "1.4.0",
                        "about": {
                            "library": "pipecat-e2e-test",
                            "library_version": "1",
                            "platform": "linux",
                        },
                    },
                }
            )
        )
        # Fallback only: normal start is synchronized by bot-tts-stopped below.
        loop.call_later(45.0, backend_ready.set)

    @channel.on("message")
    def on_channel_message(message: object) -> None:
        nonlocal bot_started_speaking
        text = str(message)
        data_messages.append(text[:1000])
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            return
        event_type = event.get("type")
        if event_type in {"bot-llm-started", "bot-tts-started", "bot-started-speaking"}:
            bot_started_speaking = True
        elif bot_started_speaking and event_type in {
            "bot-llm-stopped",
            "bot-tts-stopped",
            "bot-stopped-speaking",
        }:
            backend_ready.set()

    @pc.on("connectionstatechange")
    async def on_connection_state_change() -> None:
        connection_states.append(pc.connectionState)

    async def consume_audio(track) -> None:
        nonlocal response_audio_seconds, response_peak_rms, received_frames
        resampler = AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        try:
            while True:
                frame = await track.recv()
                for converted in resampler.resample(frame):
                    raw = converted.to_ndarray().tobytes()
                    if not raw:
                        continue
                    output_wave.writeframesraw(raw)
                    received_frames += 1
                    rms = audioop.rms(raw, 2)
                    if input_track.speech_finished.is_set():
                        response_peak_rms = max(response_peak_rms, rms)
                        if rms >= 120:
                            response_audio_seconds += converted.samples / SAMPLE_RATE
                            if response_audio_seconds >= 0.4:
                                response_started.set()
        except Exception:
            return

    @pc.on("track")
    def on_track(track) -> None:
        nonlocal remote_track_seen
        if track.kind == "audio":
            remote_track_seen = True
            asyncio.create_task(consume_audio(track))

    async with aiohttp.ClientSession() as session:
        async with session.get(STATUS_URL, timeout=10) as response:
            response.raise_for_status()
            status = await response.json()

        if not status.get("selected_flow_ready"):
            raise RuntimeError(f"Selected flow not ready: {status}")

        offer_url = status["runner"]["offer_url"]
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        await wait_for(lambda: pc.iceGatheringState == "complete", 10, "ICE gathering")

        payload = {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
            "request_data": {
                "source": "e2e_test",
                "client_id": f"e2e-{uuid.uuid4().hex}",
                "language": "ru",
                "flow_id": status["selected_flow_id"],
            },
        }
        async with session.post(offer_url, json=payload, timeout=30) as response:
            body = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"Offer failed HTTP {response.status}: {body}")
            answer = json.loads(body)

        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
        )

        await wait_for(lambda: pc.connectionState == "connected", 15, "WebRTC connection")
        await wait_for(lambda: channel.readyState == "open", 15, "RTVI data channel")
        await wait_for(lambda: input_track.speech_started.is_set(), 60, "prompt playback")
        await wait_for(lambda: input_track.speech_finished.is_set(), prompt_seconds + 8, "prompt completion")
        await wait_for(lambda: response_started.is_set(), 30, "non-silent Gemini response audio")

        # Keep recording long enough to detect continuity rather than a tiny acknowledgement.
        await asyncio.sleep(8)

    await pc.close()
    output_wave.close()

    summary = {
        "ok": (
            remote_track_seen
            and response_audio_seconds >= 1.0
            and response_peak_rms >= 120
            and "connected" in connection_states
        ),
        "flow": status["selected_flow_id"],
        "prompt_seconds": round(prompt_seconds, 3),
        "remote_track_seen": remote_track_seen,
        "response_non_silent_seconds": round(response_audio_seconds, 3),
        "response_peak_rms": response_peak_rms,
        "received_audio_frames": received_frames,
        "connection_states": connection_states,
        "data_messages_received": len(data_messages),
        "data_message_samples": data_messages[-12:],
        "response_wav": str(OUTPUT_PATH),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not summary["ok"]:
        raise SystemExit(2)


if __name__ == "__main__":
    asyncio.run(main())
