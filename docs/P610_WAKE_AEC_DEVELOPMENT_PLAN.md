# P610 Wake/AEC Development Plan

## Purpose

Turn the physical Plantronics P610 path into a true full-duplex voice endpoint where the user can say a continuous phrase such as:

    Окей Набу, включи свет

without waiting for the activation cue to finish and without losing the beginning of the command.

This plan is the source of truth for incremental wake-word / activation-cue / acoustic-echo-cancellation development.

Related development log:

- [`P610_WAKE_AEC_DEVELOPMENT_LOG.md`](P610_WAKE_AEC_DEVELOPMENT_LOG.md)

## Confirmed incident and root cause

A real P610 turn produced final STT approximately as `"чи свет"` instead of the intended `"включи свет"`.

Audio-debug comparison showed:

- `mic_raw` contained the user's beginning of speech;
- the audio actually forwarded to Gemini did not contain the beginning;
- roughly the first 2.8 seconds around wake/activation were missing from the provider input;
- the missing interval overlapped local activation-cue playback;
- the current implementation can drop microphone frames while `cue_playing=true` in order to prevent the local cue from being interpreted as user speech.

Therefore the primary fault is before Gemini STT: the production audio gate can create an acoustic blind spot while the P610 is playing its own cue.

## Production invariant

> **P610 must never discard user microphone frames merely because P610 itself is playing local audio.**

The speaker's own playback may be cancelled/suppressed. User speech must be preserved.

Fail-safe preference order:

1. clean AEC output;
2. raw or safely buffered microphone audio;
3. temporarily suppress/skip local cue if echo cancellation is unhealthy;
4. never return to intentionally dropping the user's microphone interval as the normal fallback.

## Target architecture

Preferred first production technology: WebRTC EchoCanceller3 (AEC3), either through the existing Linux audio graph when that is naturally supported or embedded in-process if changing the audio server would be riskier.

Target data flow:

```text
wake cue / local TTS
        |
        v
final digital playback processing
        |--------------------> AEC render reference
        v
     speaker
        |
        | speaker + room + reflections + device body
        v
microphone ------------------> AEC3 ------------------> cleaned capture
user speech -----------------/                             |
                                                         VAD / Gemini realtime
```

The render reference should be taken as late in the digital playback chain as practical, after the processing that materially changes what is sent to the speaker (for example volume/resampling/EQ/limiting), so that it matches the physical playback as closely as possible.

## Calibration strategy

Use a hybrid approach:

- an initial device/room seed measurement where useful;
- continuous online AEC adaptation during normal use;
- the known repeated wake cue may act as a recurring reference/calibration probe when the near-end user is silent;
- preserve double-talk protection so adaptation does not learn/suppress the user's simultaneous speech.

The implementation must tolerate changes in:

- P610 speaker volume;
- speaker/microphone acoustic coupling;
- placement relative to walls/furniture;
- room reflections/reverberation;
- playback/capture latency and jitter;
- possible sample-clock drift;
- moderate nonlinear speaker/amplifier behaviour.

Neural residual echo suppression is a later optional second stage only if a correctly aligned classical AEC3 path leaves significant nonlinear residual echo.

## Mandatory observability

For every testable physical P610 turn, the debug pipeline should eventually be able to preserve synchronized tracks:

- `mic_raw.wav` — unmodified physical microphone capture;
- `render_ref.wav` — exact/late playback reference given to the canceller;
- `aec_out.wav` — cleaned microphone stream sent downstream.

Timeline should expose at least:

- `wake_detected`;
- `cue_render_enqueued`;
- `cue_first_sample_playback` when measurable;
- `capture_first_sample_after_wake`;
- AEC delay estimate / convergence event where available;
- VAD speech start/end;
- Gemini audio start / turn commit;
- first model event;
- first tool call;
- first assistant audio.

Useful AEC metrics when available:

- estimated render/capture delay;
- render level and microphone level;
- ERLE or another residual-echo measurement;
- clipping/saturation indicators;
- near-end/double-talk state;
- echo-path-change / convergence state;
- processing latency and CPU cost.

## Incremental development phases

### Phase 0 — Inspect and baseline the real P610 audio topology

Status: **NEXT**

Before changing production audio behaviour, determine and record:

- actual P610 capture device and playback device;
- whether production currently uses ALSA, PulseAudio, PipeWire or a combination;
- actual sample formats, rates, channel counts and frame sizes;
- whether capture/playback share the same hardware clock;
- exact wake-cue playback path;
- exact location where `cue_playing` currently drops/gates microphone frames;
- buffering and measured playback-to-capture delay;
- where the most faithful render reference can be tapped;
- CPU headroom on the Raspberry Pi 5.

Acceptance:

- topology is documented from running production, not assumed;
- current frame-drop line/path is identified;
- no production audio routing has been changed.

### Phase 1 — Add AEC observability without changing Gemini input

Add synchronized render-reference capture, candidate AEC output and timing/metrics in **shadow mode**.

Gemini must still receive the current production stream during this phase.

Acceptance:

- `mic_raw`, `render_ref` and `aec_out` can be aligned for a test turn;
- no measurable regression in existing production Voice;
- shadow processing has bounded CPU/RAM cost;
- rollback tested.

### Phase 2 — Shadow WebRTC AEC3 prototype

Run AEC3 in parallel with production capture. Do not route it to Gemini yet.

Measure:

- delay estimation/convergence;
- cue residual;
- preservation of simultaneous user speech;
- added processing latency;
- Raspberry Pi 5 CPU budget.

Acceptance:

- cue is materially reduced in `aec_out`;
- first phonemes of overlapping speech survive;
- no severe speech distortion during double-talk;
- AEC remains stable across repeated wakes.

### Phase 3 — Feature-flagged cue-only A/B routing

Route AEC output to Gemini only during wake-cue playback plus a measured acoustic tail. Keep raw/debug tracks and a fast feature-flag rollback.

Acceptance requires repeated real P610 tests in the overlap matrix below. Any clipped first phoneme, abnormal STT regression, provider self-interruption or instability blocks promotion.

### Phase 4 — Calibration and online adaptation hardening

Validate across:

- multiple P610 volumes;
- near/far wall placement;
- P610 moved after convergence;
- quiet and loud user speech;
- different overlap positions;
- room changes where practical.

If needed, introduce an initial seed calibration, but keep online adaptation authoritative.

### Phase 5 — General local-playback full duplex

After cue-only AEC is stable, evaluate using the same AEC path for all local playback/TTS so the user can naturally interrupt the assistant.

This phase is not complete until barge-in works without self-triggering and without degrading direct-command latency.

## Required overlap regression matrix

Test at minimum:

1. cue only, user silent;
2. user starts at cue start;
3. user starts 50–100 ms after cue start;
4. user starts in the middle of cue;
5. user starts near cue end;
6. user starts immediately after cue while acoustic tail remains;
7. continuous `Окей Набу, включи свет` without an intentional pause;
8. quiet speech and loud speech;
9. several speaker-volume levels;
10. P610 near a wall and farther from a wall;
11. after physically moving P610 and allowing reconvergence.

For direct commands such as `Торшеры`, also verify the normal Voice control invariant: the action must have a successful corresponding HA tool call before the assistant confirms it.

## Acceptance criteria

A phase may be marked complete only when its evidence is in the development log.

For production promotion the minimum behavioural criteria are:

- no microphone-frame loss caused by local playback;
- first command phoneme preserved during cue overlap;
- expected Russian STT for continuous wake+command phrases;
- cue residual does not trigger false VAD/STT/model behaviour;
- simultaneous near-end speech is not aggressively suppressed;
- no new multi-second latency attributable to AEC;
- no unacceptable CPU/RAM pressure on Raspberry Pi 5;
- feature flag / rollback path has been tested;
- existing P610 invariants and MCP control path remain healthy.

## Hourly development procedure

The Home Assistant hourly health pass should treat this plan as a resumable engineering queue, **after critical health work**.

On each hourly pass:

1. Run the normal Home Assistant health checklist first. New CRIT incidents take priority.
2. Read this entire plan and the latest entries in `P610_WAKE_AEC_DEVELOPMENT_LOG.md` before touching code.
3. Select the smallest unblocked, low-risk next step from the current phase.
4. Before every change, preserve rollback: original file/config or exact hash + old fragment. For a new file, record that the old state was absent and record the commit/path needed to delete/revert it.
5. Make only the bounded change needed for that step.
6. Run offline/unit/synthetic tests first when possible.
7. When the current phase calls for hardware validation, run a safe real P610 test and inspect `mic_raw` / `render_ref` / `aec_out`, transcript, tool calls and latency.
8. If the test fails, immediately restore the rollback state and record the failure.
9. Append the development log with timestamp, current proxy/version, task, files/config changed, rollback location/hash, tests/metrics, result, blocker and the **exact next action**.
10. Do not mark an item or phase complete without its acceptance evidence.
11. Once a feature reaches real-world validation, subsequent hourly passes should keep examining new P610 turns until enough evidence exists to promote it.

Development must never replace the ordinary health review and must never auto-perform firmware updates, HAOS/Core reboot, firewall/routing changes, destructive actions or high-risk migrations.

## External technical references

- WebRTC EchoCanceller3 source/API: <https://webrtc.googlesource.com/src/+/refs/heads/main/modules/audio_processing/aec3/echo_canceller3.h>
- WebRTC AEC3 configuration: <https://webrtc.googlesource.com/src/+/refs/heads/main/api/audio/echo_canceller3_config.h>
- PipeWire echo-cancel module: <https://pipewire.pages.freedesktop.org/pipewire/page_module_echo_cancel.html>
- SpeexDSP AEC background/API notes: <https://www.speex.org/docs/manual/speex-manual/node7.html>
- ICASSP 2023 AEC Challenge: <https://arxiv.org/abs/2309.12553>
