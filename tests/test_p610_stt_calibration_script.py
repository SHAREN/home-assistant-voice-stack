from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "p610_stt_calibration.ps1"


class P610SttCalibrationScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_uses_stt_only_lifecycle_and_fresh_listening_gate(self) -> None:
        self.assertIn("assist_satellite/ask_question?return_response", self.source)
        self.assertIn("question_media_id", self.source)
        self.assertIn("$state.state -eq 'listening'", self.source)
        self.assertIn("$changed -ge $requestStarted", self.source)

    def test_checks_quiet_deadline_immediately_before_playback(self) -> None:
        play_position = self.source.index("& ffplay.exe")
        guard_position = self.source.rfind("Assert-BeforeQuietDeadline", 0, play_position)
        self.assertGreater(guard_position, 0)
        self.assertLess(play_position - guard_position, 300)

    def test_rejects_monitor_and_excluded_outputs(self) -> None:
        self.assertIn("P610 monitor must be stopped", self.source)
        self.assertIn("Expected one linux_voice_assistant stream", self.source)
        self.assertIn("Excluded default output", self.source)
        self.assertIn("Quantum350", self.source)
        self.assertIn("Headphones?", self.source)
        self.assertIn("Наушник", self.source)
        self.assertIn("-AllowExcludedOutput is permitted only with -PreflightOnly", self.source)
        self.assertIn("P610 monitor must be stopped with boot manual", self.source)
        self.assertIn("Assist Satellite invariant missing", self.source)
        self.assertIn("version: 1\\.1\\.15-p610\\.4", self.source)
        self.assertIn("session_end_sound: /usr/src/sounds/session_end", self.source)
        self.assertIn("watchdog: false", self.source)

    def test_reserves_full_lifecycle_before_service_call(self) -> None:
        request_position = self.source.index("$client = [System.Net.Http.HttpClient]::new()")
        guard_position = self.source.rfind(
            "Assert-BeforeQuietDeadline ($phraseDuration + 75)",
            0,
            request_position,
        )
        self.assertGreater(guard_position, 0)

    def test_collects_fresh_privacy_safe_metrics(self) -> None:
        self.assertIn("latest_stt_metrics.json", self.source)
        self.assertIn("$capturedAt -ge $requestStarted", self.source)
        self.assertIn("$metrics.turn_id -ne $record.previous_metrics_turn_id", self.source)
        self.assertIn("$record.stt_metrics = $metrics", self.source)
        self.assertIn("latest_lva_stt_pcm_metrics.json", self.source)
        self.assertIn("$lvaCapturedAt -ge $requestStarted", self.source)
        self.assertIn("$lvaMetrics.reason -eq 'streaming_ended'", self.source)
        self.assertIn("$lvaMetrics.settings_changed", self.source)
        self.assertIn("$lvaStartedAt -ge $requestStarted", self.source)
        self.assertIn("$lvaCapturedAt -le $requestCompleted.AddSeconds(2)", self.source)
        self.assertIn("$record.lva_metrics_status = 'process_changed'", self.source)
        self.assertIn("$record.lva_metrics_status = 'sequence_ambiguous'", self.source)
        self.assertIn("previous_lva_capture_sequence + 1", self.source)
        self.assertIn("$record.lva_capture_metrics = $lvaMetrics", self.source)
        self.assertIn("Test-GeminiMetricsPayload $metrics", self.source)
        self.assertIn("[int]$Metrics.schema_version -ne 1", self.source)
        self.assertIn("$capturedAt -le $requestCompleted.AddSeconds(2)", self.source)
        self.assertIn("Gemini and LVA metrics do not fit the same request window", self.source)

    def _run_metrics_validator(
        self,
        payload: dict,
        function_name: str = "Test-GeminiMetricsPayload",
    ) -> subprocess.CompletedProcess[str]:
        pwsh = shutil.which("pwsh") or shutil.which("pwsh.exe")
        if pwsh is None:
            self.skipTest("PowerShell 7 is not installed")
        start = self.source.index("function Test-FiniteNumber")
        end = self.source.index("function Get-HaAudioSnapshot", start)
        functions = self.source[start:end]
        payload_json = json.dumps(payload, ensure_ascii=False)
        validator_script = f"""{functions}
$payloadJson = @'
{payload_json}
'@
$metrics = $payloadJson | ConvertFrom-Json
if ({function_name} $metrics) {{ exit 0 }}
exit 3
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            script_path = Path(temp_dir) / "validate_metrics.ps1"
            script_path.write_text(validator_script, encoding="utf-8")
            return subprocess.run(
                [
                    pwsh,
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )

    @staticmethod
    def _valid_gemini_metrics() -> dict:
        return {
            "schema_version": 1,
            "turn_id": "turn-one",
            "outcome": "stt_success",
            "input_audio_sent": True,
            "pcm": {
                "pcm_bytes": 32000,
                "duration_seconds": 1.0,
                "rms_percent": 2.5,
                "peak_percent": 12.0,
            },
        }

    def test_metrics_validator_accepts_complete_schema_one_payload(self) -> None:
        result = self._run_metrics_validator(self._valid_gemini_metrics())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_metrics_validator_rejects_malformed_payloads(self) -> None:
        cases: dict[str, dict] = {}
        for name in ("schema_version", "turn_id", "outcome", "input_audio_sent"):
            payload = self._valid_gemini_metrics()
            payload.pop(name)
            cases[f"missing_{name}"] = payload
        missing_pcm_field = self._valid_gemini_metrics()
        missing_pcm_field["pcm"].pop("peak_percent")
        cases["missing_peak"] = missing_pcm_field
        numeric_string = self._valid_gemini_metrics()
        numeric_string["pcm"]["rms_percent"] = "2.5"
        cases["numeric_string"] = numeric_string
        over_full_scale = self._valid_gemini_metrics()
        over_full_scale["pcm"]["peak_percent"] = 101
        cases["over_full_scale"] = over_full_scale

        for name, payload in cases.items():
            with self.subTest(name=name):
                result = self._run_metrics_validator(payload)
                self.assertEqual(result.returncode, 3, result.stderr)

    @staticmethod
    def _valid_lva_metrics() -> dict:
        stage = {
            "blocks": 2,
            "pcm_bytes": 2048,
            "duration_seconds": 0.064,
            "rms_percent": 2.5,
            "peak_percent": 12.0,
        }
        return {
            "schema_version": 1,
            "process_instance_id": "process-one",
            "capture_sequence": 1,
            "satellite_generation": 1,
            "window_id": "window-one",
            "started_at": "2026-08-07T16:00:00+00:00",
            "captured_at": "2026-08-07T16:00:01+00:00",
            "reason": "streaming_ended",
            "sample_rate": 16000,
            "block_size": 512,
            "boundary_tolerance_ms": 32.0,
            "pre_webrtc": dict(stage),
            "post_webrtc": dict(stage),
            "settings": {
                "mic_volume": 50,
                "mic_auto_gain": 10,
                "mic_noise_suppression": 0,
                "muted": False,
            },
            "settings_changed": False,
        }

    def test_lva_validator_accepts_complete_schema_one_payload(self) -> None:
        result = self._run_metrics_validator(
            self._valid_lva_metrics(),
            "Test-LvaMetricsPayload",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_lva_validator_rejects_malformed_payloads(self) -> None:
        cases: dict[str, dict] = {}
        for name in ("process_instance_id", "window_id", "started_at", "settings"):
            payload = self._valid_lva_metrics()
            payload.pop(name)
            cases[f"missing_{name}"] = payload
        wrong_reason = self._valid_lva_metrics()
        wrong_reason["reason"] = "satellite_replaced"
        cases["wrong_reason"] = wrong_reason
        settings_string = self._valid_lva_metrics()
        settings_string["settings_changed"] = "false"
        cases["settings_changed_string"] = settings_string
        missing_stage_field = self._valid_lva_metrics()
        missing_stage_field["post_webrtc"].pop("peak_percent")
        cases["missing_post_peak"] = missing_stage_field
        inconsistent_boundary = self._valid_lva_metrics()
        inconsistent_boundary["boundary_tolerance_ms"] = 64
        cases["inconsistent_boundary"] = inconsistent_boundary
        fractional_sequence = self._valid_lva_metrics()
        fractional_sequence["capture_sequence"] = 1.5
        cases["fractional_sequence"] = fractional_sequence

        for name, payload in cases.items():
            with self.subTest(name=name):
                result = self._run_metrics_validator(payload, "Test-LvaMetricsPayload")
                self.assertEqual(result.returncode, 3, result.stderr)

    def test_revalidates_output_and_ha_input_before_every_playback(self) -> None:
        loop_start = self.source.index("foreach ($level in $Levels)")
        play_position = self.source.index("& ffplay.exe", loop_start)
        output_position = self.source.index("$attemptOutput = Get-DefaultOutput", loop_start)
        audio_position = self.source.index("$attemptHaAudio = Get-HaAudioSnapshot", loop_start)
        profile_position = self.source.index("$attemptProfile = Get-P610ProfileSnapshot", loop_start)
        self.assertLess(output_position, play_position)
        self.assertLess(audio_position, play_position)
        self.assertLess(profile_position, play_position)
        self.assertIn("Default output endpoint changed", self.source)
        self.assertIn("Default HA input changed", self.source)
        self.assertIn("P610 wake/microphone profile changed", self.source)

    def test_records_effective_output_and_full_profile(self) -> None:
        self.assertIn("IAudioEndpointVolume", self.source)
        self.assertIn("GetMasterVolumeLevelScalar", self.source)
        self.assertIn("output_endpoint_id", self.source)
        self.assertIn("output_master_volume_percent", self.source)
        self.assertIn("effective_output_percent", self.source)
        self.assertIn("wake_sensitivity", self.source)
        self.assertIn("mic_auto_gain", self.source)
        self.assertIn("mic_noise_suppression", self.source)

    def test_supports_repeated_attempts_and_phrase_fingerprint(self) -> None:
        self.assertIn("[int]$AttemptsPerLevel = 2", self.source)
        self.assertIn("$attemptIndex -le $AttemptsPerLevel", self.source)
        self.assertIn("attempt_index = $attemptIndex", self.source)
        self.assertIn("phrase_sha256 = $phraseSha256", self.source)
        self.assertIn("ResultsPath parent directory does not exist", self.source)

    def test_every_attempt_writes_a_row_from_finally(self) -> None:
        loop_start = self.source.index("foreach ($level in $Levels)")
        finally_start = self.source.index("finally {", loop_start)
        add_content = self.source.index("Add-Content -LiteralPath $resultsFullPath", finally_start)
        stop_after_failure = self.source.index("if ($record.status -eq 'stt_failed' -and $StopAfterFailure)", add_content)
        self.assertLess(finally_start, add_content)
        self.assertLess(add_content, stop_after_failure)
        self.assertIn("status = 'failed'", self.source[loop_start:finally_start])
        self.assertIn("error = $null", self.source[loop_start:finally_start])

    def test_failure_cleanup_disposes_http_and_waits_for_idle(self) -> None:
        finally_start = self.source.index("finally {")
        finally_body = self.source[finally_start:]
        self.assertIn("$client.CancelPendingRequests()", finally_body)
        self.assertIn("$response.Dispose()", finally_body)
        self.assertIn("$content.Dispose()", finally_body)
        self.assertIn("$client.Dispose()", finally_body)
        self.assertIn("Wait-SatelliteIdle $stateUri $headers 15", finally_body)
        self.assertIn("$postAttemptAudio = Get-HaAudioSnapshot", finally_body)
        self.assertIn("post_attempt_audio_healthy", finally_body)

    def test_continuation_requires_clean_idle_stt_failure(self) -> None:
        self.assertIn("continuation_safe = $false", self.source)
        self.assertIn("$record.satellite_idle -and", self.source)
        self.assertIn("$record.post_attempt_audio_healthy -and", self.source)
        self.assertIn("$record.http_status -ge 200", self.source)
        unsafe_abort = self.source.index("Calibration aborted after unsafe failure")
        optional_stop = self.source.index("$record.status -eq 'stt_failed' -and $StopAfterFailure")
        self.assertLess(unsafe_abort, optional_stop)

    def test_success_label_does_not_claim_phrase_correctness(self) -> None:
        self.assertIn("$record.status = 'transcript_present'", self.source)
        self.assertNotIn("$record.status = 'success'", self.source)

    def test_records_system_input_for_each_level(self) -> None:
        loop_start = self.source.index("foreach ($level in $Levels)")
        record_start = self.source.index("$record = [ordered]@{", loop_start)
        record_end = self.source.index("    }", record_start)
        record = self.source[record_start:record_end]
        self.assertIn("system_input_name = $defaultInput.name", record)
        self.assertIn("system_input_volume_percent", record)
        self.assertIn("system_input_muted = [bool]$defaultInput.mute", record)
        self.assertIn("lva_capture_metrics = $null", self.source)

    def test_results_path_guard_uses_directory_boundary(self) -> None:
        self.assertIn("Join-Path $PSScriptRoot '..'", self.source)
        self.assertIn("$resultsFullPath.Equals($repoRoot", self.source)
        self.assertIn(
            "$repoRoot + [IO.Path]::DirectorySeparatorChar",
            self.source,
        )


if __name__ == "__main__":
    unittest.main()
