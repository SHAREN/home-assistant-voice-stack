"""Speech-to-Text platform for Gemini Live."""

import asyncio
import codecs
from collections.abc import AsyncIterable, Callable
from contextlib import asynccontextmanager
import datetime
import logging
import time
from uuid import uuid4
from typing import Any

from google import genai
from google.genai import types
from homeassistant.components.stt import (
    AudioBitRates,
    AudioChannels,
    AudioCodecs,
    AudioFormats,
    AudioSampleRates,
    SpeechMetadata,
    SpeechResult,
    SpeechResultState,
    SpeechToTextEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import chat_session, llm
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_API_KEY,
    CONF_DETAILED_LOGGING,
    CONF_ENCOURAGE_WEB_SEARCH,
    CONF_MODEL,
    CONF_P610_LIVE_TEXT,
    CONF_SYSTEM_INSTRUCTION,
    CONF_SHOW_TEXT,
    CONF_TRANSCRIBE_GEMINI,
    CONF_VOICE,
    DEFAULT_TRANSCRIBE_GEMINI,
    DEFAULT_ENCOURAGE_WEB_SEARCH,
    DEFAULT_SYSTEM_INSTRUCTION,
    DEFAULT_SHOW_TEXT,
    DEFAULT_P610_LIVE_TEXT,
    DOMAIN,
    GEMINI_LIVE_TTS_PLACEHOLDER,
    GEMINI_SESSION_MANAGER_KEY,
    GEMINI_TURN_STORE_KEY,
    SUPPORTED_LANGUAGES,
)
from .observer import TurnTrace
from .input_safety import (
    LOCAL_STOP_SENTINEL,
    is_engine_on_tool_call,
    is_local_stop_phrase,
)
from .runtime import (
    AudioStream,
    PipelineTurn,
    active_pipeline_conversation_id,
)
from .network_safety import initial_network_error_type
from .transcription_finalizer import TranscriptionFinalizer
from .utils import (
    analyze_pcm_metrics,
    build_latest_stt_metrics,
    resample_24k_to_16k,
    save_failed_stt_capture,
    save_latest_stt_metrics,
    set_detailed_logging,
)

_LOGGER = logging.getLogger(__name__)

# Target optimal chunk payload size (100ms of 16kHz 16-bit mono PCM = 3200 bytes)
OPTIMAL_STREAM_CHUNK_SIZE = 3200

# Schema keys supported by the Gemini Live function declaration format
_SUPPORTED_SCHEMA_KEYS = {
    "type",
    "format",
    "description",
    "nullable",
    "enum",
    "max_items",
    "min_items",
    "properties",
    "required",
    "items",
}

_SEARCH_TOOL_HINTS = ("search", "web", "google")

_SEARCH_TOOL_INSTRUCTION = (
    "Use the available web-search tool whenever the user asks for current, latest, "
    "recent, live, or otherwise time-sensitive external information, or when the "
    "answer may have changed since your training data. Also use it when the user "
    "explicitly asks you to search, look up, check online, or verify something. "
    "Do not guess current external facts when the search tool can verify them."
)

RESPONSE_INACTIVITY_TIMEOUT = 30.0
FAILED_STT_CAPTURE_DIR = "voice_debug/failed_stt"
LATEST_STT_METRICS_PATH = "voice_debug/latest_stt_metrics.json"
FAILED_STT_CAPTURE_KEEP = 30
FAILED_STT_CAPTURE_SAMPLE_RATE = 16000
FAILED_STT_SETTING_ENTITIES = {
    "mic_volume": "number.plantronics_p610_mic_volume",
    "mic_auto_gain": "number.plantronics_p610_mic_auto_gain",
    "mic_noise_suppression": "select.plantronics_p610_mic_noise_suppression",
    "wake_word_1_sensitivity": "number.plantronics_p610_wake_word_1_sensitivity",
    "microphone_mute": "switch.plantronics_p610_mute",
}

END_CONVERSATION_TOOL_NAME = "end_conversation"

_END_CONVERSATION_INSTRUCTION = (
    f"Call {END_CONVERSATION_TOOL_NAME} when the user clearly indicates that they "
    "are finished, says goodbye, or asks to end the conversation. Do not call it "
    "merely because you have finished answering the current request. "
    "If you call it, call it before producing any response audio. "
    "If the user's first request in a conversation is only 'stop', 'cancel', "
    "'silence', 'turn it "
    "off', or a similar short command, treat it first as a request to stop an "
    "actively ringing alarm or timer. Before ending the conversation, use the "
    "available Home Assistant tools to check for and stop the ringing alarm or "
    "timer. Do not call end_conversation instead of attempting that action. After "
    "the ringing alarm or timer has been stopped, or if none is ringing, call "
    f"{END_CONVERSATION_TOOL_NAME} so Home Assistant stops listening."
)

_END_CONVERSATION_TOOL = {
    "function_declarations": [
        {
            "name": END_CONVERSATION_TOOL_NAME,
            "description": (
                "End the current voice conversation so Home Assistant stops "
                "listening for a follow-up turn. Call only when the user indicates "
                "that the conversation is finished."
            ),
        }
    ]
}

SHOW_TEXT_TOOL_NAME = "show_text"

_SHOW_TEXT_INSTRUCTION = (
    "The user WILL NOT see the transcription of what you say. "
    "Instead, if you want to display something to the user to read, for example instructions, "
    "lists, links, code blocks, or details that are better written down for the user than read out, "
    f"then you must call the {SHOW_TEXT_TOOL_NAME} function. This is the only way the user "
    "will see any text from you."
)

_SHOW_TEXT_TOOL = {
    "function_declarations": [
        {
            "name": SHOW_TEXT_TOOL_NAME,
            "description": (
                "Display text or markdown to the user. Call this when you want to show written details, "
                "instructions, or formatted text that the user should read."
            ),
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "text": {
                        "type": "STRING",
                        "description": "The text or markdown formatted text to display to the user.",
                    }
                },
                "required": ["text"],
            },
        }
    ]
}



def _is_search_tool_name(name: str) -> bool:
    """Return whether a tool name indicates web-search capability."""
    lowered_name = name.lower()
    return any(hint in lowered_name for hint in _SEARCH_TOOL_HINTS)


def _is_connection_closed_ok(exc: Exception) -> bool:
    """Return true for websockets' normal-close exception without importing it."""
    return exc.__class__.__name__ == "ConnectionClosedOK"


class _GeminiInitialNetworkError(Exception):
    """Gemini Live could not open its initial transport connection."""

    def __init__(self, trace: TurnTrace) -> None:
        super().__init__("initial Gemini transport unavailable")
        self.trace = trace


@asynccontextmanager
async def _open_transcription_live_session(
    client: Any,
    model: str,
    live_config: dict[str, Any],
    *,
    trace: TurnTrace,
):
    """Open raw STT Live session and classify only pre-entry transport errors."""
    entered = False
    try:
        async with client.aio.live.connect(model=model, config=live_config) as session:
            entered = True
            trace.emit("gemini_connection_ready")
            yield session
    except Exception as exc:
        network_type = None if entered else initial_network_error_type(exc)
        if network_type is not None:
            trace.emit("offline_network_detected", transport=network_type)
            raise _GeminiInitialNetworkError(trace) from exc
        raise


@asynccontextmanager
async def _open_direct_live_session(
    session_manager: Any,
    conversation_id: str,
    client: Any,
    model: str,
    live_config: dict[str, Any],
    *,
    trace: TurnTrace,
):
    """Acquire one persistent direct Live session and classify initial transport errors."""
    entered = False
    try:
        async with session_manager.acquire(
            conversation_id,
            client,
            model,
            live_config,
        ) as session:
            entered = True
            trace.emit("gemini_connection_ready", mode="direct_live")
            yield session
    except Exception as exc:
        network_type = None if entered else initial_network_error_type(exc)
        if network_type is not None:
            trace.emit("offline_network_detected", transport=network_type)
            raise _GeminiInitialNetworkError(trace) from exc
        raise


# ---------------------------------------------------------------------------
# Schema / tool helpers
# ---------------------------------------------------------------------------

def _camel_to_snake(name: str) -> str:
    """Convert camelCase key to snake_case (matches official integration)."""
    return "".join(["_" + c.lower() if c.isupper() else c for c in name]).lstrip("_")


def _format_schema_for_gemini(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert a voluptuous-openapi schema dict to Gemini Live-compatible format."""
    if subschemas := schema.get("allOf"):
        for subschema in subschemas:
            if "type" in subschema:
                return _format_schema_for_gemini(subschema)
        return _format_schema_for_gemini(subschemas[0])

    result: dict[str, Any] = {}
    for key, val in schema.items():
        key = _camel_to_snake(key)
        if key not in _SUPPORTED_SCHEMA_KEYS:
            continue
        if key == "type":
            val = val.upper()
        elif key == "format":
            if schema.get("type") == "string" and val not in ("enum", "date-time"):
                continue
            if schema.get("type") == "number" and val not in ("float", "double"):
                continue
            if schema.get("type") == "integer" and val not in ("int32", "int64"):
                continue
            if schema.get("type") not in ("string", "number", "integer"):
                continue
        elif key == "items":
            val = _format_schema_for_gemini(val)
        elif key == "properties":
            val = {k: _format_schema_for_gemini(v) for k, v in val.items()}
        result[key] = val

    if result.get("enum") and result.get("type") != "STRING":
        result["type"] = "STRING"
        result["enum"] = [str(item) for item in result["enum"]]

    if result.get("type") == "OBJECT" and not result.get("properties"):
        result["properties"] = {"json": {"type": "STRING"}}
        result["required"] = []

    return result


def _format_tool_for_gemini_live(
    tool: llm.Tool,
    custom_serializer: Callable[[Any], Any] | None = None,
    encourage_web_search: bool = False,
) -> dict[str, Any]:
    """Convert an HA LLM Tool to a Gemini Live functionDeclaration dict."""
    try:
        from voluptuous_openapi import convert  # type: ignore[import]

        if tool.parameters.schema:
            raw_schema = convert(
                tool.parameters,
                custom_serializer=custom_serializer,
            )
            parameters: dict | None = _format_schema_for_gemini(raw_schema)
        else:
            parameters = None
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Could not convert schema for tool %s: %s", tool.name, exc)
        parameters = None

    decl: dict[str, Any] = {
        "name": tool.name,
        "description": tool.description or f"Execute {tool.name}",
    }
    if encourage_web_search and _is_search_tool_name(tool.name):
        decl["description"] = (
            f"{decl['description']} Use this tool for current, latest, recent, "
            "time-sensitive, or explicitly requested online information."
        )
    if parameters:
        decl["parameters"] = parameters
    return decl


def _format_tools_for_gemini_live(
    tools: list[llm.Tool],
    custom_serializer: Callable[[Any], Any] | None = None,
    encourage_web_search: bool = False,
) -> list[dict[str, Any]]:
    """Convert HA LLM tools to Gemini Live tool declarations."""
    return [
        {
            "function_declarations": [
                _format_tool_for_gemini_live(
                    tool,
                    custom_serializer,
                    encourage_web_search,
                )
            ]
        }
        for tool in tools
    ]


def _add_end_conversation_tool(
    tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add the integration-owned conversation completion callback."""
    return [*tools, _END_CONVERSATION_TOOL]


def _add_end_conversation_instruction(system_instruction: str) -> str:
    """Tell Gemini when to finish the Home Assistant conversation."""
    return f"{system_instruction}\n\n{_END_CONVERSATION_INSTRUCTION}"


def _add_show_text_tool(
    tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add the integration-owned show text callback."""
    return [*tools, _SHOW_TEXT_TOOL]


def _add_show_text_instruction(system_instruction: str) -> str:
    """Tell Gemini to use the show_text callback to show text to the user."""
    return f"{system_instruction}\n\n{_SHOW_TEXT_INSTRUCTION}"



def _add_search_tool_instruction(
    system_instruction: str,
    tools: list[llm.Tool],
    encourage_web_search: bool,
) -> str:
    """Tell Gemini when to use an exposed search-like Assist tool."""
    if not encourage_web_search or not any(
        _is_search_tool_name(tool.name) for tool in tools
    ):
        return system_instruction
    return f"{system_instruction}\n\n{_SEARCH_TOOL_INSTRUCTION}"


def _escape_decode(value: Any) -> Any:
    """Recursively escape-decode values returned by the Gemini SDK."""
    if isinstance(value, str):
        return codecs.escape_decode(bytes(value, "utf-8"))[0].decode("utf-8")
    if isinstance(value, list):
        return [_escape_decode(item) for item in value]
    if isinstance(value, dict):
        return {key: _escape_decode(item) for key, item in value.items()}
    return value


def _validate_tool_results(value: Any) -> Any:
    """Recursively convert non-json-serializable tool results."""
    if isinstance(value, (datetime.time, datetime.date)):
        return value.isoformat()
    if isinstance(value, list):
        return [_validate_tool_results(item) for item in value]
    if isinstance(value, dict):
        return {key: _validate_tool_results(item) for key, item in value.items()}
    return value


# ---------------------------------------------------------------------------
# PCM diagnostics helper
# ---------------------------------------------------------------------------

def _analyse_pcm(pcm: bytes, sample_rate: int = 16000) -> str:
    """Return a one-line diagnostic string for a raw 16-bit signed mono PCM buffer."""
    metrics = analyze_pcm_metrics(pcm, sample_rate)
    if metrics["label"] == "NO_AUDIO":
        return "0 bytes — no audio at all"
    return (
        f"{metrics['pcm_bytes']:,} bytes | "
        f"{round(metrics['duration_seconds'] * 1000)} ms | "
        f"RMS {metrics['rms']:.0f} ({metrics['rms_percent']:.1f}%) | "
        f"peak {metrics['peak']} ({metrics['peak_percent']:.1f}%) | "
        f"{metrics['label']}"
    )


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Gemini Live STT platform."""
    async_add_entities([GeminiLiveSTT(config_entry)])


# ---------------------------------------------------------------------------
# STT Entity
# ---------------------------------------------------------------------------

class GeminiLiveSTT(SpeechToTextEntity):
    """Gemini Live STT Entity."""

    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the STT entity."""
        self.entry = entry
        self._attr_name = "Gemini Live"
        self._attr_unique_id = f"{entry.entry_id}_stt"

    @property
    def name(self) -> str:
        return self._attr_name

    @property
    def unique_id(self) -> str:
        return self._attr_unique_id

    async def _async_run_direct_live_sdk(
        self,
        metadata: SpeechMetadata,
        stream: AsyncIterable[bytes],
        api_key: str,
        model: str,
        voice: str,
        custom_instruction: str,
        transcribe_gemini: bool,
        encourage_web_search: bool,
        show_text: bool,
        result_future: asyncio.Future[SpeechResult],
        trace_id: str,
    ) -> SpeechResult:
        """Run one direct Live audio turn with same-session tools and streaming PCM."""
        turn_id = trace_id
        show_text_content: str | None = None
        conversation_id = active_pipeline_conversation_id(self.hass, self.entity_id)
        entry_data = self.hass.data[DOMAIN][self.entry.entry_id]
        session_manager = entry_data[GEMINI_SESSION_MANAGER_KEY]
        session_manager.reset_conversation(conversation_id)
        turn_store = entry_data[GEMINI_TURN_STORE_KEY]
        trace = TurnTrace(
            self.hass,
            self.entry.entry_id,
            conversation_id,
            trace_id,
            include_text=bool(
                ({**self.entry.data, **self.entry.options}).get(
                    CONF_P610_LIVE_TEXT,
                    DEFAULT_P610_LIVE_TEXT,
                )
            ),
        )
        trace.emit("stt_start", language=metadata.language or "ru", mode="direct_live")

        active_chat_session = chat_session.current_session.get()
        if (
            active_chat_session is None
            or active_chat_session.conversation_id != conversation_id
        ):
            active_chat_session = self.hass.data.get(
                chat_session.DATA_CHAT_SESSION,
                {},
            ).get(conversation_id)
        if active_chat_session is not None:
            session_manager.register_chat_session(self.hass, active_chat_session)

        llm_api: llm.APIInstance | None = None
        ha_tools: list[llm.Tool] = []
        system_instruction = custom_instruction or DEFAULT_SYSTEM_INSTRUCTION
        try:
            llm_api = await llm.async_get_api(
                hass=self.hass,
                api_id=llm.LLM_API_ASSIST,
                llm_context=llm.LLMContext(
                    platform=DOMAIN,
                    context=Context(),
                    language=metadata.language or "ru",
                    assistant="conversation",
                    device_id=None,
                ),
            )
            ha_tools = llm_api.tools
            api_prompt = llm_api.api_prompt
            if custom_instruction:
                system_instruction = f"{custom_instruction}\n\n{api_prompt}"
            else:
                system_instruction = f"{DEFAULT_SYSTEM_INSTRUCTION}\n\n{api_prompt}"
            system_instruction = _add_search_tool_instruction(
                system_instruction,
                ha_tools,
                encourage_web_search,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "[turn=%s] HA Assist tools unavailable for direct Live turn: %s",
                turn_id,
                exc,
            )

        system_instruction = _add_end_conversation_instruction(system_instruction)
        if not transcribe_gemini and show_text:
            system_instruction = _add_show_text_instruction(system_instruction)
        gemini_tools = _add_end_conversation_tool(
            _format_tools_for_gemini_live(
                ha_tools,
                llm_api.custom_serializer,
                encourage_web_search,
            )
            if llm_api is not None
            else []
        )
        if not transcribe_gemini and show_text:
            gemini_tools = _add_show_text_tool(gemini_tools)

        client = await self.hass.async_add_executor_job(
            lambda: genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(
                    async_client_args={"proxy": "http://127.0.0.1:18091"}
                ),
            )
        )
        live_config: dict[str, Any] = {
            "response_modalities": ["AUDIO"],
            "speech_config": {
                "voice_config": {
                    "prebuilt_voice_config": {"voice_name": voice}
                }
            },
            "system_instruction": {"parts": [{"text": system_instruction}]},
            "input_audio_transcription": {},
            "realtime_input_config": {
                "turn_coverage": "TURN_INCLUDES_ONLY_ACTIVITY"
            },
            "tools": gemini_tools,
        }
        if transcribe_gemini:
            live_config["output_audio_transcription"] = {}

        _LOGGER.info(
            "[turn=%s] direct_live_start conversation=%s tool_declarations=%d",
            turn_id,
            conversation_id,
            sum(len(tool.get("function_declarations", [])) for tool in gemini_tools),
        )

        input_transcript_parts: list[str] = []
        output_transcript_parts: list[str] = []
        model_text_parts: list[str] = []
        input_pcm = bytearray()
        response_audio_stream = AudioStream()
        first_audio = asyncio.Event()
        published = asyncio.Event()
        audio_sent = False
        response_audio_bytes = 0
        response_audio_chunks = 0
        last_response_activity = time.monotonic()
        first_input_emitted = False
        first_response_text_emitted = False
        final_transcript_emitted = False

        async with _open_direct_live_session(
            session_manager,
            conversation_id,
            client,
            model,
            live_config,
            trace=trace,
        ) as session:

            async def send_audio() -> None:
                nonlocal audio_sent
                first_chunk = True
                audio_buffer = bytearray()
                source_chunks = 0
                dispatched_blocks = 0
                previous_chunk_at: float | None = None
                max_source_gap_ms = 0.0
                async for chunk in stream:
                    if not chunk:
                        continue
                    if first_audio.is_set():
                        break
                    source_chunks += 1
                    chunk_at = time.monotonic()
                    if previous_chunk_at is not None:
                        max_source_gap_ms = max(
                            max_source_gap_ms,
                            (chunk_at - previous_chunk_at) * 1000,
                        )
                    previous_chunk_at = chunk_at
                    if first_chunk:
                        first_chunk = False
                        if chunk[:4] == b"RIFF":
                            data_offset = chunk.find(b"data")
                            if data_offset != -1:
                                chunk = chunk[data_offset + 8 :]
                    audio_buffer.extend(chunk)
                    while len(audio_buffer) >= OPTIMAL_STREAM_CHUNK_SIZE:
                        dispatch_chunk = bytes(
                            audio_buffer[:OPTIMAL_STREAM_CHUNK_SIZE]
                        )
                        del audio_buffer[:OPTIMAL_STREAM_CHUNK_SIZE]
                        input_pcm.extend(dispatch_chunk)
                        await session.send_realtime_input(
                            audio=types.Blob(
                                data=dispatch_chunk,
                                mime_type="audio/pcm;rate=16000",
                            )
                        )
                        audio_sent = True
                        dispatched_blocks += 1
                        if dispatched_blocks == 1:
                            trace.emit("core_audio_first", pcm_bytes=len(dispatch_chunk))

                if audio_buffer and not first_audio.is_set():
                    dispatch_chunk = bytes(audio_buffer)
                    input_pcm.extend(dispatch_chunk)
                    await session.send_realtime_input(
                        audio=types.Blob(
                            data=dispatch_chunk,
                            mime_type="audio/pcm;rate=16000",
                        )
                    )
                    audio_sent = True
                    dispatched_blocks += 1
                    if dispatched_blocks == 1:
                        trace.emit("core_audio_first", pcm_bytes=len(dispatch_chunk))

                if audio_sent and not first_audio.is_set():
                    await session.send_realtime_input(audio_stream_end=True)
                    trace.emit(
                        "audio_stream_end",
                        pcm_bytes=len(input_pcm),
                        blocks=dispatched_blocks,
                        source_chunks=source_chunks,
                        max_source_gap_ms=round(max_source_gap_ms, 1),
                        duration_ms=round(len(input_pcm) / 32, 1),
                    )
                _LOGGER.info(
                    "[turn=%s] direct_live_audio_sent blocks=%d metrics=%s",
                    turn_id,
                    dispatched_blocks,
                    _analyse_pcm(bytes(input_pcm)),
                )

            async def receive_responses() -> None:
                nonlocal show_text_content, response_audio_bytes
                nonlocal response_audio_chunks, last_response_activity
                nonlocal first_input_emitted, first_response_text_emitted
                nonlocal final_transcript_emitted
                async for response in session.receive():
                    last_response_activity = time.monotonic()
                    if response.tool_call:
                        function_responses = []
                        for call in response.tool_call.function_calls or []:
                            tool_name = call.name or ""
                            tool_args = _escape_decode(call.args or {})
                            trace.emit("tool_call_boundary", tool_name=tool_name)
                            if tool_name == END_CONVERSATION_TOOL_NAME:
                                if published.is_set():
                                    tool_result = {
                                        "error": "end_conversation_must_precede_audio"
                                    }
                                    trace.emit("late_end_ignored")
                                else:
                                    session_manager.complete_conversation(
                                        conversation_id
                                    )
                                    tool_result = {
                                        "success": True,
                                        "conversation_ended": True,
                                    }
                            elif tool_name == SHOW_TEXT_TOOL_NAME:
                                show_text_content = tool_args.get("text")
                                tool_result = {"success": True, "displayed": True}
                            elif is_engine_on_tool_call(tool_name, tool_args):
                                _LOGGER.error(
                                    "[turn=%s] blocked engine ON in direct Live tool path",
                                    turn_id,
                                )
                                trace.emit("engine_on_blocked")
                                tool_result = {
                                    "error": "engine_on_not_available_in_generic_voice_tools"
                                }
                            elif llm_api is not None:
                                try:
                                    tool_result = await llm_api.async_call_tool(
                                        llm.ToolInput(
                                            tool_name=tool_name,
                                            tool_args=tool_args,
                                        )
                                    )
                                except Exception as err:  # noqa: BLE001
                                    _LOGGER.error(
                                        "[turn=%s] direct Live tool failed name=%s type=%s",
                                        turn_id,
                                        tool_name,
                                        type(err).__name__,
                                    )
                                    tool_result = {"error": type(err).__name__}
                            else:
                                tool_result = {"error": "HA LLM API not available"}
                            function_responses.append(
                                types.FunctionResponse(
                                    name=tool_name,
                                    id=call.id,
                                    response=_validate_tool_results(tool_result),
                                )
                            )
                        if function_responses:
                            await session.send_tool_response(
                                function_responses=function_responses
                            )

                    content = response.server_content
                    if not content:
                        continue

                    transcription = content.input_transcription
                    if transcription and transcription.text:
                        transcript_part = transcription.text
                        input_transcript_parts.append(transcript_part)
                        trace.emit(
                            "transcription_chunk",
                            text=transcript_part,
                            revision=len(input_transcript_parts),
                            chunk_chars=len(transcript_part),
                        )
                        trace.emit(
                            "last_transcript_update",
                            text="".join(input_transcript_parts),
                            revision=len(input_transcript_parts),
                        )
                        if not first_input_emitted:
                            first_input_emitted = True
                            trace.emit(
                                "first_input_transcription",
                                text=transcript_part,
                            )
                    if transcription and bool(
                        getattr(transcription, "finished", False)
                    ):
                        final_transcript_emitted = True
                        trace.emit(
                            "final_transcript",
                            text="".join(input_transcript_parts).strip(),
                            authority="observer_only",
                        )

                    if content.model_turn:
                        for part in content.model_turn.parts or []:
                            if part.text:
                                model_text_parts.append(part.text)
                            if part.inline_data and part.inline_data.data:
                                raw_chunk = part.inline_data.data
                                pcm_chunk = resample_24k_to_16k(raw_chunk)
                                if not pcm_chunk:
                                    continue
                                response_audio_stream.add_chunk(pcm_chunk)
                                response_audio_chunks += 1
                                response_audio_bytes += len(pcm_chunk)
                                if response_audio_chunks == 1:
                                    trace.emit(
                                        "first_response_audio",
                                        pcm_bytes=len(pcm_chunk),
                                        mode="direct_live",
                                    )
                                    first_audio.set()

                    output_transcription = content.output_transcription
                    if output_transcription and output_transcription.text:
                        output_part = output_transcription.text
                        output_transcript_parts.append(output_part)
                        trace.emit("assistant_delta", text=output_part)
                        if not first_response_text_emitted:
                            first_response_text_emitted = True
                            trace.emit("first_response_text", text=output_part)

                    if content.turn_complete:
                        trace.emit(
                            "turn_complete_received",
                            audio_chunks=response_audio_chunks,
                            mode="direct_live",
                        )
                        if "native-audio" in (model or "") and not first_audio.is_set():
                            continue
                        if not output_transcript_parts and model_text_parts:
                            fallback_text = "".join(model_text_parts)
                            trace.emit("assistant_delta", text=fallback_text)
                            if not first_response_text_emitted:
                                first_response_text_emitted = True
                                trace.emit(
                                    "first_response_text",
                                    text=fallback_text,
                                    reason="model_text_fallback",
                                )
                        if not final_transcript_emitted and input_transcript_parts:
                            final_transcript_emitted = True
                            trace.emit(
                                "final_transcript",
                                text="".join(input_transcript_parts).strip(),
                                authority="observer_only",
                                reason="turn_complete_fallback",
                            )
                        break

            async def publish_streaming_turn() -> None:
                await first_audio.wait()
                user_text = (
                    "".join(input_transcript_parts).strip()
                    or f"{GEMINI_LIVE_TTS_PLACEHOLDER} input {turn_id}"
                )
                if is_local_stop_phrase(user_text):
                    session_manager.complete_conversation(conversation_id)
                    for task in (send_task, receive_task):
                        if not task.done():
                            task.cancel()
                    turn_store.add_trace(
                        conversation_id,
                        LOCAL_STOP_SENTINEL,
                        trace,
                    )
                    trace.emit("local_stop", source="direct_live_transcript")
                    published.set()
                    if not result_future.done():
                        result_future.set_result(
                            SpeechResult(
                                LOCAL_STOP_SENTINEL,
                                SpeechResultState.SUCCESS,
                            )
                        )
                    return
                if not transcribe_gemini and show_text and show_text_content:
                    tts_message = show_text_content
                else:
                    tts_message = f"{GEMINI_LIVE_TTS_PLACEHOLDER} {turn_id}"
                turn_store.add_voice_turn(
                    PipelineTurn(
                        conversation_id=conversation_id,
                        user_text=user_text,
                        assistant_text=tts_message,
                        audio=response_audio_stream,
                        assistant_text_stream=None,
                    )
                )
                turn_store.add_trace(conversation_id, user_text, trace)
                trace.emit("turnstore_ready", streaming=True, mode="direct_live")
                published.set()
                if not result_future.done():
                    result_future.set_result(
                        SpeechResult(user_text, SpeechResultState.SUCCESS)
                    )

            send_task = asyncio.create_task(send_audio())
            receive_task = asyncio.create_task(receive_responses())
            publish_task = asyncio.create_task(publish_streaming_turn())

            async def cancel_sender_on_reply() -> None:
                await first_audio.wait()
                if not send_task.done():
                    send_task.cancel()

            cancel_sender_task = asyncio.create_task(cancel_sender_on_reply())
            try:
                done, _ = await asyncio.wait(
                    (send_task, receive_task),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if receive_task in done:
                    if not send_task.done():
                        send_task.cancel()
                else:
                    send_result = await asyncio.gather(
                        send_task,
                        return_exceptions=True,
                    )
                    if send_result and isinstance(send_result[0], Exception):
                        raise send_result[0]
                    while not receive_task.done():
                        remaining = RESPONSE_INACTIVITY_TIMEOUT - (
                            time.monotonic() - last_response_activity
                        )
                        if remaining <= 0:
                            receive_task.cancel()
                            break
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(receive_task),
                                timeout=remaining,
                            )
                        except TimeoutError:
                            continue
                await asyncio.gather(send_task, receive_task, return_exceptions=True)
                if first_audio.is_set():
                    await publish_task
                else:
                    publish_task.cancel()
            finally:
                for task in (
                    send_task,
                    receive_task,
                    publish_task,
                    cancel_sender_task,
                ):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(
                    send_task,
                    receive_task,
                    publish_task,
                    cancel_sender_task,
                    return_exceptions=True,
                )
                response_audio_stream.finish()

        input_transcript = "".join(input_transcript_parts).strip()
        response_text = "".join(output_transcript_parts) or "".join(model_text_parts)
        settings: dict[str, str | None] = {}
        for setting_name, entity_id in FAILED_STT_SETTING_ENTITIES.items():
            state = self.hass.states.get(entity_id)
            settings[setting_name] = state.state if state is not None else None
        latest_metrics = build_latest_stt_metrics(
            turn_id=turn_id,
            conversation_id=conversation_id,
            input_transcript=input_transcript,
            response_audio_received=first_audio.is_set(),
            response_audio_bytes=response_audio_bytes,
            response_text=response_text,
            input_audio_sent=audio_sent,
            input_pcm=bytes(input_pcm),
            settings=settings,
        )
        try:
            await self.hass.async_add_executor_job(
                save_latest_stt_metrics,
                self.hass.config.path(LATEST_STT_METRICS_PATH),
                latest_metrics,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("[turn=%s] Failed to save latest STT metrics", turn_id)

        if first_audio.is_set():
            trace.emit(
                "direct_live_complete",
                audio_chunks=response_audio_chunks,
                audio_bytes=response_audio_bytes,
            )
            return SpeechResult(
                input_transcript or f"{GEMINI_LIVE_TTS_PLACEHOLDER} input {turn_id}",
                SpeechResultState.SUCCESS,
            )

        if input_pcm:
            capture_metadata = {
                "turn_id": turn_id,
                "conversation_id": conversation_id,
                "reason": "no_direct_live_response_audio",
                "model": model,
                "partial_input_transcript": "",
                "audio_analysis": _analyse_pcm(bytes(input_pcm)),
                "settings": settings,
            }
            try:
                capture_dir = self.hass.config.path(FAILED_STT_CAPTURE_DIR)
                await self.hass.async_add_executor_job(
                    save_failed_stt_capture,
                    capture_dir,
                    bytes(input_pcm),
                    capture_metadata,
                    FAILED_STT_CAPTURE_KEEP,
                    FAILED_STT_CAPTURE_SAMPLE_RATE,
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception("[turn=%s] Failed to save direct Live failure", turn_id)
        trace.emit("stt_failed", reason="no_direct_live_response_audio")
        return SpeechResult(None, SpeechResultState.ERROR)

    async def _async_run_audio_stream_sdk(
        self,
        metadata: SpeechMetadata,
        stream: AsyncIterable[bytes],
        api_key: str,
        model: str,
        voice: str,
        custom_instruction: str,
        transcribe_gemini: bool,
        encourage_web_search: bool,
        show_text: bool,
        result_future: asyncio.Future[SpeechResult],
        trace_id: str,
    ) -> SpeechResult:
        """Transcribe raw audio in an ephemeral session that exposes no tools."""
        del custom_instruction, transcribe_gemini, encourage_web_search, show_text
        turn_id = uuid4().hex[:8]
        started_at = time.monotonic()
        conversation_id = active_pipeline_conversation_id(self.hass, self.entity_id)
        entry_data = self.hass.data[DOMAIN][self.entry.entry_id]
        session_manager = entry_data[GEMINI_SESSION_MANAGER_KEY]
        session_manager.reset_conversation(conversation_id)
        trace = TurnTrace(
            self.hass,
            self.entry.entry_id,
            conversation_id,
            trace_id,
            include_text=bool(
                ({**self.entry.data, **self.entry.options}).get(
                    CONF_P610_LIVE_TEXT,
                    DEFAULT_P610_LIVE_TEXT,
                )
            ),
        )
        trace.emit("stt_start", language=metadata.language or "ru")

        _LOGGER.warning(
            "[turn=%s] transcription-only STT start model=%s language=%s",
            turn_id,
            model,
            metadata.language or "ru",
        )

        client = await self.hass.async_add_executor_job(
            lambda: genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(
                    async_client_args={"proxy": "http://127.0.0.1:18091"}
                ),
            )
        )
        live_config: dict[str, Any] = {
            "response_modalities": ["AUDIO"],
            "speech_config": {
                "voice_config": {
                    "prebuilt_voice_config": {"voice_name": voice}
                }
            },
            "system_instruction": {
                "parts": [
                    {
                        "text": (
                            "Transcribe the user's speech exactly. Do not answer, "
                            "interpret, call functions, or perform actions."
                        )
                    }
                ]
            },
            "input_audio_transcription": {},
            "realtime_input_config": {
                "turn_coverage": "TURN_INCLUDES_ONLY_ACTIVITY"
            },
        }
        _LOGGER.warning(
            "[turn=%s] transcription-only config prepared has_tools=%s",
            turn_id,
            "tools" in live_config,
        )

        finalizer = TranscriptionFinalizer()
        input_pcm_for_failed_capture = bytearray()
        audio_sent = False
        unexpected_tool_call = False
        receive_failed = False

        async with _open_transcription_live_session(
            client,
            model,
            live_config,
            trace=trace,
        ) as session:

            async def send_audio() -> None:
                nonlocal audio_sent
                first_chunk = True
                audio_buffer = bytearray()
                chunk_count = 0
                first_audio_emitted = False
                source_chunk_count = 0
                max_source_gap_ms = 0.0
                previous_source_chunk_at: float | None = None
                async for chunk in stream:
                    if not chunk:
                        continue
                    source_chunk_count += 1
                    source_chunk_at = time.monotonic()
                    if previous_source_chunk_at is not None:
                        max_source_gap_ms = max(
                            max_source_gap_ms,
                            (source_chunk_at - previous_source_chunk_at) * 1000,
                        )
                    previous_source_chunk_at = source_chunk_at
                    if first_chunk:
                        first_chunk = False
                        if chunk[:4] == b"RIFF":
                            data_offset = chunk.find(b"data")
                            if data_offset != -1:
                                chunk = chunk[data_offset + 8 :]
                    audio_buffer.extend(chunk)
                    while len(audio_buffer) >= OPTIMAL_STREAM_CHUNK_SIZE:
                        dispatch_chunk = bytes(
                            audio_buffer[:OPTIMAL_STREAM_CHUNK_SIZE]
                        )
                        del audio_buffer[:OPTIMAL_STREAM_CHUNK_SIZE]
                        input_pcm_for_failed_capture.extend(dispatch_chunk)
                        await session.send_realtime_input(
                            audio=types.Blob(
                                data=dispatch_chunk,
                                mime_type="audio/pcm;rate=16000",
                            )
                        )
                        audio_sent = True
                        chunk_count += 1
                        if not first_audio_emitted:
                            first_audio_emitted = True
                            trace.emit("core_audio_first", pcm_bytes=len(dispatch_chunk))

                if audio_buffer:
                    dispatch_chunk = bytes(audio_buffer)
                    input_pcm_for_failed_capture.extend(dispatch_chunk)
                    await session.send_realtime_input(
                        audio=types.Blob(
                            data=dispatch_chunk,
                            mime_type="audio/pcm;rate=16000",
                        )
                    )
                    audio_sent = True
                    chunk_count += 1
                    if not first_audio_emitted:
                        first_audio_emitted = True
                        trace.emit("core_audio_first", pcm_bytes=len(dispatch_chunk))

                if audio_sent:
                    finalizer.mark_audio_stream_end_pending()
                    await session.send_realtime_input(audio_stream_end=True)
                    finalizer.mark_audio_stream_end()
                    trace.emit(
                        "audio_stream_end",
                        pcm_bytes=len(input_pcm_for_failed_capture),
                        blocks=chunk_count,
                        source_chunks=source_chunk_count,
                        max_source_gap_ms=round(max_source_gap_ms, 1),
                        duration_ms=round(
                            len(input_pcm_for_failed_capture) / 32,
                            1,
                        ),
                    )
                _LOGGER.warning(
                    "[turn=%s] transcription-only audio sent blocks=%d metrics=%s",
                    turn_id,
                    chunk_count,
                    _analyse_pcm(bytes(input_pcm_for_failed_capture)),
                )

            async def receive_transcript() -> None:
                nonlocal receive_failed, unexpected_tool_call
                try:
                    async for response in session.receive():
                        if response.tool_call:
                            unexpected_tool_call = True
                            finalizer.fail("unexpected_tool_call")
                            _LOGGER.error(
                                "[turn=%s] rejected unexpected tool_call in transcription-only phase",
                                turn_id,
                            )
                            return

                        content = response.server_content
                        if not content:
                            continue
                        transcription = content.input_transcription
                        if transcription and transcription.text:
                            transcript_part = transcription.text
                            first_chunk = finalizer.revision == 0
                            if finalizer.add_transcript_chunk(transcript_part):
                                trace.emit(
                                    "transcription_chunk",
                                    text=transcript_part,
                                    revision=finalizer.revision,
                                    chunk_chars=len(transcript_part),
                                )
                                trace.emit(
                                    "last_transcript_update",
                                    text=finalizer.transcript,
                                    revision=finalizer.revision,
                                )
                            if first_chunk:
                                trace.emit(
                                    "first_input_transcription",
                                    text=transcript_part,
                                )
                            _LOGGER.info(
                                "[turn=%s] transcription_chunk revision=%d chars=%d finished=%s",
                                turn_id,
                                finalizer.revision,
                                len(transcript_part),
                                bool(getattr(transcription, "finished", False)),
                            )

                        if transcription and bool(
                            getattr(transcription, "finished", False)
                        ):
                            after_audio_end = finalizer.audio_stream_end_at is not None
                            trace.emit(
                                "input_transcription_finished",
                                after_audio_end=after_audio_end,
                                audio_end_pending=finalizer.audio_stream_end_pending,
                                revision=finalizer.revision,
                            )
                            if finalizer.mark_provider_finished():
                                return
                            if finalizer.provider_finished_received:
                                _LOGGER.info(
                                    "[turn=%s] deferred transcription.finished while audio_stream_end send is in flight",
                                    turn_id,
                                )
                                return
                            _LOGGER.warning(
                                "[turn=%s] ignored transcription.finished while source audio is still open",
                                turn_id,
                            )
                        if content.turn_complete:
                            after_audio_end = finalizer.audio_stream_end_at is not None
                            trace.emit(
                                "turn_complete_received",
                                after_audio_end=after_audio_end,
                                revision=finalizer.revision,
                            )
                            if finalizer.mark_turn_complete():
                                return
                            if finalizer.turn_complete_received:
                                _LOGGER.info(
                                    "[turn=%s] deferred turnComplete while audio_stream_end send is in flight",
                                    turn_id,
                                )
                                return
                            _LOGGER.warning(
                                "[turn=%s] ignored early turnComplete before audio_stream_end",
                                turn_id,
                            )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    receive_failed = True
                    finalizer.fail("incomplete_transcription")
                    if _is_connection_closed_ok(exc):
                        _LOGGER.warning(
                            "[turn=%s] transcription-only websocket closed before terminal transcript",
                            turn_id,
                        )
                    else:
                        _LOGGER.exception(
                            "[turn=%s] transcription-only receive failed: %s",
                            turn_id,
                            exc,
                        )
                else:
                    if not finalizer.done:
                        receive_failed = True
                        finalizer.fail("incomplete_transcription")

            send_task = asyncio.create_task(send_audio())
            receive_task = asyncio.create_task(receive_transcript())
            try:
                await send_task
                if not audio_sent:
                    finalizer.fail("no_audio_sent")
                if not finalizer.done:
                    try:
                        await asyncio.wait_for(
                            finalizer.event.wait(),
                            timeout=RESPONSE_INACTIVITY_TIMEOUT,
                        )
                    except TimeoutError:
                        receive_failed = True
                        finalizer.fail("incomplete_transcription")
                        _LOGGER.warning(
                            "[turn=%s] transcription-only receive timed out",
                            turn_id,
                        )
                if not receive_task.done():
                    receive_task.cancel()
                await asyncio.gather(receive_task, return_exceptions=True)
            finally:
                for task in (send_task, receive_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(send_task, receive_task, return_exceptions=True)

        input_transcript = finalizer.transcript
        if finalizer.final_reason is not None:
            trace.emit(
                "stt_finalize_reason",
                reason=finalizer.final_reason,
                revision=finalizer.revision,
            )
            trace.emit(
                "stt_tail_wait_ms",
                value=round(finalizer.tail_wait_ms or 0.0, 1),
            )
            _LOGGER.info(
                "[turn=%s] stt_finalize_reason=%s stt_tail_wait_ms=%.1f revision=%d turn_complete_received=%s",
                turn_id,
                finalizer.final_reason,
                finalizer.tail_wait_ms or 0.0,
                finalizer.revision,
                finalizer.turn_complete_received,
            )
        settings: dict[str, str | None] = {}
        for setting_name, entity_id in FAILED_STT_SETTING_ENTITIES.items():
            state = self.hass.states.get(entity_id)
            settings[setting_name] = state.state if state is not None else None

        if not audio_sent:
            _LOGGER.error("[turn=%s] transcription-only STT received no audio", turn_id)

        failure_reason: str | None = None
        if unexpected_tool_call:
            failure_reason = "unexpected_tool_call"
        elif not audio_sent:
            failure_reason = "no_audio_sent"
        elif finalizer.failure_reason is not None:
            failure_reason = finalizer.failure_reason
        elif receive_failed or finalizer.final_reason is None:
            failure_reason = "incomplete_transcription"
        elif not input_transcript:
            failure_reason = "no_usable_transcript"

        latest_metrics = build_latest_stt_metrics(
            turn_id=turn_id,
            conversation_id=conversation_id,
            input_transcript=(input_transcript if failure_reason is None else ""),
            response_audio_received=False,
            response_audio_bytes=0,
            response_text="",
            input_audio_sent=audio_sent,
            input_pcm=bytes(input_pcm_for_failed_capture),
            settings=settings,
        )
        try:
            latest_metrics_path = self.hass.config.path(LATEST_STT_METRICS_PATH)
            await self.hass.async_add_executor_job(
                save_latest_stt_metrics,
                latest_metrics_path,
                latest_metrics,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("[turn=%s] Failed to save latest STT metrics", turn_id)

        if failure_reason is not None:
            if input_pcm_for_failed_capture:
                capture_metadata = {
                    "turn_id": turn_id,
                    "conversation_id": conversation_id,
                    "reason": failure_reason,
                    "model": model,
                    "partial_input_transcript": "",
                    "audio_analysis": _analyse_pcm(
                        bytes(input_pcm_for_failed_capture)
                    ),
                    "settings": settings,
                }
                try:
                    capture_dir = self.hass.config.path(FAILED_STT_CAPTURE_DIR)
                    wav_path, json_path = await self.hass.async_add_executor_job(
                        save_failed_stt_capture,
                        capture_dir,
                        bytes(input_pcm_for_failed_capture),
                        capture_metadata,
                        FAILED_STT_CAPTURE_KEEP,
                        FAILED_STT_CAPTURE_SAMPLE_RATE,
                    )
                    _LOGGER.warning(
                        "[turn=%s] Saved failed STT capture wav=%s metadata=%s reason=%s",
                        turn_id,
                        wav_path,
                        json_path,
                        failure_reason,
                    )
                except Exception:  # noqa: BLE001
                    _LOGGER.exception(
                        "[turn=%s] Failed to save failed-STT audio capture",
                        turn_id,
                    )
            _LOGGER.error(
                "[turn=%s] transcription-only STT failed closed reason=%s",
                turn_id,
                failure_reason,
            )
            trace.emit("stt_failed", reason=failure_reason)
            return SpeechResult(None, SpeechResultState.ERROR)

        from .input_safety import classify_russian_input, speech_result_text

        safety = classify_russian_input(input_transcript)
        safe_text = speech_result_text(input_transcript, safety)
        trace.emit("final_transcript", text=input_transcript)
        trace.emit(
            "gate",
            action=safety.action,
            reason=safety.reason,
            cyrillic_letters=safety.cyrillic_letters,
            latin_letters=safety.latin_letters,
            other_letters=safety.other_letters,
        )
        entry_data[GEMINI_TURN_STORE_KEY].add_trace(
            conversation_id,
            safe_text,
            trace,
        )
        _LOGGER.warning(
            "[turn=%s] input gate action=%s reason=%s cyrillic=%d latin=%d other=%d elapsed=%.3fs",
            turn_id,
            safety.action,
            safety.reason,
            safety.cyrillic_letters,
            safety.latin_letters,
            safety.other_letters,
            time.monotonic() - started_at,
        )
        if not result_future.done():
            result_future.set_result(
                SpeechResult(safe_text, SpeechResultState.SUCCESS)
            )
        return SpeechResult(safe_text, SpeechResultState.SUCCESS)

    async def _async_process_audio_stream_sdk(
        self,
        metadata: SpeechMetadata,
        stream: AsyncIterable[bytes],
        api_key: str,
        model: str,
        voice: str,
        custom_instruction: str,
        transcribe_gemini: bool,
        encourage_web_search: bool,
        show_text: bool,
        trace_id: str,
    ) -> SpeechResult:
        """Run the Live turn in the background so TTS can consume it immediately."""
        result_future: asyncio.Future[SpeechResult] = asyncio.Future()
        task = self.hass.async_create_background_task(
            self._async_run_direct_live_sdk(
                metadata,
                stream,
                api_key,
                model,
                voice,
                custom_instruction,
                transcribe_gemini,
                encourage_web_search,
                show_text,
                result_future,
                trace_id,
            ),
            "Gemini direct Live audio turn",
        )

        def set_final_result(completed_task: asyncio.Task[SpeechResult]) -> None:
            try:
                final_result = completed_task.result()
            except asyncio.CancelledError:
                if not result_future.done():
                    result_future.set_result(
                        SpeechResult(None, SpeechResultState.ERROR)
                    )
            except _GeminiInitialNetworkError as exc:
                if result_future.done():
                    _LOGGER.warning(
                        "Gemini direct Live initial-network error arrived after handoff"
                    )
                    return
                from .input_safety import OFFLINE_INPUT_SENTINEL

                entry_data = self.hass.data[DOMAIN][self.entry.entry_id]
                entry_data[GEMINI_TURN_STORE_KEY].add_trace(
                    exc.trace.conversation_id,
                    OFFLINE_INPUT_SENTINEL,
                    exc.trace,
                )
                exc.trace.emit("offline_handoff")
                result_future.set_result(
                    SpeechResult(OFFLINE_INPUT_SENTINEL, SpeechResultState.SUCCESS)
                )
            except Exception as exc:  # noqa: BLE001
                if result_future.done():
                    _LOGGER.error(
                        "Gemini direct Live turn failed after PCM handoff type=%s",
                        type(exc).__name__,
                    )
                else:
                    _LOGGER.exception("Gemini Live audio turn failed")
                    result_future.set_result(
                        SpeechResult(None, SpeechResultState.ERROR)
                    )
            else:
                if not result_future.done():
                    result_future.set_result(final_result)

        task.add_done_callback(set_final_result)
        try:
            return await result_future
        except asyncio.CancelledError:
            task.cancel()
            raise

    @property
    def supported_languages(self) -> list[str]:
        return SUPPORTED_LANGUAGES

    @property
    def supported_formats(self) -> list[AudioFormats]:
        return [AudioFormats.WAV]

    @property
    def supported_codecs(self) -> list[AudioCodecs]:
        return [AudioCodecs.PCM]

    @property
    def supported_sample_rates(self) -> list[AudioSampleRates]:
        return [AudioSampleRates.SAMPLERATE_16000]

    @property
    def supported_channels(self) -> list[AudioChannels]:
        return [AudioChannels.CHANNEL_MONO]

    @property
    def supported_bit_rates(self) -> list[AudioBitRates]:
        return [AudioBitRates.BITRATE_16]

    async def async_process_audio_stream(
        self,
        metadata: SpeechMetadata,
        stream: AsyncIterable[bytes],
    ) -> SpeechResult:
        """Process the audio stream and send it directly to Gemini Live API."""
        turn_id = uuid4().hex[:8]
        config = {**self.entry.data, **self.entry.options}
        api_key = config.get(CONF_API_KEY)
        model = config.get(CONF_MODEL)
        voice = config.get(CONF_VOICE)
        custom_instruction = config.get(CONF_SYSTEM_INSTRUCTION, "")
        transcribe_gemini = bool(
            config.get(CONF_TRANSCRIBE_GEMINI, DEFAULT_TRANSCRIBE_GEMINI)
        )
        encourage_web_search = bool(
            config.get(CONF_ENCOURAGE_WEB_SEARCH, DEFAULT_ENCOURAGE_WEB_SEARCH)
        )
        show_text = bool(
            config.get(CONF_SHOW_TEXT, DEFAULT_SHOW_TEXT)
        )
        set_detailed_logging(bool(config.get(CONF_DETAILED_LOGGING, False)))

        _LOGGER.warning(
            "[turn=%s] STT start language=%s model=%s voice=%s detailed_logging=%s",
            turn_id,
            metadata.language or "en",
            model,
            voice,
            bool(config.get(CONF_DETAILED_LOGGING, False)),
        )

        if not api_key:
            _LOGGER.error("API Key not configured for Gemini Live")
            return SpeechResult(None, SpeechResultState.ERROR)

        return await self._async_process_audio_stream_sdk(
            metadata,
            stream,
            api_key,
            model,
            voice,
            custom_instruction,
            transcribe_gemini,
            encourage_web_search,
            show_text,
            turn_id,
        )
