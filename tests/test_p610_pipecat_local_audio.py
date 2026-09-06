from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "addons/pipecat_assist_proxy/app/main.py").read_text(encoding="utf-8")
CONFIG = (ROOT / "addons/pipecat_assist_proxy/config.yaml").read_text(encoding="utf-8")
DOCKERFILE = (ROOT / "addons/pipecat_assist_proxy/Dockerfile").read_text(encoding="utf-8")
RUN_SH = (ROOT / "addons/pipecat_assist_proxy/root/run.sh").read_text(encoding="utf-8")


def test_addon_exposes_host_audio_and_keeps_p610_opt_in():
    assert "audio: true" in CONFIG
    assert "host_network: true" in CONFIG
    assert "p610_local_audio: true" in CONFIG
    assert "P610_LOCAL_AUDIO_ENABLED" in RUN_SH


def test_image_installs_exact_local_audio_dependency_without_upgrading_pipecat():
    assert "portaudio19-dev" in DOCKERFILE
    assert '"pyaudio~=0.2.14"' in DOCKERFILE
    assert "pip3 install" in DOCKERFILE
    assert "pipecat-ai" not in DOCKERFILE


def test_run_script_exports_p610_local_options():
    for value in [
        "P610_LOCAL_AUDIO_ENABLED",
        "P610_LOCAL_FLOW_ID",
        "P610_WAKE_THRESHOLD",
        "P610_STOP_THRESHOLD",
        "P610_REFRACTORY_SECONDS",
        "P610_STOP_GUARD_SECONDS",
    ]:
        assert value in RUN_SH


def test_physical_p610_uses_same_selected_flow_with_local_audio_transport():
    assert "LocalAudioTransport" in MAIN
    assert "LocalAudioTransportParams" in MAIN
    assert "P610 local audio" in MAIN
    assert "_p610_local_flow_id" in MAIN


def test_status_exposes_full_duplex_health_and_interruption_setting():
    assert "P610_LOCAL_AUDIO_STATE" in MAIN
    assert '"interrupt_response"' in MAIN
    assert '"provider_healthy"' in MAIN
    assert '"standby_state"' in MAIN


def test_p610_stop_recycles_to_fresh_warm_standby_without_memory_reuse():
    assert "recycle_requested" in MAIN
    assert "immediately warming a fresh standby session" in MAIN
    assert "P610 conversation ended" in MAIN


def test_gemini_warm_connection_does_not_generate_initial_greeting():
    assert "inference_on_context_initialization=False" in MAIN
    assert "gemini-quiet-context-prime" in MAIN
    assert "gemini-quiet-context-prime" in MAIN


def test_p610_keeps_audio_preroll_and_buffers_until_provider_ready():
    assert 'P610_AUDIO_PREROLL_SECONDS", 2.0' in MAIN
    assert 'P610_ACTIVATION_BUFFER_MAX_SECONDS", 30.0' in MAIN
    assert "remember_pre_roll" in MAIN
    assert "buffer_activation_frame" in MAIN
    assert "drain_activation_buffer" in MAIN


def test_gemini_tools_and_context_are_preloaded_without_initial_inference():
    assert "tools_schema" in MAIN
    assert "gemini-quiet-context-prime" in MAIN
    assert "context-primed and ready" in MAIN


def test_proxy18_continuous_command_skips_cue_and_trims_wake_phrase():
    assert "wake_cue_skipped_for_continuation" in MAIN
    assert "command_overlap_seconds" in MAIN
    assert "continuation_detected" in MAIN


def test_proxy18_compacts_realtime_tools_without_removing_tools():
    assert "_compact_realtime_tools_schema" in MAIN
    assert "compact_properties" in MAIN


def test_proxy18_pads_p610_output_tail():
    assert "P610_OUTPUT_TAIL_SECONDS" in MAIN or "0.20" in MAIN


def test_proxy19_forces_russian_interpretation_without_unsupported_api_flag():
    assert "language_codes" not in MAIN
    assert "рус" in MAIN.lower()


def test_proxy20_uses_pacat_jitter_buffer_and_gap_metrics():
    assert '"pacat"' in MAIN
    assert "latency-msec=180" in MAIN
    assert "last_response_gap_100ms" in MAIN
    assert "last_response_gap_200ms" in MAIN
    assert "last_response_gap_500ms" in MAIN
    assert "last_response_max_gap_ms" in MAIN


def test_proxy20_silence_tail_uses_saved_sample_rate():
    assert "int(sample_rate * 0.20)" in MAIN


def test_proxy20_does_not_retry_short_non_russian_garbage_on_p610():
    assert "ignore_short_non_cyrillic" in MAIN
    assert 'not re.search(r"[А-Яа-яЁё]", user_text)' in MAIN


def test_proxy23_keeps_short_command_window_open_before_wake_cue():
    assert 'P610_CUE_DECISION_SECONDS", 0.90' in MAIN
    assert "await asyncio.sleep(self._gate.cue_decision_seconds)" in MAIN
    assert "wake_cue_skipped_for_continuation" in MAIN


def test_proxy23_uses_faster_p610_only_gemini_end_of_speech():
    assert "def _gemini_vad(flow: FlowConfig, *, p610_fast_turns: bool = False)" in MAIN
    assert "EndSensitivity.END_SENSITIVITY_HIGH" in MAIN
    assert "silence_duration_ms=350" in MAIN
    assert "p610_fast_turns=bool(p610_wake_gate)" in MAIN
    assert "if not p610_fast_turns:" in MAIN
    assert "return GeminiVADParams(silence_duration_ms=silence_duration_ms)" in MAIN


def test_proxy23_prevents_invalid_entities_search_type():
    assert "omit search_types for entities" in MAIN
    assert '"automation", "dashboard", "helper", "scene", "script"' in MAIN
    assert 'passing "entities" there is invalid in HA-MCP' in MAIN


def test_proxy26_recovers_stale_gemini_readiness_without_global_runtime_restart():
    assert "self.provider_recover_callback = None" in MAIN
    assert "p610-active-provider-recovery" in MAIN
    assert "wake_provider_not_ready" in MAIN
    assert "active_turn_provider_not_ready" in MAIN
    assert "await self._gate.provider_recover_callback()" in MAIN
    assert "await llm._reconnect()" in MAIN
    assert "p610-provider-readiness-monitor" in MAIN
    assert "recycling only P610 worker" in MAIN


def test_proxy26_activation_overflow_is_not_sticky_forever():
    assert 'P610_LOCAL_AUDIO_STATE["activation_buffer_overflow"] = False' in MAIN
    assert "activation_buffer_overflow_count" in MAIN
    assert "last_activation_buffer_overflow_at" in MAIN
    assert "last_activation_buffer_overflow_recovered_at" in MAIN


def test_proxy26_status_tracks_private_gemini_realtime_readiness():
    assert "provider_input_ready" in MAIN
    assert "provider_input_ready_checked_at" in MAIN
    assert "last_provider_reconnect_duration_ms" in MAIN


def test_proxy26_supports_model_driven_conversation_end_after_farewell():
    assert "_p610_end_conversation_tool_schema" in MAIN
    assert 'name="end_conversation"' in MAIN
    assert "request_model_end" in MAIN
    assert "model_end_completed" in MAIN
    assert "completed after farewell" in MAIN


def test_proxy26_supports_optional_thinking_cue_for_long_work():
    assert "P610_THINKING_CUE_PATH" in MAIN
    assert "_p610_thinking_signal_tool_schema" in MAIN
    assert 'name="thinking_signal"' in MAIN
    assert "p610-thinking-cue" in MAIN
    assert (ROOT / "addons/pipecat_assist_proxy/sounds/thinking.wav").is_file()


def test_proxy28_never_runs_proactive_reconnect_during_active_conversation():
    assert 'if p610_wake_gate.active:' in MAIN
    assert 'proactive_reconnect_deferred_reason' in MAIN
    assert '"active_conversation"' in MAIN


def test_proxy28_active_idle_timeout_starts_on_wake_and_respects_busy_work():
    assert 'P610_ACTIVE_IDLE_TIMEOUT_SECONDS' in MAIN
    assert 'self._restart_idle_watch()' in MAIN
    assert 'name="p610-active-idle-timeout"' in MAIN
    assert 'provider_recovery_in_progress' in MAIN
    assert '_context_has_pending_tool' in MAIN
    assert 'p610_active_idle_timeout_seconds: 30' in CONFIG
