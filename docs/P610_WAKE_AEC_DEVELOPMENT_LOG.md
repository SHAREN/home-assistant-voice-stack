# P610 Wake/AEC Development Log

This file preserves resumable engineering context for the incremental P610 wake-word / activation-cue / AEC work.

Before each development step, read:

1. [`P610_WAKE_AEC_DEVELOPMENT_PLAN.md`](P610_WAKE_AEC_DEVELOPMENT_PLAN.md)
2. the latest entries in this log.

Do not rely on chat history alone.

---

## 2026-09-09 — Initial context / research handoff

### Current production context

- Physical endpoint: Plantronics P610.
- Production Voice path: Pipecat Assist Proxy.
- Known production line: `0.1.75-proxy30` at the time of the incident/research; re-check live version before any future code change.
- Expected flow: `home-default`.
- Expected realtime model: `gemini-3.1-flash-live-preview`.
- Expected language: `ru-RU`.
- Expected `interrupt_response=true`.
- HA-MCP baseline around the incident: 33 tools; mandatory `HassTurnOn`, `HassTurnOff`, `GetLiveContext`.

### Confirmed incident

A physical P610 turn intended as approximately:

    Окей Набу, включи свет

ended with final STT approximately:

    чи свет

The turn also exhibited very large post-wake latency. Wake detection itself was not the root cause.

### Evidence

Audio-debug comparison showed:

- raw physical microphone recording contained the beginning of the user's speech;
- provider/Gemini input did not contain the same beginning;
- roughly the first 2.8 seconds around activation were missing from the forwarded input;
- missing audio overlapped local wake/activation cue playback;
- current P610 logic can suppress/drop microphone frames while `cue_playing=true` to avoid forwarding the speaker's own activation cue.

This proves the speech truncation can happen before Gemini STT.

### Root cause decision

The existing anti-self-echo behaviour is too coarse. It prevents the local cue from reaching the model by creating a temporary microphone blind spot. When the user continues speaking during that interval, real near-end speech is lost together with the cue.

The permanent design must not depend on dropping the microphone during local playback.

### Research decision

Use reference-based Acoustic Echo Cancellation as the primary architecture.

First production candidate: WebRTC EchoCanceller3 (AEC3), using the actual/late playback PCM as render reference and continuous microphone capture as the capture stream.

If the existing P610 Linux audio topology naturally supports PipeWire echo cancellation without risky migration, evaluate `libpipewire-module-echo-cancel` with WebRTC AEC. Otherwise prefer an in-process AEC3 stage in the existing Pipecat/audio path rather than changing the whole audio server only for AEC.

Calibration should be hybrid:

- optional initial seed measurement of delay/acoustic path;
- continuous online adaptation;
- protect double-talk so the user is not learned/suppressed;
- allow repeated wake cue to help convergence when near-end speech is absent.

Neural residual echo suppression is explicitly deferred until classical AEC3 is correctly aligned, measured and shown insufficient.

### Production invariant established

> Speaker playback may be suppressed; user microphone audio must never be discarded merely because P610 is playing local audio.

### Safe rollout decision

1. Shadow observability first; do not change Gemini input.
2. Shadow AEC3 and measure real P610 signals.
3. Feature-flagged A/B only for wake cue + measured acoustic tail.
4. Calibration/adaptation hardening.
5. Only after cue path is stable, consider all local TTS/playback for general full duplex/barge-in.

Fail-safe must preserve speech: AEC output -> raw/buffered mic -> optionally skip cue. It must not fall back to dropping user frames.

### Required future debug data

Preserve synchronized where possible:

- `mic_raw.wav`
- `render_ref.wav`
- `aec_out.wav`

and timestamps/metrics for wake, cue enqueue/playback, capture, AEC delay/convergence, VAD, Gemini input, tool call and first assistant audio.

### Rollback for documentation changes

- `docs/P610_WAKE_AEC_DEVELOPMENT_PLAN.md` did not exist before commit `5141fb1cb84a8e790bee9c82a7dcdb1a87b7f827`; rollback is to revert that commit or delete the file.
- This development log did not exist before its creation; rollback is to revert/delete this file's creation commit.

### Current phase

**Phase 0 — inspect and baseline the real P610 audio topology.**

### Exact next action

On the next hourly development step, do **read-only inspection first** and record:

1. actual capture/playback device names;
2. ALSA/PulseAudio/PipeWire topology used by production P610;
3. sample rate, sample format, channels and frame sizes;
4. whether capture/playback appear to share one hardware clock;
5. exact activation-cue playback function/path;
6. exact code path/line where `cue_playing` drops or gates mic frames;
7. current buffering and measurable render-to-capture delay;
8. best candidate point for tapping the late render reference;
9. Raspberry Pi 5 CPU headroom while P610 is idle/active.

No production audio-routing change is authorized by Phase 0. After inspection, append all findings here and set one bounded next implementation task.
