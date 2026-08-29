from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "addons/pipecat_assist_proxy/app/main.py").read_text(encoding="utf-8")
CONFIG = (ROOT / "addons/pipecat_assist_proxy/config.yaml").read_text(encoding="utf-8")
DOCKERFILE = (ROOT / "addons/pipecat_assist_proxy/Dockerfile").read_text(encoding="utf-8")
RUN_SH = (ROOT / "addons/pipecat_assist_proxy/root/run.sh").read_text(encoding="utf-8")


def test_addon_exposes_host_audio_and_keeps_p610_opt_in():
    assert "audio: true" in CONFIG
    assert "p610_local_audio: false" in CONFIG
    assert 'p610_flow_id: "home-default"' in CONFIG
    assert "p610_local_audio: bool" in CONFIG


def test_image_installs_exact_local_audio_dependency_without_upgrading_pipecat():
    assert '"pyaudio~=0.2.14"' in DOCKERFILE
    assert "portaudio19-dev" in DOCKERFILE
    assert "pipecat-ai[local]" not in DOCKERFILE


def test_run_script_exports_p610_local_options():
    assert "P610_LOCAL_AUDIO_ENABLED" in RUN_SH
    assert "P610_LOCAL_FLOW_ID" in RUN_SH
    assert "export P610_LOCAL_AUDIO_ENABLED" in RUN_SH
    assert "export P610_LOCAL_FLOW_ID" in RUN_SH


def test_physical_p610_uses_same_selected_flow_with_local_audio_transport():
    assert "LocalAudioTransport" in MAIN
    assert "LocalAudioTransportParams" in MAIN
    assert 'flow_id = _p610_local_flow_id()' in MAIN
    assert 'config.selected_flow(flow_id)' in MAIN
    assert '"source": "p610-local-audio"' in MAIN
    assert "runner_args.pipeline_idle_timeout_secs = 0" in MAIN
    assert "audio_in_enabled=True" in MAIN
    assert "audio_out_enabled=True" in MAIN


def test_status_exposes_full_duplex_health_and_interruption_setting():
    assert '"p610_local_audio": {' in MAIN
    assert '"task_alive": bool(P610_LOCAL_AUDIO_TASK' in MAIN
    assert '"interrupt_response": bool(flow.interrupt_response)' in MAIN


def test_p610_stop_recycles_to_fresh_warm_standby_without_memory_reuse():
    assert 'session_memory_enabled = _memory_enabled(config, flow) and p610_wake_gate is None' in MAIN
    assert 'P610_LOCAL_AUDIO_STATE["recycle_requested"] = True' in MAIN
    assert 'asyncio.create_task(worker.cancel(), name="p610-stop-recycle")' in MAIN
    assert 'immediately warming a fresh standby session' in MAIN
    assert 'P610_LOCAL_AUDIO_STATE["standby_state"] = "ready"' in MAIN


def test_gemini_warm_connection_does_not_generate_initial_greeting():
    assert 'inference_on_context_initialization=False' in MAIN
    assert 'flow.greeting.strip() and provider_kind != "gemini"' in MAIN


def test_p610_keeps_audio_preroll_and_buffers_until_provider_ready():
    assert 'P610_AUDIO_PREROLL_SECONDS' in MAIN
    assert 'P610_ACTIVATION_BUFFER_MAX_SECONDS' in MAIN
    assert 'self._gate.remember_pre_roll(frame)' in MAIN
    assert 'self._gate.begin_activation_buffer()' in MAIN
    assert 'not self._gate.provider_ready()' in MAIN
    assert 'self._gate.drain_activation_buffer()' in MAIN
    assert 'P610 flushing {} buffered microphone frames after wake' in MAIN


def test_gemini_tools_and_context_are_preloaded_without_initial_inference():
    assert 'tools=tools_schema' in MAIN
    assert 'inference_on_context_initialization=False' in MAIN
    assert 'asyncio.create_task(llm._handle_context(context), name="gemini-quiet-context-prime")' in MAIN
    assert 'flow.greeting.strip() and provider_kind != "gemini"' in MAIN


def test_proxy18_continuous_command_skips_cue_and_trims_wake_phrase():
    assert 'P610_COMMAND_OVERLAP_SECONDS' in MAIN
    assert 'P610_CUE_DECISION_SECONDS' in MAIN
    assert 'P610_CONTINUATION_RMS_THRESHOLD' in MAIN
    assert 'wake_cue_skipped_for_continuation' in MAIN
    assert 'waiting briefly for continuous command' in MAIN


def test_proxy18_compacts_realtime_tools_without_removing_tools():
    assert 'def _compact_realtime_tools_schema' in MAIN
    assert 'compact_properties = strip_descriptions(schema.properties)' in MAIN
    assert 'tools=_compact_realtime_tools_schema(tools_schema)' in MAIN


def test_proxy18_pads_p610_output_tail():
    assert 'int(sample_rate * 0.20)' in MAIN
    assert 'sample_rate * 0.20' in MAIN



def test_proxy19_forces_russian_interpretation_without_unsupported_api_flag():
    assert 'ВАЖНО ДЛЯ ГОЛОСА' in MAIN
    assert 'пользователь говорит по-русски' in MAIN
    assert 'patch_gemini_transcription.py' not in DOCKERFILE


def test_proxy20_uses_pacat_jitter_buffer_and_gap_metrics():
    assert '"pacat"' in MAIN
    assert '"--latency-msec=180"' in MAIN
    assert 'last_response_gap_200ms' in MAIN
    assert 'recent_audio_gaps_ms' in MAIN
    assert 'last_response_first_audio_after_wake_ms' in MAIN
    assert 'pulseaudio-utils' in DOCKERFILE


def test_proxy20_silence_tail_uses_saved_sample_rate():
    assert 'sample_rate = self._pcm_output_sample_rate' in MAIN
    assert 'int(sample_rate * 0.20)' in MAIN


def test_proxy20_does_not_retry_short_non_russian_garbage_on_p610():
    assert 'ignore_short_non_cyrillic' in MAIN
    assert 'not re.search(r"[А-Яа-яЁё]", user_text)' in MAIN


def test_proxy23_keeps_short_command_window_open_before_wake_cue():
    assert 'P610_CUE_DECISION_SECONDS", 0.90' in MAIN
    assert 'await asyncio.sleep(self._gate.cue_decision_seconds)' in MAIN
    assert 'wake_cue_skipped_for_continuation' in MAIN


def test_proxy23_uses_faster_p610_only_gemini_end_of_speech():
    assert 'def _gemini_vad(flow: FlowConfig, *, p610_fast_turns: bool = False)' in MAIN
    assert 'EndSensitivity.END_SENSITIVITY_HIGH' in MAIN
    assert 'silence_duration_ms=350' in MAIN
    assert 'p610_fast_turns=bool(p610_wake_gate)' in MAIN
    assert 'if not p610_fast_turns:' in MAIN
    assert 'return GeminiVADParams(silence_duration_ms=silence_duration_ms)' in MAIN


def test_proxy23_prevents_invalid_entities_search_type():
    assert 'omit search_types for entities' in MAIN
    assert '"automation", "dashboard", "helper", "scene", "script"' in MAIN
    assert 'passing "entities" there is invalid in HA-MCP' in MAIN
