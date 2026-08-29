"""Conversation platform for Gemini Live."""

import asyncio
import logging
import time
from typing import Any
from uuid import uuid4

from google import genai
from google.genai import types
from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import chat_session, llm
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.intent import IntentResponse

from .const import (
    CONF_API_KEY,
    CONF_ENCOURAGE_WEB_SEARCH,
    CONF_MODEL,
    CONF_P610_LIVE_TEXT,
    CONF_SYSTEM_INSTRUCTION,
    CONF_TRANSCRIBE_GEMINI,
    CONF_SHOW_TEXT,
    CONF_VOICE,
    DEFAULT_SYSTEM_INSTRUCTION,
    DEFAULT_ENCOURAGE_WEB_SEARCH,
    DEFAULT_TRANSCRIBE_GEMINI,
    DEFAULT_SHOW_TEXT,
    DEFAULT_P610_LIVE_TEXT,
    DOMAIN,
    GEMINI_LIVE_TTS_PLACEHOLDER,
    GEMINI_SESSION_MANAGER_KEY,
    GEMINI_TURN_STORE_KEY,
    SUPPORTED_LANGUAGES,
)
from .stt import (
    END_CONVERSATION_TOOL_NAME,
    _add_end_conversation_instruction,
    _add_end_conversation_tool,
    _add_search_tool_instruction,
    _escape_decode,
    _format_tools_for_gemini_live,
    _is_connection_closed_ok,
    _validate_tool_results,
    SHOW_TEXT_TOOL_NAME,
    _add_show_text_instruction,
    _add_show_text_tool,
)
from .input_safety import (
    ENGINE_CONFIRMATION_PROMPT,
    ENGINE_ENTITY_ID,
    FOREIGN_READ_ONLY_INSTRUCTION,
    LOCAL_STOP_SENTINEL,
    OFFLINE_INPUT_SENTINEL,
    RUSSIAN_ONLY_TOOL_INSTRUCTION,
    EngineLocalAction,
    EngineSafetyGuard,
    InputAction,
    InputSafetyDecision,
    classify_russian_input,
    is_engine_on_tool_call,
)
from .local_audio import OFFLINE_RESPONSE_TEXT, offline_response_wav, silent_response_wav
from .observer import TurnTrace
from .runtime import AudioStream, TextStream, new_conversation_id
from .utils import resample_24k_to_16k

_LOGGER = logging.getLogger(__name__)


class _UnexpectedNoToolsCall(Exception):
    """Model attempted a tool call in a code-enforced no-tools turn."""


def _ensure_unique_tts_placeholder(assistant_text: str) -> str:
    """Make the streaming placeholder unique so HA cannot reuse cached audio."""
    if assistant_text == GEMINI_LIVE_TTS_PLACEHOLDER:
        return f"{GEMINI_LIVE_TTS_PLACEHOLDER} {uuid4().hex[:8]}"
    return assistant_text


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Gemini Live Conversation platform."""
    async_add_entities([GeminiLiveConversationAgent(hass, config_entry)])


class GeminiLiveConversationAgent(conversation.ConversationEntity):
    """Gemini Live conversation entity."""

    _attr_supported_features = conversation.ConversationEntityFeature.CONTROL
    _attr_supports_streaming = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the agent."""
        self.hass = hass
        self.entry = entry
        self._name = "Gemini Live"
        self._unique_id = f"{entry.entry_id}_conversation"
        self._engine_safety = EngineSafetyGuard()
        self._engine_cleanup_registered: set[str] = set()

    @property
    def name(self) -> str:
        """Return the name of the entity."""
        return self._name

    @property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return self._unique_id

    @property
    def supported_features(self) -> conversation.ConversationEntityFeature:
        """Return supported features."""
        return conversation.ConversationEntityFeature.CONTROL

    @property
    def supported_languages(self) -> list[str] | str:
        """Return supported languages."""
        return SUPPORTED_LANGUAGES

    def _fire_conversation_entry(
        self,
        user_transcript: str,
        assistant_text: str,
        *,
        include_text: bool,
    ) -> None:
        """Fire the text event used by automations and dashboards."""
        payload = {
            "user_transcript_chars": len(user_transcript),
            "assistant_text_chars": len(assistant_text),
        }
        if include_text:
            payload.update(
                {
                    "user_transcript": user_transcript,
                    "assistant_text": assistant_text,
                }
            )
        self.hass.bus.async_fire(
            "gemini_live_conversation_entry",
            payload,
        )
        _LOGGER.debug(
            "Fired gemini_live_conversation_entry user_chars=%d assistant_chars=%d",
            len(user_transcript or ""),
            len(assistant_text or ""),
        )

    async def _async_get_llm_api(
        self,
        llm_context: llm.LLMContext,
    ) -> tuple[llm.APIInstance | None, list[dict[str, Any]], str]:
        """Load HA Assist tools and the final Gemini system instruction."""
        config = {**self.entry.data, **self.entry.options}
        custom_instruction = config.get(CONF_SYSTEM_INSTRUCTION, "")
        encourage_web_search = bool(
            config.get(CONF_ENCOURAGE_WEB_SEARCH, DEFAULT_ENCOURAGE_WEB_SEARCH)
        )
        transcribe_gemini = bool(
            config.get(CONF_TRANSCRIBE_GEMINI, DEFAULT_TRANSCRIBE_GEMINI)
        )
        show_text = bool(
            config.get(CONF_SHOW_TEXT, DEFAULT_SHOW_TEXT)
        )
        system_instruction = custom_instruction or DEFAULT_SYSTEM_INSTRUCTION

        try:
            llm_api = await llm.async_get_api(
                hass=self.hass,
                api_id=llm.LLM_API_ASSIST,
                llm_context=llm_context,
            )
            api_prompt = llm_api.api_prompt
            if custom_instruction:
                system_instruction = f"{custom_instruction}\n\n{api_prompt}"
            else:
                system_instruction = DEFAULT_SYSTEM_INSTRUCTION + "\n\n" + api_prompt
            system_instruction = (
                f"{system_instruction}\n\n{RUSSIAN_ONLY_TOOL_INSTRUCTION}"
            )
            system_instruction = _add_search_tool_instruction(
                system_instruction,
                llm_api.tools,
                encourage_web_search,
            )
            system_instruction = _add_end_conversation_instruction(system_instruction)
            if not transcribe_gemini and show_text:
                system_instruction = _add_show_text_instruction(system_instruction)

            gemini_tools = _add_end_conversation_tool(
                _format_tools_for_gemini_live(
                    llm_api.tools,
                    llm_api.custom_serializer,
                    encourage_web_search,
                )
            )
            if not transcribe_gemini and show_text:
                gemini_tools = _add_show_text_tool(gemini_tools)

            _LOGGER.debug(
                "Conversation text path loaded %d HA Assist tools",
                len(gemini_tools),
            )
            return llm_api, gemini_tools, system_instruction
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Could not load HA Assist LLM API for text path: %s. Tools will be unavailable.",
                exc,
            )
            gemini_tools = _add_end_conversation_tool([])
            system_instruction = (
                f"{system_instruction}\n\n{RUSSIAN_ONLY_TOOL_INSTRUCTION}"
            )
            system_instruction = _add_end_conversation_instruction(system_instruction)
            if not transcribe_gemini and show_text:
                system_instruction = _add_show_text_instruction(system_instruction)
                gemini_tools = _add_show_text_tool(gemini_tools)
            return (
                None,
                gemini_tools,
                system_instruction,
            )

    async def _async_process_text_live(
        self,
        user_text: str,
        user_input: conversation.ConversationInput,
        conversation_id: str,
        safety_decision: InputSafetyDecision,
        *,
        tools_allowed: bool = True,
        safety_instruction: str | None = None,
        trace: TurnTrace | None = None,
    ) -> str | None:
        """Send typed text to Gemini Live and cache the returned audio for TTS."""
        turn_id = uuid4().hex[:8]
        show_text_content: str | None = None
        started_at = time.monotonic()
        config = {**self.entry.data, **self.entry.options}
        api_key = config.get(CONF_API_KEY)
        model = config.get(CONF_MODEL)
        voice = config.get(CONF_VOICE)
        language = user_input.language or "en"
        transcribe_gemini = bool(
            config.get(CONF_TRANSCRIBE_GEMINI, DEFAULT_TRANSCRIBE_GEMINI)
        )
        show_text = bool(
            config.get(CONF_SHOW_TEXT, DEFAULT_SHOW_TEXT)
        )

        verified_safety = classify_russian_input(user_text)
        actions_match = safety_decision.action is verified_safety.action
        safe_for_mode = (
            verified_safety.action is InputAction.ACCEPT
            if tools_allowed
            else verified_safety.action
            in (InputAction.ACCEPT, InputAction.REJECT_FOREIGN)
        )
        if not actions_match or not safe_for_mode:
            _LOGGER.error(
                "Refusing conversation path for supplied_action=%s verified_action=%s reason=%s",
                safety_decision.action,
                verified_safety.action,
                verified_safety.reason,
            )
            return None
        if not api_key:
            _LOGGER.error("API Key not configured for Gemini Live")
            return None

        if tools_allowed:
            llm_api, gemini_tools, system_instruction = await self._async_get_llm_api(
                user_input.as_llm_context(DOMAIN)
            )
        else:
            llm_api = None
            gemini_tools = []
            system_instruction = safety_instruction or (
                f"{RUSSIAN_ONLY_TOOL_INSTRUCTION} "
                "Это безопасный read-only turn: кратко ответь по-русски и не "
                "выполняй никаких действий."
            )
        entry_data = self.hass.data[DOMAIN][self.entry.entry_id]
        session_manager = entry_data[GEMINI_SESSION_MANAGER_KEY]
        turn_store = entry_data[GEMINI_TURN_STORE_KEY]

        live_config: dict[str, Any] = {
            "response_modalities": ["AUDIO"],
            "speech_config": {
                "voice_config": {
                    "prebuilt_voice_config": {"voice_name": voice}
                }
            },
            "system_instruction": {"parts": [{"text": system_instruction}]},
            "input_audio_transcription": {},
            "output_audio_transcription": {},
            "realtime_input_config": {
                "turn_coverage": "TURN_INCLUDES_ONLY_ACTIVITY"
            },
        }
        if gemini_tools:
            live_config["tools"] = gemini_tools

        native_audio_model = "native-audio" in (model or "")
        response_future: asyncio.Future[str | None] = asyncio.Future()
        audio_stream = AudioStream()
        text_stream = TextStream()
        tts_marker = _ensure_unique_tts_placeholder(GEMINI_LIVE_TTS_PLACEHOLDER)

        _LOGGER.warning(
            "[turn=%s] conversation text path start model=%s voice=%s tools=%d "
            "text_chars=%d gate=%s",
            turn_id,
            model,
            voice,
            len(gemini_tools),
            len(user_text),
            verified_safety.action,
        )
        if trace is not None:
            trace.emit(
                "text_path_start",
                tools_allowed=tools_allowed,
                text=user_text,
            )

        client = await self.hass.async_add_executor_job(
            lambda: genai.Client(api_key=api_key, http_options=types.HttpOptions(async_client_args={'proxy': 'http://127.0.0.1:18091'}))
        )

        async def run_text_turn() -> None:
            nonlocal show_text_content
            audio_chunks = 0
            audio_bytes = 0
            streaming_registered = False
            first_response_text_emitted = False
            output_transcript_received = False
            model_text_parts: list[str] = []
            try:
                async with session_manager.acquire(
                    conversation_id,
                    client,
                    model,
                    live_config,
                ) as session:
                    if trace is not None:
                        trace.emit("text_connection_ready")
                    await session.send_realtime_input(text=user_text)
                    if trace is not None:
                        trace.emit("text_send", text_chars=len(user_text))

                    async with asyncio.timeout(30):
                        async for response in session.receive():
                            if response.tool_call:
                                if not tools_allowed:
                                    if trace is not None:
                                        trace.emit("no_tools_violation")
                                    raise _UnexpectedNoToolsCall
                                function_responses = []
                                for call in response.tool_call.function_calls or []:
                                    tool_name = call.name or ""
                                    tool_args = _escape_decode(call.args or {})
                                    call_id = call.id
                                    _LOGGER.info(
                                        "Gemini Live text path tool call name=%s arg_keys=%d",
                                        tool_name,
                                        len(tool_args) if isinstance(tool_args, dict) else 0,
                                    )
                                    if tool_name == END_CONVERSATION_TOOL_NAME:
                                        if streaming_registered:
                                            # ConversationResult may already be on
                                            # its way to Assist once first PCM is
                                            # available. A late lifecycle change
                                            # would leave the satellite with stale
                                            # continue=true, so fail closed and keep
                                            # this turn open for deterministic stop.
                                            _LOGGER.warning(
                                                "Ignored late end_conversation after response audio started"
                                            )
                                            if trace is not None:
                                                trace.emit("late_end_ignored")
                                            tool_result = {
                                                "error": "end_conversation_must_precede_audio"
                                            }
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
                                            "Blocked engine ON at HA tool boundary "
                                            "reason=stale_assist_exposure"
                                        )
                                        tool_result = {
                                            "error": "engine_on_requires_local_russian_confirmation"
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
                                            _LOGGER.error("Tool %s failed: %s", tool_name, err)
                                            tool_result = {"error": str(err)}
                                    else:
                                        tool_result = {"error": "HA LLM API not available"}
                                    function_responses.append(
                                        types.FunctionResponse(
                                            name=tool_name,
                                            id=call_id,
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
                            if content.model_turn:
                                for part in content.model_turn.parts or []:
                                    if part.text:
                                        # Native-audio responses may expose both
                                        # model text and spoken output transcription.
                                        # Keep model text only as a terminal fallback
                                        # so the spoken transcript is never doubled.
                                        model_text_parts.append(part.text)
                                    if part.inline_data and part.inline_data.data:
                                        raw_chunk = part.inline_data.data
                                        pcm_chunk = resample_24k_to_16k(raw_chunk)
                                        if not pcm_chunk:
                                            continue
                                        if not streaming_registered:
                                            turn_store.add_streaming_audio(
                                                tts_marker,
                                                text_stream,
                                                audio_stream,
                                                trace,
                                            )
                                            streaming_registered = True
                                            if trace is not None:
                                                trace.emit(
                                                    "turnstore_ready",
                                                    streaming=True,
                                                )
                                            if not response_future.done():
                                                response_future.set_result(tts_marker)
                                        audio_stream.add_chunk(pcm_chunk)
                                        audio_chunks += 1
                                        audio_bytes += len(pcm_chunk)
                                        if audio_chunks == 1 and trace is not None:
                                            trace.emit(
                                                "first_response_audio",
                                                pcm_bytes=len(pcm_chunk),
                                            )

                            if content.output_transcription and content.output_transcription.text:
                                output_transcript_received = True
                                transcript_chunk = content.output_transcription.text
                                text_stream.add_chunk(transcript_chunk)
                                if trace is not None:
                                    trace.emit("assistant_delta", text=transcript_chunk)
                                    if not first_response_text_emitted:
                                        first_response_text_emitted = True
                                        trace.emit(
                                            "first_response_text",
                                            text=transcript_chunk,
                                        )

                            if content.turn_complete:
                                if native_audio_model and not audio_chunks:
                                    _LOGGER.warning(
                                        "[turn=%s] text path turnComplete before audio; waiting",
                                        turn_id,
                                    )
                                    continue
                                break
                    if not output_transcript_received and model_text_parts:
                        fallback_model_text = "".join(model_text_parts)
                        text_stream.add_chunk(fallback_model_text)
                        if trace is not None:
                            trace.emit("assistant_delta", text=fallback_model_text)
                            if not first_response_text_emitted:
                                first_response_text_emitted = True
                                trace.emit(
                                    "first_response_text",
                                    text=fallback_model_text,
                                )
            except TimeoutError:
                _LOGGER.error("[turn=%s] Gemini Live text path timed out", turn_id)
            except _UnexpectedNoToolsCall:
                _LOGGER.error(
                    "[turn=%s] rejected unexpected tool call in no-tools text path",
                    turn_id,
                )
            except Exception as exc:  # noqa: BLE001
                if _is_connection_closed_ok(exc):
                    _LOGGER.warning(
                        "[turn=%s] Gemini Live text path websocket closed normally",
                        turn_id,
                    )
                else:
                    _LOGGER.exception(
                        "[turn=%s] error in Gemini Live text path: %s",
                        turn_id,
                        exc,
                    )
            finally:
                text_stream.finish()
                audio_stream.finish()

            if not response_future.done():
                fallback_text = (
                    show_text_content
                    if not transcribe_gemini and show_text and show_text_content
                    else text_stream.text
                )
                response_future.set_result(fallback_text or None)
            if trace is not None:
                trace.emit(
                    "text_turn_complete",
                    text=text_stream.text or None,
                    audio_chunks=audio_chunks,
                    audio_bytes=audio_bytes,
                )
            _LOGGER.info(
                "[turn=%s] conversation text path complete text_chars=%d "
                "audio_chunks=%d audio_bytes=%d elapsed=%.3fs",
                turn_id,
                len(text_stream.text),
                audio_chunks,
                audio_bytes,
                time.monotonic() - started_at,
            )

        task = self.hass.async_create_background_task(
            run_text_turn(),
            f"Gemini Live text turn {turn_id}",
        )

        def finish_result(completed_task: asyncio.Task[None]) -> None:
            if response_future.done():
                return
            try:
                completed_task.result()
            except asyncio.CancelledError:
                response_future.set_result(None)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("[turn=%s] background text turn failed", turn_id)
                response_future.set_result(None)

        task.add_done_callback(finish_result)
        return await response_future

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Consume direct voice turns or process non-voice safety fallbacks."""
        entry_data = self.hass.data[DOMAIN][self.entry.entry_id]
        turn_store = entry_data[GEMINI_TURN_STORE_KEY]
        language = user_input.language or "ru"
        input_text = user_input.text or ""
        current_chat_session = chat_session.current_session.get()
        conversation_id = (
            current_chat_session.conversation_id
            if current_chat_session is not None
            else user_input.conversation_id
        ) or new_conversation_id()
        session_manager = entry_data[GEMINI_SESSION_MANAGER_KEY]
        trace = turn_store.take_trace(conversation_id, input_text)
        if trace is None:
            config = {**self.entry.data, **self.entry.options}
            trace = TurnTrace(
                self.hass,
                self.entry.entry_id,
                conversation_id,
                uuid4().hex[:8],
                include_text=bool(
                    config.get(CONF_P610_LIVE_TEXT, DEFAULT_P610_LIVE_TEXT)
                ),
            )
            trace.emit("conversation_start", text=input_text)
        if current_chat_session is not None:
            session_manager.register_chat_session(self.hass, current_chat_session)
            if conversation_id not in self._engine_cleanup_registered:
                self._engine_cleanup_registered.add(conversation_id)

                def clear_engine_confirmation() -> None:
                    self._engine_cleanup_registered.discard(conversation_id)
                    self._engine_safety.clear(conversation_id)

                current_chat_session.async_on_cleanup(clear_engine_confirmation)

        voice_turn = turn_store.take_voice_turn(conversation_id, input_text)
        if voice_turn is not None:
            self._engine_safety.clear(conversation_id)
            assistant_text = _ensure_unique_tts_placeholder(
                voice_turn.assistant_text
            )
            turn_store.add_audio(assistant_text, voice_turn.audio, trace)
            chat_log.async_add_assistant_content_without_tools(
                conversation.AssistantContent(
                    agent_id=self.entity_id,
                    content=assistant_text,
                )
            )
            trace.emit(
                "conversation_direct_live",
                language_gate="bypassed",
                tools="same_session",
            )
            self._fire_conversation_entry(
                input_text,
                assistant_text,
                include_text=trace.include_text,
            )
            intent_response = IntentResponse(language=language)
            intent_response.async_set_speech(assistant_text)
            continue_conversation = session_manager.should_continue_conversation(
                conversation_id
            )
            trace.emit(
                "conversation_result",
                text=assistant_text,
                continue_conversation=continue_conversation,
            )
            return conversation.ConversationResult(
                response=intent_response,
                conversation_id=conversation_id,
                continue_conversation=continue_conversation,
            )

        if input_text == LOCAL_STOP_SENTINEL:
            safety = InputSafetyDecision(InputAction.LOCAL_STOP, "stt_local_stop", 0, 0, 0)
        elif input_text == OFFLINE_INPUT_SENTINEL:
            safety = InputSafetyDecision(
                InputAction.OFFLINE_NETWORK,
                "stt_initial_network_failure",
                0,
                0,
                0,
            )
        else:
            safety = classify_russian_input(input_text)

        _LOGGER.warning(
            "Conversation input gate action=%s reason=%s cyrillic=%d latin=%d other=%d",
            safety.action,
            safety.reason,
            safety.cyrillic_letters,
            safety.latin_letters,
            safety.other_letters,
        )
        trace.emit(
            "conversation_gate",
            action=safety.action,
            reason=safety.reason,
            cyrillic_letters=safety.cyrillic_letters,
            latin_letters=safety.latin_letters,
            other_letters=safety.other_letters,
        )

        user_transcript = "" if safety.action is InputAction.OFFLINE_NETWORK else input_text
        assistant_text: str

        if safety.action is InputAction.OFFLINE_NETWORK:
            self._engine_safety.clear(conversation_id)
            await session_manager.async_close(conversation_id)
            session_manager.complete_conversation(conversation_id)
            assistant_text = OFFLINE_RESPONSE_TEXT
            local_audio = await self.hass.async_add_executor_job(offline_response_wav)
            turn_store.add_audio(assistant_text, local_audio, trace)
            _LOGGER.info(
                "[conversation=%s] phase=response_local kind=offline_network "
                "audio_bytes=%d continue=false",
                conversation_id,
                len(local_audio),
            )
            chat_log.async_add_assistant_content_without_tools(
                conversation.AssistantContent(
                    agent_id=self.entity_id,
                    content=assistant_text,
                )
            )
            trace.emit(
                "response_local",
                kind="offline_network",
                text=assistant_text,
                audio_bytes=len(local_audio),
            )
        elif safety.action is InputAction.LOCAL_STOP:
            self._engine_safety.clear(conversation_id)
            await session_manager.async_close(conversation_id)
            session_manager.complete_conversation(conversation_id)
            assistant_text = _ensure_unique_tts_placeholder(
                GEMINI_LIVE_TTS_PLACEHOLDER
            )
            turn_store.add_audio(assistant_text, silent_response_wav(), trace)
            trace.emit("response_local", kind="local_stop")
            chat_log.async_add_assistant_content_without_tools(
                conversation.AssistantContent(
                    agent_id=self.entity_id,
                    content=assistant_text,
                )
            )
        elif safety.action is InputAction.REJECT_FOREIGN:
            self._engine_safety.clear(conversation_id)
            session_manager.reset_conversation(conversation_id)
            assistant_text = await self._async_process_text_live(
                input_text,
                user_input,
                conversation_id,
                safety,
                tools_allowed=False,
                safety_instruction=FOREIGN_READ_ONLY_INSTRUCTION,
                trace=trace,
            ) or "Пожалуйста, повторите или уточните реплику."
            chat_log.async_add_assistant_content_without_tools(
                conversation.AssistantContent(
                    agent_id=self.entity_id,
                    content=assistant_text,
                )
            )
        else:
            session_manager.reset_conversation(conversation_id)
            engine_action = self._engine_safety.classify_turn(
                conversation_id,
                input_text,
            )
            local_response: str | None = None

            if engine_action is EngineLocalAction.REQUEST_CONFIRMATION:
                local_response = ENGINE_CONFIRMATION_PROMPT
            elif engine_action is EngineLocalAction.CONFIRMED_ON:
                try:
                    await self.hass.services.async_call(
                        "switch",
                        "turn_on",
                        {"entity_id": ENGINE_ENTITY_ID},
                        blocking=True,
                    )
                    local_response = "Двигатель запущен."
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Confirmed local engine start failed")
                    local_response = "Не удалось запустить двигатель."
            elif engine_action is EngineLocalAction.OFF:
                try:
                    await self.hass.services.async_call(
                        "switch",
                        "turn_off",
                        {"entity_id": ENGINE_ENTITY_ID},
                        blocking=True,
                    )
                    local_response = "Двигатель выключен."
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Local engine stop failed")
                    local_response = "Не удалось выключить двигатель."
            elif engine_action is EngineLocalAction.CANCELLED:
                local_response = "Подтверждение запуска двигателя отменено."

            if local_response is not None:
                local_safety = classify_russian_input(local_response)
                assistant_text = await self._async_process_text_live(
                    local_response,
                    user_input,
                    conversation_id,
                    local_safety,
                    tools_allowed=False,
                    trace=trace,
                ) or local_response
                chat_log.async_add_assistant_content_without_tools(
                    conversation.AssistantContent(
                        agent_id=self.entity_id,
                        content=assistant_text,
                    )
                )
            else:
                voice_turn = turn_store.take_voice_turn(conversation_id, input_text)
                if voice_turn:
                    fallback_assistant_text = _ensure_unique_tts_placeholder(
                        voice_turn.assistant_text
                    )
                    if voice_turn.assistant_text_stream is not None:
                        if not isinstance(voice_turn.audio, AudioStream):
                            raise RuntimeError(
                                "Streaming transcript has no streaming audio"
                            )
                        turn_store.add_streaming_audio(
                            fallback_assistant_text,
                            voice_turn.assistant_text_stream,
                            voice_turn.audio,
                        )

                        async def transcript_deltas():
                            """Yield Gemini response transcript into Home Assistant."""
                            yield {"role": "assistant"}
                            received_text = False
                            async for chunk in voice_turn.assistant_text_stream.async_chunks():
                                received_text = True
                                yield {"content": chunk}
                            if not received_text:
                                yield {"content": fallback_assistant_text}

                        async for _content in chat_log.async_add_delta_content_stream(
                            self.entity_id,
                            transcript_deltas(),
                        ):
                            pass
                        assistant_text = (
                            voice_turn.assistant_text_stream.text
                            or fallback_assistant_text
                        )
                    else:
                        assistant_text = fallback_assistant_text
                        turn_store.add_audio(assistant_text, voice_turn.audio)
                        chat_log.async_add_assistant_content_without_tools(
                            conversation.AssistantContent(
                                agent_id=self.entity_id,
                                content=assistant_text,
                            )
                        )
                else:
                    assistant_text = await self._async_process_text_live(
                        input_text,
                        user_input,
                        conversation_id,
                        safety,
                        trace=trace,
                    ) or "Не удалось получить ответ от Gemini Live."
                    chat_log.async_add_assistant_content_without_tools(
                        conversation.AssistantContent(
                            agent_id=self.entity_id,
                            content=assistant_text,
                        )
                    )

        self._fire_conversation_entry(
            user_transcript,
            assistant_text,
            include_text=trace.include_text,
        )

        intent_response = IntentResponse(language=language)
        intent_response.async_set_speech(assistant_text)
        continue_conversation = session_manager.should_continue_conversation(
            conversation_id
        )
        trace.emit(
            "conversation_result",
            text=assistant_text,
            continue_conversation=continue_conversation,
        )
        return conversation.ConversationResult(
            response=intent_response,
            conversation_id=conversation_id,
            continue_conversation=continue_conversation,
        )
