from __future__ import annotations

from dataclasses import dataclass
from contextlib import asynccontextmanager
import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock


ROOT = Path(__file__).resolve().parents[1]
COMPONENT_ROOT = ROOT / "experimental" / "gemini_live"


def module(name: str) -> types.ModuleType:
    value = types.ModuleType(name)
    sys.modules[name] = value
    return value


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


package = module("gemini_live_behavior")
package.__path__ = [str(COMPONENT_ROOT)]

google = module("google")
genai = module("google.genai")
genai_types = module("google.genai.types")
genai.types = genai_types
google.genai = genai
genai.Client = object
genai_types.HttpOptions = object
genai_types.FunctionResponse = object

homeassistant = module("homeassistant")
components = module("homeassistant.components")
ha_conversation = module("homeassistant.components.conversation")
components.conversation = ha_conversation
homeassistant.components = components


class ConversationEntity:
    entity_id = "conversation.gemini_live"


class ConversationEntityFeature:
    CONTROL = 1


@dataclass
class AssistantContent:
    agent_id: str
    content: str


@dataclass
class ConversationResult:
    response: object
    conversation_id: str
    continue_conversation: bool


ha_conversation.ConversationEntity = ConversationEntity
ha_conversation.ConversationEntityFeature = ConversationEntityFeature
ha_conversation.AssistantContent = AssistantContent
ha_conversation.ConversationResult = ConversationResult
ha_conversation.ConversationInput = object
ha_conversation.ChatLog = object

config_entries = module("homeassistant.config_entries")
config_entries.ConfigEntry = object
core = module("homeassistant.core")
core.HomeAssistant = object
homeassistant.config_entries = config_entries
homeassistant.core = core

helpers = module("homeassistant.helpers")
chat_session = module("homeassistant.helpers.chat_session")
chat_session.current_session = types.SimpleNamespace(get=lambda: None)
llm = module("homeassistant.helpers.llm")
llm.LLMContext = object
llm.APIInstance = object
llm.ToolInput = object
helpers.chat_session = chat_session
helpers.llm = llm
homeassistant.helpers = helpers

entity_platform = module("homeassistant.helpers.entity_platform")
entity_platform.AddEntitiesCallback = object
intent = module("homeassistant.helpers.intent")


class IntentResponse:
    def __init__(self, language: str) -> None:
        self.language = language
        self.speech = None

    def async_set_speech(self, text: str) -> None:
        self.speech = text


intent.IntentResponse = IntentResponse

const = module("gemini_live_behavior.const")
for name, value in {
    "CONF_API_KEY": "api_key",
    "CONF_DETAILED_LOGGING": "detailed_logging",
    "CONF_ENCOURAGE_WEB_SEARCH": "encourage_web_search",
    "CONF_MODEL": "model",
    "CONF_P610_LIVE_TEXT": "p610_live_text",
    "CONF_SYSTEM_INSTRUCTION": "system_instruction",
    "CONF_TRANSCRIBE_GEMINI": "transcribe_gemini",
    "CONF_SHOW_TEXT": "show_text",
    "CONF_VOICE": "voice",
    "DEFAULT_SYSTEM_INSTRUCTION": "default",
    "DEFAULT_ENCOURAGE_WEB_SEARCH": False,
    "DEFAULT_TRANSCRIBE_GEMINI": False,
    "DEFAULT_SHOW_TEXT": True,
    "DEFAULT_P610_LIVE_TEXT": False,
    "DOMAIN": "gemini_live",
    "GEMINI_LIVE_TTS_PLACEHOLDER": "-- gemini live --",
    "GEMINI_SESSION_MANAGER_KEY": "session_manager",
    "GEMINI_TURN_STORE_KEY": "turn_store",
    "SUPPORTED_LANGUAGES": ["ru"],
}.items():
    setattr(const, name, value)

stt = module("gemini_live_behavior.stt")
stt.END_CONVERSATION_TOOL_NAME = "end_conversation"
stt.SHOW_TEXT_TOOL_NAME = "show_text"
for name in (
    "_add_end_conversation_instruction",
    "_add_end_conversation_tool",
    "_add_search_tool_instruction",
    "_add_show_text_instruction",
    "_add_show_text_tool",
    "_escape_decode",
    "_format_tools_for_gemini_live",
    "_validate_tool_results",
):
    setattr(stt, name, lambda value, *args, **kwargs: value)
stt._is_connection_closed_ok = lambda exc: False

runtime = module("gemini_live_behavior.runtime")


class FakeAudioStream:
    def __init__(self) -> None:
        self.chunks = []
        self.finished = False

    def add_chunk(self, chunk: bytes) -> None:
        self.chunks.append(chunk)

    def finish(self) -> None:
        self.finished = True


class FakeTextStream:
    def __init__(self) -> None:
        self.parts = []
        self.finished = False

    @property
    def text(self) -> str:
        return "".join(self.parts)

    def add_chunk(self, chunk: str) -> None:
        self.parts.append(chunk)

    def finish(self) -> None:
        self.finished = True


runtime.AudioStream = FakeAudioStream
runtime.TextStream = FakeTextStream
runtime.new_conversation_id = lambda: "new-cid"

utils = module("gemini_live_behavior.utils")
utils.pcm_to_wav = lambda pcm, rate: b"wav"
utils.resample_24k_to_16k = lambda data: data

safety = load_module(
    "gemini_live_behavior.input_safety",
    COMPONENT_ROOT / "input_safety.py",
)
conversation_module = load_module(
    "gemini_live_behavior.conversation",
    COMPONENT_ROOT / "conversation.py",
)


class FakeSessionManager:
    def __init__(self) -> None:
        self.completed: set[str] = set()
        self.reset_calls: list[str] = []
        self.close_calls: list[str] = []

    def register_chat_session(self, hass, session) -> None:
        pass

    async def async_close(self, conversation_id: str) -> None:
        self.close_calls.append(conversation_id)
        self.completed.discard(conversation_id)

    def complete_conversation(self, conversation_id: str) -> None:
        self.completed.add(conversation_id)

    def reset_conversation(self, conversation_id: str) -> None:
        self.reset_calls.append(conversation_id)
        self.completed.discard(conversation_id)

    def should_continue_conversation(self, conversation_id: str) -> bool:
        return conversation_id not in self.completed


class FakeTurnStore:
    def __init__(self) -> None:
        self.audio: list[tuple[str, bytes]] = []
        self.streaming = []
        self.voice_turn = None

    def take_voice_turn(self, conversation_id: str, text: str):
        turn = self.voice_turn
        self.voice_turn = None
        return turn

    def add_audio(self, text: str, audio: bytes, trace=None) -> None:
        self.audio.append((text, audio))

    def take_trace(self, conversation_id: str, text: str):
        return None

    def add_streaming_audio(self, marker, text_stream, audio_stream, trace=None) -> None:
        self.streaming.append((marker, text_stream, audio_stream, trace))


class FakeChatLog:
    def __init__(self) -> None:
        self.contents = []

    def async_add_assistant_content_without_tools(self, content) -> None:
        self.contents.append(content)


@dataclass
class FakeInput:
    text: str
    conversation_id: str = "cid"
    language: str = "ru"

    def as_llm_context(self, domain: str):
        return object()


class ConversationSafetyBehaviorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.manager = FakeSessionManager()
        self.turn_store = FakeTurnStore()
        self.services = types.SimpleNamespace(async_call=AsyncMock())

        async def async_add_executor_job(func, *args):
            return func(*args)

        self.background_tasks = []

        def async_create_background_task(coro, name):
            task = asyncio.create_task(coro, name=name)
            self.background_tasks.append(task)
            return task

        bus = types.SimpleNamespace(async_fire=Mock())

        self.hass = types.SimpleNamespace(
            data={
                "gemini_live": {
                    "entry": {
                        "session_manager": self.manager,
                        "turn_store": self.turn_store,
                    }
                }
            },
            services=self.services,
            async_add_executor_job=async_add_executor_job,
            async_create_background_task=async_create_background_task,
            bus=bus,
        )
        cls = conversation_module.GeminiLiveConversationAgent
        self.agent = cls.__new__(cls)
        self.agent.hass = self.hass
        self.agent.entry = types.SimpleNamespace(entry_id="entry", data={}, options={})
        self.agent._name = "Gemini Live"
        self.agent._unique_id = "entry_conversation"
        self.agent._engine_safety = safety.EngineSafetyGuard()
        self.agent._engine_cleanup_registered = set()
        self.agent._async_process_text_live = AsyncMock(return_value="локальный ответ")
        self.agent._fire_conversation_entry = Mock()

    async def handle(self, text: str, conversation_id: str = "cid"):
        return await self.agent._async_handle_message(
            FakeInput(text=text, conversation_id=conversation_id),
            FakeChatLog(),
        )

    async def test_foreign_turn_never_reaches_tool_capable_path_or_service(self) -> None:
        result = await self.handle("Arbeitet ihr jetzt?")
        self.assertTrue(result.continue_conversation)
        self.services.async_call.assert_not_awaited()
        self.agent._async_process_text_live.assert_awaited_once()
        self.assertFalse(
            self.agent._async_process_text_live.await_args.kwargs.get(
                "tools_allowed",
                True,
            )
        )
        self.assertEqual(
            self.agent._async_process_text_live.await_args.args[0],
            "Arbeitet ihr jetzt?",
        )
        self.assertEqual(
            self.agent._async_process_text_live.await_args.kwargs["safety_instruction"],
            safety.FOREIGN_READ_ONLY_INSTRUCTION,
        )
        self.agent._fire_conversation_entry.assert_called_once_with(
            "Arbeitet ihr jetzt?",
            "локальный ответ",
            include_text=False,
        )

    async def test_direct_voice_foreign_transcript_bypasses_language_gate(self) -> None:
        self.turn_store.voice_turn = types.SimpleNamespace(
            assistant_text="-- gemini live -- direct",
            audio=b"direct-live-audio",
            assistant_text_stream=None,
        )

        result = await self.handle("Arbeitet ihr jetzt?")

        self.assertTrue(result.continue_conversation)
        self.agent._async_process_text_live.assert_not_awaited()
        self.services.async_call.assert_not_awaited()
        self.assertEqual(
            self.turn_store.audio,
            [("-- gemini live -- direct", b"direct-live-audio")],
        )
        self.agent._fire_conversation_entry.assert_called_once_with(
            "Arbeitet ihr jetzt?",
            "-- gemini live -- direct",
            include_text=False,
        )

    async def test_offline_turn_uses_local_audio_and_ends_without_model_or_tools(self) -> None:
        result = await self.handle(safety.OFFLINE_INPUT_SENTINEL)
        self.assertFalse(result.continue_conversation)
        self.assertEqual(self.manager.close_calls, ["cid"])
        self.services.async_call.assert_not_awaited()
        self.agent._async_process_text_live.assert_not_awaited()
        self.assertEqual(len(self.turn_store.audio), 1)
        text, audio = self.turn_store.audio[0]
        self.assertEqual(text, "Нет подключения к интернету.")
        self.assertTrue(audio.startswith(b"RIFF"))
        self.agent._fire_conversation_entry.assert_called_once_with(
            "",
            "Нет подключения к интернету.",
            include_text=False,
        )

    async def test_normal_path_recovers_immediately_after_offline_turn(self) -> None:
        await self.handle(safety.OFFLINE_INPUT_SENTINEL)
        self.agent._async_process_text_live.reset_mock()
        result = await self.handle("включи свет на кухне", conversation_id="next")
        self.assertTrue(result.continue_conversation)
        self.agent._async_process_text_live.assert_awaited_once()

    async def test_local_stop_calls_no_gemini_or_service_and_ends_session(self) -> None:
        result = await self.handle("стоп")
        self.assertFalse(result.continue_conversation)
        self.assertEqual(self.manager.close_calls, ["cid"])
        self.services.async_call.assert_not_awaited()
        self.agent._async_process_text_live.assert_not_awaited()

    async def test_engine_on_requires_exact_next_russian_confirmation(self) -> None:
        first = await self.handle("заведи машину")
        self.assertTrue(first.continue_conversation)
        self.services.async_call.assert_not_awaited()
        self.assertFalse(
            self.agent._async_process_text_live.await_args.kwargs["tools_allowed"]
        )

        self.agent._async_process_text_live.reset_mock()
        await self.handle("подтверждаю запуск двигателя")
        self.services.async_call.assert_awaited_once_with(
            "switch",
            "turn_on",
            {"entity_id": "switch.honda_jazz_engine"},
            blocking=True,
        )
        self.assertFalse(
            self.agent._async_process_text_live.await_args.kwargs["tools_allowed"]
        )

        self.agent._async_process_text_live.reset_mock()
        await self.handle("подтверждаю запуск двигателя")
        self.assertEqual(self.services.async_call.await_count, 1)
        self.assertNotIn(
            "tools_allowed",
            self.agent._async_process_text_live.await_args.kwargs,
        )

    async def test_bare_yes_cancels_pending_without_engine_service(self) -> None:
        await self.handle("запусти двигатель")
        self.agent._async_process_text_live.reset_mock()
        await self.handle("да")
        self.services.async_call.assert_not_awaited()
        self.assertFalse(
            self.agent._async_process_text_live.await_args.kwargs["tools_allowed"]
        )

    async def test_normal_russian_light_command_reaches_normal_text_path(self) -> None:
        await self.handle("включи свет на кухне")
        self.services.async_call.assert_not_awaited()
        args = self.agent._async_process_text_live.await_args
        self.assertEqual(args.args[0], "включи свет на кухне")
        self.assertNotIn("tools_allowed", args.kwargs)

    async def test_accepted_audio_reaches_turnstore_before_turn_complete(self) -> None:
        allow_complete = asyncio.Event()

        class FakeSession:
            async def send_realtime_input(self, **kwargs) -> None:
                pass

            async def receive(self):
                part = types.SimpleNamespace(
                    text=None,
                    inline_data=types.SimpleNamespace(data=b"\x01\x00" * 60),
                )
                content = types.SimpleNamespace(
                    model_turn=types.SimpleNamespace(parts=[part]),
                    output_transcription=None,
                    turn_complete=False,
                )
                yield types.SimpleNamespace(tool_call=None, server_content=content)
                await allow_complete.wait()
                yield types.SimpleNamespace(
                    tool_call=None,
                    server_content=types.SimpleNamespace(
                        model_turn=None,
                        output_transcription=None,
                        turn_complete=True,
                    ),
                )

        @asynccontextmanager
        async def acquire(*args, **kwargs):
            yield FakeSession()

        self.manager.acquire = acquire
        self.agent.entry.data = {
            "api_key": "test",
            "model": "gemini-native-audio-test",
            "voice": "Puck",
        }
        self.agent._async_get_llm_api = AsyncMock(return_value=(None, [], "safe"))
        conversation_module.genai.Client = lambda **kwargs: object()
        conversation_module.types.HttpOptions = lambda **kwargs: object()
        actual = conversation_module.GeminiLiveConversationAgent._async_process_text_live
        self.agent._async_process_text_live = actual.__get__(self.agent)
        decision = safety.classify_russian_input("включи свет")

        result = await asyncio.wait_for(
            self.agent._async_process_text_live(
                "включи свет",
                FakeInput("включи свет"),
                "cid",
                decision,
            ),
            timeout=1,
        )

        self.assertTrue(result.startswith("-- gemini live --"))
        self.assertFalse(allow_complete.is_set())
        self.assertEqual(len(self.turn_store.streaming), 1)
        _marker, _text, audio, _trace = self.turn_store.streaming[0]
        self.assertTrue(audio.chunks)
        self.assertFalse(audio.finished)
        allow_complete.set()
        await asyncio.gather(*self.background_tasks)
        self.assertTrue(audio.finished)

    async def test_no_tools_turn_rejects_unexpected_tool_call_before_callbacks(self) -> None:
        class FakeSession:
            async def send_realtime_input(self, **kwargs) -> None:
                pass

            async def receive(self):
                yield types.SimpleNamespace(
                    tool_call=types.SimpleNamespace(
                        function_calls=[
                            types.SimpleNamespace(
                                name="end_conversation",
                                args={},
                                id="unsafe",
                            )
                        ]
                    ),
                    server_content=None,
                )

        @asynccontextmanager
        async def acquire(*args, **kwargs):
            yield FakeSession()

        self.manager.acquire = acquire
        self.agent.entry.data = {
            "api_key": "test",
            "model": "gemini-native-audio-test",
            "voice": "Puck",
        }
        conversation_module.genai.Client = lambda **kwargs: object()
        conversation_module.types.HttpOptions = lambda **kwargs: object()
        actual = conversation_module.GeminiLiveConversationAgent._async_process_text_live
        self.agent._async_process_text_live = actual.__get__(self.agent)
        decision = safety.classify_russian_input("Arbeitet ihr jetzt?")

        result = await self.agent._async_process_text_live(
            "Arbeitet ihr jetzt?",
            FakeInput("Arbeitet ihr jetzt?"),
            "cid",
            decision,
            tools_allowed=False,
            safety_instruction=safety.FOREIGN_READ_ONLY_INSTRUCTION,
        )

        self.assertIsNone(result)
        self.assertFalse(self.manager.completed)
        self.services.async_call.assert_not_awaited()
        self.assertFalse(self.turn_store.streaming)

    async def test_end_conversation_before_first_audio_is_visible_during_streaming(self) -> None:
        allow_complete = asyncio.Event()

        class FakeSession:
            async def send_realtime_input(self, **kwargs) -> None:
                pass

            async def send_tool_response(self, **kwargs) -> None:
                pass

            async def receive(self):
                yield types.SimpleNamespace(
                    tool_call=types.SimpleNamespace(
                        function_calls=[
                            types.SimpleNamespace(
                                name="end_conversation",
                                args={},
                                id="finish",
                            )
                        ]
                    ),
                    server_content=None,
                )
                part = types.SimpleNamespace(
                    text=None,
                    inline_data=types.SimpleNamespace(data=b"\x01\x00" * 60),
                )
                yield types.SimpleNamespace(
                    tool_call=None,
                    server_content=types.SimpleNamespace(
                        model_turn=types.SimpleNamespace(parts=[part]),
                        output_transcription=None,
                        turn_complete=False,
                    ),
                )
                await allow_complete.wait()
                yield types.SimpleNamespace(
                    tool_call=None,
                    server_content=types.SimpleNamespace(
                        model_turn=None,
                        output_transcription=None,
                        turn_complete=True,
                    ),
                )

        @asynccontextmanager
        async def acquire(*args, **kwargs):
            yield FakeSession()

        self.manager.acquire = acquire
        self.agent.entry.data = {
            "api_key": "test",
            "model": "gemini-native-audio-test",
            "voice": "Puck",
        }
        self.agent._async_get_llm_api = AsyncMock(return_value=(None, [], "safe"))
        conversation_module.genai.Client = lambda **kwargs: object()
        conversation_module.types.HttpOptions = lambda **kwargs: object()
        conversation_module.types.FunctionResponse = lambda **kwargs: kwargs
        actual = conversation_module.GeminiLiveConversationAgent._async_process_text_live
        self.agent._async_process_text_live = actual.__get__(self.agent)
        decision = safety.classify_russian_input("расскажи историю")

        result = await asyncio.wait_for(
            self.agent._async_process_text_live(
                "расскажи историю",
                FakeInput("расскажи историю"),
                "cid",
                decision,
            ),
            timeout=1,
        )

        self.assertTrue(result.startswith("-- gemini live --"))
        self.assertFalse(self.manager.should_continue_conversation("cid"))
        self.assertEqual(len(self.turn_store.streaming), 1)
        allow_complete.set()
        await asyncio.gather(*self.background_tasks)

    async def test_late_end_conversation_cannot_stale_satellite_lifecycle(self) -> None:
        allow_late_tool = asyncio.Event()

        class FakeSession:
            async def send_realtime_input(self, **kwargs) -> None:
                pass

            async def send_tool_response(self, **kwargs) -> None:
                pass

            async def receive(self):
                part = types.SimpleNamespace(
                    text=None,
                    inline_data=types.SimpleNamespace(data=b"\x01\x00" * 60),
                )
                yield types.SimpleNamespace(
                    tool_call=None,
                    server_content=types.SimpleNamespace(
                        model_turn=types.SimpleNamespace(parts=[part]),
                        output_transcription=None,
                        turn_complete=False,
                    ),
                )
                await allow_late_tool.wait()
                yield types.SimpleNamespace(
                    tool_call=types.SimpleNamespace(
                        function_calls=[
                            types.SimpleNamespace(
                                name="end_conversation",
                                args={},
                                id="late-finish",
                            )
                        ]
                    ),
                    server_content=None,
                )
                yield types.SimpleNamespace(
                    tool_call=None,
                    server_content=types.SimpleNamespace(
                        model_turn=None,
                        output_transcription=None,
                        turn_complete=True,
                    ),
                )

        @asynccontextmanager
        async def acquire(*args, **kwargs):
            yield FakeSession()

        self.manager.acquire = acquire
        self.agent.entry.data = {
            "api_key": "test",
            "model": "gemini-native-audio-test",
            "voice": "Puck",
        }
        self.agent._async_get_llm_api = AsyncMock(return_value=(None, [], "safe"))
        conversation_module.genai.Client = lambda **kwargs: object()
        conversation_module.types.HttpOptions = lambda **kwargs: object()
        conversation_module.types.FunctionResponse = lambda **kwargs: kwargs
        actual = conversation_module.GeminiLiveConversationAgent._async_process_text_live
        self.agent._async_process_text_live = actual.__get__(self.agent)
        decision = safety.classify_russian_input("расскажи историю")

        result = await asyncio.wait_for(
            self.agent._async_process_text_live(
                "расскажи историю",
                FakeInput("расскажи историю"),
                "cid",
                decision,
            ),
            timeout=1,
        )
        self.assertTrue(result.startswith("-- gemini live --"))
        self.assertTrue(self.manager.should_continue_conversation("cid"))

        allow_late_tool.set()
        await asyncio.gather(*self.background_tasks)
        self.assertTrue(self.manager.should_continue_conversation("cid"))

    async def test_output_transcription_does_not_duplicate_model_part_text(self) -> None:
        class FakeSession:
            async def send_realtime_input(self, **kwargs) -> None:
                pass

            async def receive(self):
                part = types.SimpleNamespace(
                    text="Добрый вечер.",
                    inline_data=types.SimpleNamespace(data=b"\x01\x00" * 60),
                )
                yield types.SimpleNamespace(
                    tool_call=None,
                    server_content=types.SimpleNamespace(
                        model_turn=types.SimpleNamespace(parts=[part]),
                        output_transcription=types.SimpleNamespace(
                            text="Добрый вечер."
                        ),
                        turn_complete=True,
                    ),
                )

        @asynccontextmanager
        async def acquire(*args, **kwargs):
            yield FakeSession()

        self.manager.acquire = acquire
        self.agent.entry.data = {
            "api_key": "test",
            "model": "gemini-native-audio-test",
            "voice": "Puck",
        }
        self.agent._async_get_llm_api = AsyncMock(return_value=(None, [], "safe"))
        conversation_module.genai.Client = lambda **kwargs: object()
        conversation_module.types.HttpOptions = lambda **kwargs: object()
        actual = conversation_module.GeminiLiveConversationAgent._async_process_text_live
        self.agent._async_process_text_live = actual.__get__(self.agent)
        decision = safety.classify_russian_input("скажи приветствие")

        await self.agent._async_process_text_live(
            "скажи приветствие",
            FakeInput("скажи приветствие"),
            "cid",
            decision,
        )
        await asyncio.gather(*self.background_tasks)

        self.assertEqual(len(self.turn_store.streaming), 1)
        _marker, text_stream, _audio, _trace = self.turn_store.streaming[0]
        self.assertEqual(text_stream.text, "Добрый вечер.")


if __name__ == "__main__":
    unittest.main()
