"""Speech-to-text entity for Pipecat Assist."""

from __future__ import annotations

import asyncio
import json
import math
import sys
from array import array
from collections.abc import AsyncIterable

import aiohttp

from homeassistant.components import stt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_FLOW_ID, CONF_TOKEN, CONF_URL, SUPPORTED_LANGUAGES

DEFAULT_SAMPLE_RATE = int(stt.AudioSampleRates.SAMPLERATE_16000)
DEFAULT_BIT_RATE = int(stt.AudioBitRates.BITRATE_16)
DEFAULT_CHANNELS = int(stt.AudioChannels.CHANNEL_MONO)
STREAM_TIMEOUT_SECONDS = 30.0
PCM_SPEECH_RMS_THRESHOLD = 0.012
PCM_MIN_SPEECH_SECONDS = 0.2
PCM_END_SILENCE_SECONDS = 1.1
PCM_INITIAL_SILENCE_SECONDS = 8.0
PCM_MAX_AUDIO_SECONDS = 25.0
WEBSOCKET_RESULT_TIMEOUT_SECONDS = 45.0


def _metadata_value(value) -> str:
    """Return the raw enum value Home Assistant exposes for speech metadata."""

    return str(getattr(value, "value", value) or "").lower()


def _pcm16_payload(chunk: bytes) -> bytes:
    """Return PCM payload, skipping a WAV header when HA includes one."""

    if chunk.startswith(b"RIFF"):
        data_index = chunk.find(b"data")
        if data_index >= 0 and len(chunk) >= data_index + 8:
            return chunk[data_index + 8 :]
    return chunk


def _pcm16_rms(chunk: bytes) -> float:
    """Return normalized RMS for little-endian 16-bit PCM audio."""

    payload = _pcm16_payload(chunk)
    length = len(payload) - (len(payload) % 2)
    if length <= 0:
        return 0.0
    samples = array("h")
    samples.frombytes(payload[:length])
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return 0.0
    stride = max(1, len(samples) // 4096)
    total = 0
    count = 0
    for sample in samples[::stride]:
        total += sample * sample
        count += 1
    return math.sqrt(total / max(1, count)) / 32768.0


def _chunk_seconds(chunk: bytes, sample_rate: int, bit_rate: int, channels: int) -> float:
    byte_rate = max(1, sample_rate * max(1, channels) * max(1, bit_rate // 8))
    return len(_pcm16_payload(chunk)) / byte_rate


def _can_detect_pcm_silence(metadata: stt.SpeechMetadata) -> bool:
    codec = _metadata_value(metadata.codec)
    return (
        (codec == "pcm" or codec.endswith(".pcm"))
        and int(metadata.bit_rate or DEFAULT_BIT_RATE) == 16
        and int(metadata.channel or DEFAULT_CHANNELS) == 1
    )


def _websocket_url(base_url: str, path: str) -> str:
    clean = base_url.rstrip("/")
    if clean.startswith("https://"):
        clean = f"wss://{clean[8:]}"
    elif clean.startswith("http://"):
        clean = f"ws://{clean[7:]}"
    return f"{clean}/{path.lstrip('/')}"


async def _collect_audio_stream(
    metadata: stt.SpeechMetadata,
    stream: AsyncIterable[bytes],
) -> bytes:
    """Collect HA Assist audio, ending early when PCM silence follows speech."""

    sample_rate = int(metadata.sample_rate or DEFAULT_SAMPLE_RATE)
    bit_rate = int(metadata.bit_rate or DEFAULT_BIT_RATE)
    channels = int(metadata.channel or DEFAULT_CHANNELS)
    use_pcm_vad = _can_detect_pcm_silence(metadata)
    chunks: list[bytes] = []
    total_seconds = 0.0
    speech_seconds = 0.0
    trailing_silence_seconds = 0.0

    try:
        async with asyncio.timeout(STREAM_TIMEOUT_SECONDS):
            async for chunk in stream:
                if not chunk:
                    continue
                chunks.append(chunk)
                if not use_pcm_vad:
                    continue

                seconds = _chunk_seconds(chunk, sample_rate, bit_rate, channels)
                total_seconds += seconds
                if _pcm16_rms(chunk) >= PCM_SPEECH_RMS_THRESHOLD:
                    speech_seconds += seconds
                    trailing_silence_seconds = 0.0
                elif speech_seconds >= PCM_MIN_SPEECH_SECONDS:
                    trailing_silence_seconds += seconds

                if (
                    speech_seconds >= PCM_MIN_SPEECH_SECONDS
                    and trailing_silence_seconds >= PCM_END_SILENCE_SECONDS
                ):
                    break
                if speech_seconds <= 0 and total_seconds >= PCM_INITIAL_SILENCE_SECONDS:
                    break
                if total_seconds >= PCM_MAX_AUDIO_SECONDS:
                    break
    except TimeoutError:
        pass

    return b"".join(chunks)


def _stream_tracker(metadata: stt.SpeechMetadata):
    sample_rate = int(metadata.sample_rate or DEFAULT_SAMPLE_RATE)
    bit_rate = int(metadata.bit_rate or DEFAULT_BIT_RATE)
    channels = int(metadata.channel or DEFAULT_CHANNELS)
    use_pcm_vad = _can_detect_pcm_silence(metadata)
    total_seconds = 0.0
    speech_seconds = 0.0
    trailing_silence_seconds = 0.0

    def should_stop(chunk: bytes) -> bool:
        nonlocal total_seconds, speech_seconds, trailing_silence_seconds
        if not use_pcm_vad:
            return False
        seconds = _chunk_seconds(chunk, sample_rate, bit_rate, channels)
        total_seconds += seconds
        if _pcm16_rms(chunk) >= PCM_SPEECH_RMS_THRESHOLD:
            speech_seconds += seconds
            trailing_silence_seconds = 0.0
        elif speech_seconds >= PCM_MIN_SPEECH_SECONDS:
            trailing_silence_seconds += seconds
        return (
            speech_seconds >= PCM_MIN_SPEECH_SECONDS
            and trailing_silence_seconds >= PCM_END_SILENCE_SECONDS
        ) or (
            speech_seconds <= 0 and total_seconds >= PCM_INITIAL_SILENCE_SECONDS
        ) or total_seconds >= PCM_MAX_AUDIO_SECONDS

    return should_stop


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Pipecat Assist STT entity."""

    async_add_entities([PipecatAssistSpeechToTextEntity(hass, entry)])


class PipecatAssistSpeechToTextEntity(stt.SpeechToTextEntity):
    """Speech-to-text bridge backed by the Pipecat Assist add-on."""

    _attr_has_entity_name = True
    _attr_name = "Pipecat Assist"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_stt"
        self._session = async_get_clientsession(hass)

    @property
    def supported_languages(self) -> list[str]:
        """Return supported languages."""

        return SUPPORTED_LANGUAGES

    @property
    def supported_formats(self) -> list[stt.AudioFormats]:
        """Return supported audio formats."""

        return [stt.AudioFormats.WAV]

    @property
    def supported_codecs(self) -> list[stt.AudioCodecs]:
        """Return supported audio codecs."""

        return [stt.AudioCodecs.PCM]

    @property
    def supported_bit_rates(self) -> list[stt.AudioBitRates]:
        """Return supported bit rates."""

        return [stt.AudioBitRates.BITRATE_16]

    @property
    def supported_sample_rates(self) -> list[stt.AudioSampleRates]:
        """Return supported sample rates."""

        return [
            stt.AudioSampleRates.SAMPLERATE_8000,
            stt.AudioSampleRates.SAMPLERATE_11000,
            stt.AudioSampleRates.SAMPLERATE_16000,
            stt.AudioSampleRates.SAMPLERATE_18900,
            stt.AudioSampleRates.SAMPLERATE_22000,
            stt.AudioSampleRates.SAMPLERATE_32000,
            stt.AudioSampleRates.SAMPLERATE_37800,
            stt.AudioSampleRates.SAMPLERATE_44100,
            stt.AudioSampleRates.SAMPLERATE_48000,
        ]

    @property
    def supported_channels(self) -> list[stt.AudioChannels]:
        """Return supported channels."""

        return [stt.AudioChannels.CHANNEL_MONO]

    async def _post_buffered_audio(
        self,
        metadata: stt.SpeechMetadata,
        stream: AsyncIterable[bytes],
        headers: dict[str, str],
        url: str,
    ) -> stt.SpeechResult:
        body = await _collect_audio_stream(metadata, stream)
        params = {}
        if flow_id := self._entry.data.get(CONF_FLOW_ID):
            params["flow_id"] = flow_id

        try:
            async with self._session.post(
                f"{url}/api/assist/stt",
                params=params,
                data=body,
                headers=headers,
            ) as response:
                if response.status >= 400:
                    try:
                        data = await response.json()
                        detail = data.get("detail", "Pipecat Assist STT failed.")
                    except (aiohttp.ClientError, ValueError):
                        detail = await response.text()
                    return stt.SpeechResult(
                        text=detail or "Pipecat Assist STT failed.",
                        result=stt.SpeechResultState.ERROR,
                    )
                data = await response.json()
        except aiohttp.ClientError as err:
            return stt.SpeechResult(text=str(err), result=stt.SpeechResultState.ERROR)

        return stt.SpeechResult(
            text=data.get("text") or "",
            result=stt.SpeechResultState.SUCCESS,
        )

    async def _stream_audio(
        self,
        metadata: stt.SpeechMetadata,
        stream: AsyncIterable[bytes],
        headers: dict[str, str],
        url: str,
        content_type: str,
        audio_format: str,
        audio_codec: str,
        sample_rate: int,
        bit_rate: int,
        channels: int,
    ) -> stt.SpeechResult:
        start_message = {
            "type": "start",
            "flow_id": self._entry.data.get(CONF_FLOW_ID) or None,
            "content_type": content_type,
            "metadata": {
                "format": audio_format,
                "codec": audio_codec,
                "sample_rate": sample_rate,
                "bit_rate": bit_rate,
                "channel": channels,
                "language": metadata.language,
            },
        }
        should_stop = _stream_tracker(metadata)
        timeout = aiohttp.ClientTimeout(total=STREAM_TIMEOUT_SECONDS + WEBSOCKET_RESULT_TIMEOUT_SECONDS)
        ws_url = _websocket_url(url, "/api/assist/stt/stream")

        async with self._session.ws_connect(
            ws_url,
            headers=headers,
            heartbeat=20,
            timeout=timeout,
        ) as websocket:
            await websocket.send_json(start_message)
            try:
                async with asyncio.timeout(STREAM_TIMEOUT_SECONDS):
                    async for chunk in stream:
                        if not chunk:
                            continue
                        await websocket.send_bytes(chunk)
                        if should_stop(chunk):
                            break
            except TimeoutError:
                pass
            await websocket.send_json({"type": "end"})

            try:
                async with asyncio.timeout(WEBSOCKET_RESULT_TIMEOUT_SECONDS):
                    async for message in websocket:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(message.data)
                            except ValueError:
                                continue
                            if data.get("type") == "final":
                                return stt.SpeechResult(
                                    text=data.get("text") or "",
                                    result=stt.SpeechResultState.SUCCESS,
                                )
                            if data.get("type") == "error":
                                return stt.SpeechResult(
                                    text=data.get("detail") or "Pipecat Assist STT failed.",
                                    result=stt.SpeechResultState.ERROR,
                                )
                        elif message.type in {
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        }:
                            break
            except TimeoutError:
                return stt.SpeechResult(text="", result=stt.SpeechResultState.SUCCESS)

        return stt.SpeechResult(
            text="",
            result=stt.SpeechResultState.SUCCESS,
        )

    async def async_process_audio_stream(
        self,
        metadata: stt.SpeechMetadata,
        stream: AsyncIterable[bytes],
    ) -> stt.SpeechResult:
        """Process an audio stream through the add-on STT bridge."""

        url = self._entry.data[CONF_URL].rstrip("/")
        token = self._entry.data.get(CONF_TOKEN)
        sample_rate = int(metadata.sample_rate or DEFAULT_SAMPLE_RATE)
        bit_rate = int(metadata.bit_rate or DEFAULT_BIT_RATE)
        channels = int(metadata.channel or DEFAULT_CHANNELS)
        audio_format = _metadata_value(metadata.format) or "wav"
        audio_codec = _metadata_value(metadata.codec) or "pcm"
        headers = {
            "Content-Type": f"audio/{audio_format}",
            "X-Speech-Content": (
                f"format={audio_format}; codec={audio_codec}; "
                f"sample_rate={sample_rate}; bit_rate={bit_rate}; "
                f"channel={channels}; language={metadata.language}"
            ),
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            return await self._stream_audio(
                metadata,
                stream,
                headers=headers,
                url=url,
                content_type=headers["Content-Type"],
                audio_format=audio_format,
                audio_codec=audio_codec,
                sample_rate=sample_rate,
                bit_rate=bit_rate,
                channels=channels,
            )
        except (aiohttp.ClientConnectorError, aiohttp.WSServerHandshakeError):
            return await self._post_buffered_audio(metadata, stream, headers, url)
        except (TimeoutError, aiohttp.ClientError) as err:
            return stt.SpeechResult(text=str(err), result=stt.SpeechResultState.ERROR)
