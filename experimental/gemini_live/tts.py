"""Text-to-Speech platform for Gemini Live."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Any

from homeassistant.components.tts import (
    TextToSpeechEntity,
    TtsAudioType,
)
from homeassistant.components.tts.entity import TTSAudioRequest, TTSAudioResponse
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, GEMINI_TURN_STORE_KEY, SUPPORTED_LANGUAGES
from .runtime import AudioStream
from .utils import pcm_to_wav, streaming_wav_header

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Gemini Live TTS platform."""
    async_add_entities([GeminiLiveTTS(config_entry)])


class GeminiLiveTTS(TextToSpeechEntity):
    """Gemini Live TTS Entity.

    This entity does NOT synthesise audio itself. Instead, it retrieves or
    streams the native audio from the Gemini Live turn handled by the STT
    stage of the same pipeline run.

    The conversation text is always delivered via the
    `gemini_live_conversation_entry` event fired by conversation.py. If no
    cached audio is available, a short silence lets the pipeline complete
    cleanly.
    """

    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the TTS entity."""
        self.entry = entry
        self._attr_name = "Gemini Live"
        self._attr_unique_id = f"{entry.entry_id}_tts"

    @property
    def default_language(self) -> str:
        """Return the default language."""
        return "en"

    @property
    def supported_languages(self) -> list[str]:
        """Return supported languages."""
        return SUPPORTED_LANGUAGES

    @property
    def supported_options(self) -> list[str]:
        """Return supported options."""
        return []

    async def async_get_tts_audio(
        self,
        message: str,
        language: str,
        options: dict[str, Any] | None = None,
    ) -> TtsAudioType:
        """Retrieve the cached audio response from hass.data."""
        entry_data = self.hass.data[DOMAIN][self.entry.entry_id]
        stored = entry_data[GEMINI_TURN_STORE_KEY].take_audio_entry(message)
        audio = stored.audio if stored is not None else None
        trace = stored.trace if stored is not None else None
        if trace is not None:
            trace.emit("tts_store_take", message_chars=len(message or ""))
        _LOGGER.warning(
            "TTS: async_get_tts_audio called | message_chars=%d | matched_audio=%s",
            len(message or ""),
            len(audio) if isinstance(audio, bytes) else type(audio).__name__,
        )
        if isinstance(audio, bytes):
            if trace is not None:
                trace.emit("tts_response_ready", audio_bytes=len(audio))
            return "wav", audio

        _LOGGER.debug(
            "TTS: No cached audio found; returning silence so the pipeline can finish."
        )

        return "wav", self._get_dummy_wav()

    async def async_stream_tts_audio(
        self,
        request: TTSAudioRequest,
    ) -> TTSAudioResponse:
        """Stream Gemini's native audio response as it arrives."""
        entry_data = self.hass.data[DOMAIN][self.entry.entry_id]
        turn_store = entry_data[GEMINI_TURN_STORE_KEY]
        try:
            initial_text = await anext(request.message_gen)
        except StopAsyncIteration:
            initial_text = ""

        streaming = turn_store.take_streaming_audio_entry(initial_text)
        audio = streaming.audio if streaming is not None else None
        trace = streaming.trace if streaming is not None else None
        message = initial_text

        if audio is None:
            message += "".join([chunk async for chunk in request.message_gen])
            stored = turn_store.take_audio_entry(message)
            audio = stored.audio if stored is not None else None
            trace = stored.trace if stored is not None else None
        if trace is not None:
            trace.emit("tts_store_take", message_chars=len(message))

        async def data_gen() -> AsyncGenerator[bytes]:
            if isinstance(audio, AudioStream):

                async def drain_message_stream() -> None:
                    async for _chunk in request.message_gen:
                        pass

                drain_task = asyncio.create_task(drain_message_stream())
                try:
                    yield streaming_wav_header()
                    first_chunk = True
                    async for chunk in audio.async_chunks():
                        if first_chunk and trace is not None:
                            first_chunk = False
                            trace.emit("tts_first_pcm_yield", pcm_bytes=len(chunk))
                        yield chunk
                finally:
                    await drain_task
                return
            if audio:
                if trace is not None:
                    trace.emit("tts_first_pcm_yield", audio_bytes=len(audio))
                yield audio
                return
            yield self._get_dummy_wav()

        _LOGGER.warning(
            "TTS: async_stream_tts_audio called | message_chars=%d | streaming=%s",
            len(message),
            isinstance(audio, AudioStream),
        )
        return TTSAudioResponse("wav", data_gen())

    def _get_dummy_wav(self) -> bytes:
        """Return 1 second of silence as 16kHz mono 16-bit PCM WAV."""
        # 16000 samples/sec * 2 bytes/sample * 1 sec = 32000 bytes of zero
        pcm_data = b"\x00" * 32000
        return pcm_to_wav(pcm_data, 16000)
