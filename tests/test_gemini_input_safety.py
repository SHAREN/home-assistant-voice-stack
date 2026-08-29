from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "experimental" / "gemini_live" / "input_safety.py"
STT_PATH = ROOT / "experimental" / "gemini_live" / "stt.py"
CONVERSATION_PATH = ROOT / "experimental" / "gemini_live" / "conversation.py"


def load_policy():
    spec = importlib.util.spec_from_file_location("gemini_live_input_safety_test", POLICY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


policy = load_policy()


class RussianInputPolicyTests(unittest.TestCase):
    def assert_action(self, text: str, expected) -> None:
        self.assertEqual(policy.classify_russian_input(text).action, expected)

    def test_foreign_languages_are_rejected(self) -> None:
        self.assert_action("Arbeitet ihr jetzt?", policy.InputAction.REJECT_FOREIGN)
        self.assert_action("Enciende la luz", policy.InputAction.REJECT_FOREIGN)
        self.assert_action("ライトをつけて", policy.InputAction.REJECT_FOREIGN)

    def test_russian_and_allowlisted_latin_brand_are_accepted(self) -> None:
        self.assert_action("Включи свет на кухне", policy.InputAction.ACCEPT)
        self.assert_action(
            "Включи YouTube на телевизоре",
            policy.InputAction.ACCEPT,
        )
        self.assert_action("Да", policy.InputAction.ACCEPT)

    def test_local_stop_phrases_are_exact_and_composable(self) -> None:
        for text in (
            "стоп",
            "СТОП!",
            "всё",
            "хватит",
            "закончили",
            "до свидания",
            "стоп, всё",
            "стоп, пожалуйста",
        ):
            with self.subTest(text=text):
                self.assert_action(text, policy.InputAction.LOCAL_STOP)
        for text in ("стопка", "остановка", "стоп-сигнал", "включи стоп сигнал"):
            with self.subTest(text=text):
                self.assertNotEqual(
                    policy.classify_russian_input(text).action,
                    policy.InputAction.LOCAL_STOP,
                )

    def test_foreign_text_is_preserved_and_stop_is_hidden(self) -> None:
        foreign = policy.classify_russian_input("Arbeitet ihr jetzt?")
        stop = policy.classify_russian_input("стоп")
        self.assertEqual(
            policy.speech_result_text("Arbeitet ihr jetzt?", foreign),
            "Arbeitet ihr jetzt?",
        )
        self.assertEqual(
            policy.speech_result_text("стоп", stop),
            policy.LOCAL_STOP_SENTINEL,
        )


class EngineSafetyTests(unittest.TestCase):
    def test_engine_on_requires_exact_next_confirmation(self) -> None:
        now = [100.0]
        guard = policy.EngineSafetyGuard(monotonic=lambda: now[0])
        self.assertEqual(
            guard.classify_turn("cid", "заведи машину"),
            policy.EngineLocalAction.REQUEST_CONFIRMATION,
        )
        self.assertEqual(
            guard.classify_turn("cid", "да"),
            policy.EngineLocalAction.CANCELLED,
        )
        self.assertEqual(
            guard.classify_turn("cid", "подтверждаю запуск двигателя"),
            policy.EngineLocalAction.NONE,
        )
        self.assertEqual(
            guard.classify_turn("cid", "запусти двигатель"),
            policy.EngineLocalAction.REQUEST_CONFIRMATION,
        )
        self.assertEqual(
            guard.classify_turn("cid", "подтверждаю запуск двигателя"),
            policy.EngineLocalAction.CONFIRMED_ON,
        )
        self.assertEqual(
            guard.classify_turn("cid", "подтверждаю запуск двигателя"),
            policy.EngineLocalAction.NONE,
        )

    def test_engine_confirmation_expires_and_is_bound_to_conversation(self) -> None:
        now = [100.0]
        guard = policy.EngineSafetyGuard(monotonic=lambda: now[0])
        guard.classify_turn("first", "заведи машину")
        self.assertEqual(
            guard.classify_turn("other", "подтверждаю запуск двигателя"),
            policy.EngineLocalAction.NONE,
        )
        now[0] = 200.0
        self.assertEqual(
            guard.classify_turn("first", "подтверждаю запуск двигателя"),
            policy.EngineLocalAction.NONE,
        )

    def test_engine_off_is_local_and_unconfirmed(self) -> None:
        guard = policy.EngineSafetyGuard()
        self.assertEqual(
            guard.classify_turn("cid", "заглуши машину"),
            policy.EngineLocalAction.OFF,
        )

    def test_stale_engine_tool_exposure_is_blocked(self) -> None:
        for args in (
            {"name": "Машина Двигатель"},
            {"entity_id": "switch.honda_jazz_engine"},
            {"name": "Honda Jazz"},
        ):
            with self.subTest(args=args):
                self.assertTrue(policy.is_engine_on_tool_call("HassTurnOn", args))
        self.assertFalse(
            policy.is_engine_on_tool_call("HassTurnOn", {"name": "Свет Кухня"})
        )
        self.assertTrue(
            policy.is_engine_on_tool_call(
                "HassCallService",
                {
                    "domain": "switch",
                    "service": "turn_on",
                    "entity_id": "switch.honda_jazz_engine",
                },
            )
        )
        self.assertFalse(
            policy.is_engine_on_tool_call(
                "HassCallService",
                {
                    "domain": "switch",
                    "service": "turn_off",
                    "entity_id": "switch.honda_jazz_engine",
                },
            )
        )
        self.assertFalse(
            policy.is_engine_on_tool_call(
                "HassTurnOn",
                {"name": "Стиральная машина"},
            )
        )
        self.assertFalse(
            policy.is_engine_on_tool_call(
                "HassTurnOn",
                {"name": "Мотор вентиляции"},
            )
        )


class DirectLiveArchitectureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.stt_source = STT_PATH.read_text(encoding="utf-8")
        cls.conversation_source = CONVERSATION_PATH.read_text(encoding="utf-8")

    def test_p610_production_uses_one_tool_capable_direct_live_turn(self) -> None:
        start = self.stt_source.index("    async def _async_run_direct_live_sdk(")
        end = self.stt_source.index(
            "    async def _async_run_audio_stream_sdk(",
            start,
        )
        direct = self.stt_source[start:end]
        process = self.stt_source[
            self.stt_source.index("    async def _async_process_audio_stream_sdk(") :
        ]
        self.assertIn("llm.async_get_api", direct)
        self.assertIn("llm_api.async_call_tool", direct)
        self.assertIn('"tools": gemini_tools', direct)
        self.assertIn("_open_direct_live_session", direct)
        self.assertIn("turn_store.add_voice_turn", direct)
        self.assertIn("response_audio_stream.add_chunk", direct)
        self.assertIn("self._async_run_direct_live_sdk(", process)
        self.assertNotIn("self._async_run_audio_stream_sdk(", process)
        self.assertNotIn("classify_russian_input", direct)
        self.assertNotIn("_async_process_text_live", direct)

    def test_first_audio_publishes_without_final_transcript_or_turn_complete(self) -> None:
        start = self.stt_source.index("    async def _async_run_direct_live_sdk(")
        end = self.stt_source.index("    async def _async_run_audio_stream_sdk(", start)
        direct = self.stt_source[start:end]
        publish_start = direct.index("            async def publish_streaming_turn()")
        publish_end = direct.index("            send_task =", publish_start)
        publish = direct[publish_start:publish_end]
        self.assertIn("await first_audio.wait()", publish)
        self.assertIn("turn_store.add_voice_turn", publish)
        self.assertIn("result_future.set_result", publish)
        self.assertNotIn("turn_complete", publish)
        self.assertNotIn("finished", publish)

    def test_engine_on_is_blocked_before_generic_tool_execution(self) -> None:
        start = self.stt_source.index("    async def _async_run_direct_live_sdk(")
        end = self.stt_source.index("    async def _async_run_audio_stream_sdk(", start)
        direct = self.stt_source[start:end]
        engine_guard = direct.index("is_engine_on_tool_call(tool_name, tool_args)")
        generic_call = direct.index("llm_api.async_call_tool", engine_guard)
        self.assertLess(engine_guard, generic_call)
        self.assertIn("engine_on_not_available_in_generic_voice_tools", direct)

    def test_voice_turn_bypasses_language_gate_and_second_gemini_turn(self) -> None:
        handler = self.conversation_source[
            self.conversation_source.index("    async def _async_handle_message(") :
        ]
        voice_lookup = handler.index("voice_turn = turn_store.take_voice_turn")
        language_gate = handler.index("classify_russian_input(input_text)")
        direct_return = handler.index("return conversation.ConversationResult", voice_lookup)
        self.assertLess(voice_lookup, language_gate)
        self.assertLess(direct_return, language_gate)
        direct_branch = handler[voice_lookup:language_gate]
        self.assertIn('language_gate="bypassed"', direct_branch)
        self.assertNotIn("_async_process_text_live", direct_branch)

    def test_conversation_tools_require_accepted_decision(self) -> None:
        process_start = self.conversation_source.index(
            "    async def _async_process_text_live("
        )
        api_load = self.conversation_source.index(
            "self._async_get_llm_api(",
            process_start,
        )
        gate = self.conversation_source.index("safe_for_mode = (", process_start)
        self.assertLess(gate, api_load)

    def test_foreign_and_local_responses_use_no_tool_model_path(self) -> None:
        handler = self.conversation_source[
            self.conversation_source.index("    async def _async_handle_message(") :
        ]
        foreign = handler[
            handler.index("elif safety.action is InputAction.REJECT_FOREIGN") :
            handler.index("else:", handler.index("elif safety.action is InputAction.REJECT_FOREIGN"))
        ]
        self.assertIn("tools_allowed=False", foreign)
        self.assertIn("FOREIGN_READ_ONLY_INSTRUCTION", foreign)
        self.assertIn("input_text,", foreign)
        self.assertNotIn("Пожалуйста, повторите команду по-русски", foreign)
        self.assertIn("self._engine_safety.clear", foreign)
        self.assertIn("tools_allowed=False", handler)
        self.assertIn("if not tools_allowed:", self.conversation_source)
        self.assertIn("raise _UnexpectedNoToolsCall", self.conversation_source)

    def test_local_stop_completes_without_gemini_or_tools(self) -> None:
        handler = self.conversation_source[
            self.conversation_source.index("    async def _async_handle_message(") :
        ]
        stop_branch = handler[
            handler.index("elif safety.action is InputAction.LOCAL_STOP") :
            handler.index("elif safety.action is InputAction.REJECT_FOREIGN")
        ]
        self.assertIn("complete_conversation", stop_branch)
        self.assertNotIn("_async_process_text_live", stop_branch)
        self.assertNotIn("async_call_tool", stop_branch)
        self.assertNotIn("services.async_call", stop_branch)

    def test_normal_russian_path_and_exact_engine_service_remain(self) -> None:
        self.assertIn(
            "assistant_text = await self._async_process_text_live(\n"
            "                        input_text,",
            self.conversation_source,
        )
        self.assertIn('{"entity_id": ENGINE_ENTITY_ID}', self.conversation_source)
        self.assertIn('"switch",\n                        "turn_on"', self.conversation_source)
        self.assertIn("is_engine_on_tool_call(tool_name, tool_args)", self.conversation_source)

    def test_direct_observer_transcript_never_controls_tool_authority(self) -> None:
        start = self.stt_source.index("    async def _async_run_direct_live_sdk(")
        end = self.stt_source.index("    async def _async_run_audio_stream_sdk(", start)
        direct = self.stt_source[start:end]
        self.assertIn('authority="observer_only"', direct)
        self.assertIn('"first_input_transcription"', direct)
        self.assertIn('"first_response_audio"', direct)
        self.assertIn('"tool_call_boundary"', direct)
        self.assertNotIn("InputAction.REJECT_FOREIGN", direct)
        self.assertNotIn("FOREIGN_READ_ONLY_INSTRUCTION", direct)

    def test_direct_local_stop_precedes_voice_turn_handoff(self) -> None:
        start = self.stt_source.index("    async def _async_run_direct_live_sdk(")
        end = self.stt_source.index("    async def _async_run_audio_stream_sdk(", start)
        direct = self.stt_source[start:end]
        publish = direct[direct.index("async def publish_streaming_turn()") :]
        self.assertLess(
            publish.index("is_local_stop_phrase(user_text)"),
            publish.index("turn_store.add_voice_turn"),
        )
        self.assertIn("LOCAL_STOP_SENTINEL", publish)
        self.assertIn("task.cancel()", publish)

    def test_tool_path_recomputes_policy_for_same_text(self) -> None:
        process_start = self.conversation_source.index(
            "    async def _async_process_text_live("
        )
        api_load = self.conversation_source.index(
            "self._async_get_llm_api(",
            process_start,
        )
        verify = self.conversation_source.index(
            "verified_safety = classify_russian_input(user_text)",
            process_start,
        )
        verified_gate = self.conversation_source.index("safe_for_mode = (", process_start)
        self.assertLess(verify, verified_gate)
        self.assertLess(verified_gate, api_load)

    def test_offline_is_local_and_only_initial_network_failure_maps_to_it(self) -> None:
        handler = self.conversation_source[
            self.conversation_source.index("    async def _async_handle_message(") :
        ]
        offline = handler[
            handler.index("if safety.action is InputAction.OFFLINE_NETWORK") :
            handler.index("elif safety.action is InputAction.LOCAL_STOP")
        ]
        self.assertIn("offline_response_wav", offline)
        self.assertIn("complete_conversation", offline)
        self.assertNotIn("_async_process_text_live", offline)
        self.assertNotIn("async_call_tool", offline)
        self.assertIn("except _GeminiInitialNetworkError", self.stt_source)
        self.assertIn("OFFLINE_INPUT_SENTINEL", self.stt_source)
        direct_open = self.stt_source[
            self.stt_source.index("async def _open_direct_live_session(") :
            self.stt_source.index("# Schema / tool helpers")
        ]
        self.assertIn("session_manager.acquire", direct_open)
        self.assertIn("network_type = None if entered else", direct_open)
        self.assertIn("raise _GeminiInitialNetworkError(trace)", direct_open)

    def test_mandatory_russian_instruction_is_appended(self) -> None:
        self.assertIn(
            'f"{system_instruction}\\n\\n{RUSSIAN_ONLY_TOOL_INSTRUCTION}"',
            self.conversation_source,
        )


if __name__ == "__main__":
    unittest.main()
