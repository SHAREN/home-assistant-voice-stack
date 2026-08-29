from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import importlib.util
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
COMPONENT_ROOT = ROOT / "experimental" / "gemini_live"
PACKAGE = "gemini_live_direct_test"


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


package = module(PACKAGE)
package.__path__ = [str(COMPONENT_ROOT)]

google = module("google")
genai = module("google.genai")
genai_types = module("google.genai.types")
google.genai = genai
genai.types = genai_types


@dataclass
class Blob:
    data: bytes
    mime_type: str


@dataclass
class FunctionResponse:
    name: str
    id: str | None
    response: object


class HttpOptions:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


genai_types.Blob = Blob
genai_types.FunctionResponse = FunctionResponse
genai_types.HttpOptions = HttpOptions
genai.Client = lambda **kwargs: types.SimpleNamespace(kwargs=kwargs)

homeassistant = module("homeassistant")
components = module("homeassistant.components")
ha_stt = module("homeassistant.components.stt")
components.stt = ha_stt
homeassistant.components = components


class _Values:
    WAV = "wav"
    PCM = "pcm"
    HERTZ_16000 = 16000
    CHANNEL_MONO = 1
    BITRATE_16 = 16


@dataclass
class SpeechMetadata:
    language: str = "ru"


class SpeechResultState:
    SUCCESS = "success"
    ERROR = "error"


@dataclass
class SpeechResult:
    text: str | None
    result: str


class SpeechToTextEntity:
    entity_id = "stt.gemini_live"


for name in (
    "AudioBitRates",
    "AudioChannels",
    "AudioCodecs",
    "AudioFormats",
    "AudioSampleRates",
):
    setattr(ha_stt, name, _Values)
ha_stt.SpeechMetadata = SpeechMetadata
ha_stt.SpeechResult = SpeechResult
ha_stt.SpeechResultState = SpeechResultState
ha_stt.SpeechToTextEntity = SpeechToTextEntity

config_entries = module("homeassistant.config_entries")
config_entries.ConfigEntry = object
core = module("homeassistant.core")
core.Context = type("Context", (), {})
core.HomeAssistant = object
homeassistant.config_entries = config_entries
homeassistant.core = core

helpers = module("homeassistant.helpers")
chat_session = module("homeassistant.helpers.chat_session")
chat_session.DATA_CHAT_SESSION = "chat_sessions"
chat_session.current_session = types.SimpleNamespace(get=lambda: None)
chat_session.conversation_id = lambda: None
llm = module("homeassistant.helpers.llm")
llm.LLM_API_ASSIST = "assist"
llm.APIInstance = object
llm.Tool = object


class LLMContext:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


@dataclass
class ToolInput:
    tool_name: str
    tool_args: dict


llm.LLMContext = LLMContext
llm.ToolInput = ToolInput
helpers.chat_session = chat_session
helpers.llm = llm
homeassistant.helpers = helpers
entity_platform = module("homeassistant.helpers.entity_platform")
entity_platform.AddEntitiesCallback = object

const = module(f"{PACKAGE}.const")
for name, value in {
    "CONF_API_KEY": "api_key",
    "CONF_DETAILED_LOGGING": "detailed_logging",
    "CONF_ENCOURAGE_WEB_SEARCH": "encourage_web_search",
    "CONF_MODEL": "model",
    "CONF_P610_LIVE_TEXT": "p610_live_text",
    "CONF_SYSTEM_INSTRUCTION": "system_instruction",
    "CONF_SHOW_TEXT": "show_text",
    "CONF_TRANSCRIBE_GEMINI": "transcribe_gemini",
    "CONF_VOICE": "voice",
    "DEFAULT_TRANSCRIBE_GEMINI": False,
    "DEFAULT_ENCOURAGE_WEB_SEARCH": False,
    "DEFAULT_SYSTEM_INSTRUCTION": "default instruction",
    "DEFAULT_SHOW_TEXT": False,
    "DEFAULT_P610_LIVE_TEXT": True,
    "DOMAIN": "gemini_live",
    "GEMINI_LIVE_TTS_PLACEHOLDER": "-- gemini live --",
    "GEMINI_SESSION_MANAGER_KEY": "session_manager",
    "GEMINI_TURN_STORE_KEY": "turn_store",
    "SUPPORTED_LANGUAGES": ["ru"],
}.items():
    setattr(const, name, value)

observer = module(f"{PACKAGE}.observer")


class TurnTrace:
    instances: list["TurnTrace"] = []

    def __init__(
        self,
        hass,
        entry_id: str,
        conversation_id: str,
        trace_id: str,
        include_text: bool = False,
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.conversation_id = conversation_id
        self.trace_id = trace_id
        self.include_text = include_text
        self.events: list[tuple[str, dict]] = []
        self.instances.append(self)

    def emit(self, stage: str, **data) -> None:
        self.events.append((stage, data))


observer.TurnTrace = TurnTrace

input_safety = load_module(
    f"{PACKAGE}.input_safety",
    COMPONENT_ROOT / "input_safety.py",
)

runtime = module(f"{PACKAGE}.runtime")


class AudioStream:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.finished = False

    def add_chunk(self, chunk: bytes) -> None:
        self.chunks.append(chunk)

    def finish(self) -> None:
        self.finished = True


class TextStream:
    pass


@dataclass
class PipelineTurn:
    conversation_id: str
    user_text: str
    assistant_text: str
    audio: AudioStream
    assistant_text_stream: object | None = None


runtime.AudioStream = AudioStream
runtime.TextStream = TextStream
runtime.PipelineTurn = PipelineTurn
runtime.active_pipeline_conversation_id = lambda hass, entity_id: "conversation-1"

network_safety = module(f"{PACKAGE}.network_safety")
network_safety.initial_network_error_type = (
    lambda exc: "connect_reset" if isinstance(exc, ConnectionResetError) else None
)

finalizer = module(f"{PACKAGE}.transcription_finalizer")
finalizer.TranscriptionFinalizer = object

utils = module(f"{PACKAGE}.utils")
utils.analyze_pcm_metrics = lambda pcm, sample_rate=16000: {}
utils.build_latest_stt_metrics = lambda **kwargs: kwargs
utils.resample_24k_to_16k = lambda chunk: chunk
utils.save_failed_stt_capture = lambda *args, **kwargs: None
utils.save_latest_stt_metrics = lambda *args, **kwargs: None
utils.set_detailed_logging = lambda *args, **kwargs: None

stt = load_module(f"{PACKAGE}.stt", COMPONENT_ROOT / "stt.py")


@dataclass
class FakeCall:
    name: str
    args: dict
    id: str


def content(
    *,
    input_text: str | None = None,
    input_finished: bool = False,
    audio: bytes | None = None,
    output_text: str | None = None,
    turn_complete: bool = False,
):
    transcription = (
        types.SimpleNamespace(text=input_text, finished=input_finished)
        if input_text is not None or input_finished
        else None
    )
    parts = []
    if audio is not None:
        parts.append(
            types.SimpleNamespace(
                text=None,
                inline_data=types.SimpleNamespace(data=audio),
            )
        )
    model_turn = types.SimpleNamespace(parts=parts) if parts else None
    output = (
        types.SimpleNamespace(text=output_text)
        if output_text is not None
        else None
    )
    return types.SimpleNamespace(
        input_transcription=transcription,
        model_turn=model_turn,
        output_transcription=output,
        turn_complete=turn_complete,
    )


def response(*, server_content=None, calls: list[FakeCall] | None = None):
    tool_call = (
        types.SimpleNamespace(function_calls=calls)
        if calls is not None
        else None
    )
    return types.SimpleNamespace(tool_call=tool_call, server_content=server_content)


class FakeSession:
    def __init__(self) -> None:
        self.release_terminal = asyncio.Event()
        self.terminal_sent = False
        self.realtime_inputs: list[dict] = []
        self.tool_responses: list[FunctionResponse] = []

    async def send_realtime_input(self, **kwargs) -> None:
        self.realtime_inputs.append(kwargs)

    async def send_tool_response(self, *, function_responses) -> None:
        self.tool_responses.extend(function_responses)

    async def receive(self):
        yield response(
            server_content=content(
                input_text="Arbeitet ihr jetzt?",
                input_finished=True,
            )
        )
        yield response(
            calls=[FakeCall("HassTurnOn", {"name": "Свет в кухне"}, "normal")]
        )
        yield response(
            calls=[
                FakeCall(
                    "HassTurnOn",
                    {"entity_id": "switch.honda_jazz_engine"},
                    "engine",
                )
            ]
        )
        yield response(
            server_content=content(audio=b"response-pcm", output_text="Готово.")
        )
        await self.release_terminal.wait()
        self.terminal_sent = True
        yield response(server_content=content(turn_complete=True))


class StopFakeSession(FakeSession):
    def __init__(self) -> None:
        super().__init__()
        self.receive_cancelled = False

    async def receive(self):
        try:
            yield response(
                server_content=content(
                    input_text="стоп",
                    input_finished=True,
                )
            )
            yield response(server_content=content(audio=b"must-not-play"))
            await asyncio.Event().wait()
        finally:
            self.receive_cancelled = True


class FakeSessionManager:
    def __init__(self, session: FakeSession | None = None, error=None) -> None:
        self.session = session
        self.error = error
        self.acquire_count = 0
        self.exit_count = 0
        self.completed: set[str] = set()

    def reset_conversation(self, conversation_id: str) -> None:
        self.completed.discard(conversation_id)

    def register_chat_session(self, hass, session) -> None:
        pass

    def complete_conversation(self, conversation_id: str) -> None:
        self.completed.add(conversation_id)

    @asynccontextmanager
    async def acquire(self, conversation_id, client, model, live_config):
        self.acquire_count += 1
        if self.error is not None:
            raise self.error
        assert self.session is not None
        try:
            yield self.session
        finally:
            self.exit_count += 1


class FakeTurnStore:
    def __init__(self) -> None:
        self.turns: list[PipelineTurn] = []
        self.traces: list[tuple[str, str, TurnTrace]] = []

    def add_voice_turn(self, turn: PipelineTurn) -> None:
        self.turns.append(turn)

    def add_trace(self, conversation_id: str, text: str, trace: TurnTrace) -> None:
        self.traces.append((conversation_id, text, trace))


class FakeLLMAPI:
    def __init__(self) -> None:
        self.tools = [object()]
        self.api_prompt = "HA tools"
        self.custom_serializer = None
        self.calls: list[ToolInput] = []

    async def async_call_tool(self, tool_input: ToolInput):
        self.calls.append(tool_input)
        return {"success": True}


class FakeHass:
    def __init__(self, manager: FakeSessionManager, store: FakeTurnStore) -> None:
        self.data = {
            "gemini_live": {
                "entry-1": {
                    "session_manager": manager,
                    "turn_store": store,
                }
            },
            "chat_sessions": {},
        }
        self.states = types.SimpleNamespace(get=lambda entity_id: None)
        self.config = types.SimpleNamespace(path=lambda path: path)

    async def async_add_executor_job(self, target, *args):
        return target(*args)


async def input_audio():
    yield b"\x01\x00" * 1600


class DirectLiveRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.session = FakeSession()
        self.manager = FakeSessionManager(self.session)
        self.store = FakeTurnStore()
        self.hass = FakeHass(self.manager, self.store)
        self.api = FakeLLMAPI()
        llm.async_get_api = lambda **kwargs: asyncio.sleep(0, result=self.api)
        stt._format_tools_for_gemini_live = lambda *args, **kwargs: [
            {"function_declarations": [{"name": "HassTurnOn"}]}
        ]
        stt._add_end_conversation_tool = lambda tools: tools
        stt._add_show_text_tool = lambda tools: tools
        stt._add_end_conversation_instruction = lambda value: value
        stt._add_show_text_instruction = lambda value: value
        stt._add_search_tool_instruction = lambda value, *args: value
        stt._validate_tool_results = lambda value: value
        stt._escape_decode = lambda value: value
        stt._analyse_pcm = lambda pcm: "fake metrics"
        self.entry = types.SimpleNamespace(
            entry_id="entry-1",
            data={"p610_live_text": True},
            options={},
        )
        self.entity = stt.GeminiLiveSTT(self.entry)
        self.entity.hass = self.hass

    async def test_same_session_streams_before_terminal_and_keeps_foreign_text(self):
        loop = asyncio.get_running_loop()
        result_future = loop.create_future()
        run_task = asyncio.create_task(
            self.entity._async_run_direct_live_sdk(
                SpeechMetadata("ru"),
                input_audio(),
                "api-key",
                "gemini-live-native-audio",
                "voice",
                "instruction",
                True,
                False,
                False,
                result_future,
                "trace-1",
            )
        )

        early_result = await asyncio.wait_for(result_future, timeout=1)
        self.assertEqual(self.manager.acquire_count, 1)
        self.assertFalse(self.session.terminal_sent)
        self.assertEqual(early_result.result, SpeechResultState.SUCCESS)
        self.assertEqual(early_result.text, "Arbeitet ihr jetzt?")
        self.assertEqual(len(self.store.turns), 1)
        self.assertEqual(self.store.turns[0].user_text, "Arbeitet ihr jetzt?")
        self.assertEqual(self.store.turns[0].audio.chunks, [b"response-pcm"])

        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(self.api.calls[0].tool_args, {"name": "Свет в кухне"})
        engine_response = next(
            item for item in self.session.tool_responses if item.id == "engine"
        )
        self.assertIn("error", engine_response.response)

        stages = [stage for stage, _data in TurnTrace.instances[-1].events]
        self.assertIn("first_input_transcription", stages)
        self.assertIn("first_response_audio", stages)
        self.assertIn("engine_on_blocked", stages)

        self.session.release_terminal.set()
        final_result = await asyncio.wait_for(run_task, timeout=1)
        self.assertEqual(final_result.result, SpeechResultState.SUCCESS)

    async def test_initial_manager_acquire_network_error_is_classified(self):
        manager = FakeSessionManager(error=ConnectionResetError("offline"))
        trace = TurnTrace(self.hass, "entry-1", "conversation-1", "network")
        with self.assertRaises(stt._GeminiInitialNetworkError):
            async with stt._open_direct_live_session(
                manager,
                "conversation-1",
                object(),
                "model",
                {},
                trace=trace,
            ):
                self.fail("unreachable")
        self.assertEqual(manager.acquire_count, 1)
        self.assertIn(
            "offline_network_detected",
            [stage for stage, _data in trace.events],
        )

    async def test_exact_stop_ends_locally_without_waiting_for_terminal(self):
        stop_session = StopFakeSession()
        manager = FakeSessionManager(stop_session)
        store = FakeTurnStore()
        hass = FakeHass(manager, store)
        entity = stt.GeminiLiveSTT(self.entry)
        entity.hass = hass
        result_future = asyncio.get_running_loop().create_future()

        run_task = asyncio.create_task(
            entity._async_run_direct_live_sdk(
                SpeechMetadata("ru"),
                input_audio(),
                "api-key",
                "gemini-live-native-audio",
                "voice",
                "instruction",
                True,
                False,
                False,
                result_future,
                "trace-stop",
            )
        )

        early_result = await asyncio.wait_for(result_future, timeout=0.5)
        self.assertEqual(early_result.text, input_safety.LOCAL_STOP_SENTINEL)
        final_result = await asyncio.wait_for(run_task, timeout=0.5)
        self.assertEqual(final_result.result, SpeechResultState.SUCCESS)

        self.assertEqual(manager.acquire_count, 1)
        self.assertEqual(manager.exit_count, 1)
        self.assertTrue(stop_session.receive_cancelled)
        self.assertFalse(stop_session.terminal_sent)
        self.assertEqual(self.api.calls, [])
        self.assertEqual(store.turns, [])
        stop_trace = next(
            trace for trace in TurnTrace.instances if trace.trace_id == "trace-stop"
        )
        self.assertIn(
            "local_stop",
            [stage for stage, _data in stop_trace.events],
        )


if __name__ == "__main__":
    unittest.main()
