# P610 deployed production state — 2026-09-23

This document preserves the **current deployed behavior and recovery contract** for the physical Plantronics P610 Home Assistant voice endpoint.

It exists because the public repository runtime source currently trails the deployed Home Assistant copy. The repository still contains an older `proxy28`-era application snapshot, while the production runbook documents `0.1.75-proxy39`.

## Important recovery warning

Do **not** treat the current GitHub add-on source as a byte-for-byte recovery image of production `proxy39`.

The behavior below is confirmed by the canonical local runbook, but the exact deployed source files for the later proxy revisions must be copied from the production runbook tree before the repository version number is advanced. Do not hand-reconstruct production Python and then label it proxy39.

Canonical local source-of-truth paths:

- `/home/renat/runbooks/pipecat-assist/README.md`
- `/home/renat/runbooks/pipecat-assist/app/main.py`
- `/home/renat/runbooks/pipecat-assist/app/control_guard.py`
- `/home/renat/runbooks/pipecat-assist/app/mcp_bridge.py`
- `/home/renat/runbooks/pipecat-assist/app/config.yaml`
- `/home/renat/runbooks/pipecat-assist/sounds/control_on.wav`
- `/home/renat/runbooks/pipecat-assist/sounds/control_off.wav`
- `/home/renat/runbooks/home-assistant/health-checklist.md`

## Production baseline

As of 2026-09-23:

- Pipecat Assist Proxy: `0.1.75-proxy39`
- active flow: `home-default`
- Gemini model: `gemini-3.8-live`
- language: `ru-RU`
- physical endpoint: Plantronics P610 through Pipecat `LocalAudioTransport`
- `interrupt_response=true`
- Home Assistant MCP baseline: 33 tools; required control/context tools must remain present
- local wake word: `Okay Nabu`, threshold `0.85`
- local stop word: `Stop`, threshold `0.5`
- wake refractory interval: about 2 s
- active-session genuine-idle timeout: about 30 s
- pre-roll: about 2 s
- wake overlap: about 0.4 s
- response playback: `pacat` / PulseAudio with a small ~180 ms jitter buffer
- session audio-debug retention: 24 hours

## Wake and continuous speech invariant

The current production invariant is stricter than the old cue-skip implementation:

1. `Okay Nabu` is detected locally.
2. The activation cue starts immediately.
3. Microphone capture **continues while the cue is playing**.
4. Pre-roll, wake overlap, and cue-time microphone frames remain buffered locally.
5. Buffered command audio is flushed in order once the realtime provider is ready.
6. The physical P610 echo canceller is expected to suppress most of the device's own cue.
7. A phrase such as `Okay Nabu, выключи свет` must be speakable without waiting for the cue.

User microphone frames must not be dropped just because the P610 is producing local playback.

## Stop and semantic conversation end

Two termination paths intentionally coexist:

- local `Stop`: immediate no-model interruption, playback cancellation, end cue, worker recycle, fresh warm standby;
- model-driven `end_conversation`: used only when the user semantically asks to end the voice dialogue/listening session itself.

Ordinary device/media/timer stop requests must not terminate the voice session.

After the 2026-09-06 self-wake incident, the end-cue path blocks wake processing for the entire end cue plus an acoustic-tail guard (production value about 1.0 s) and carries that guard across the immediate worker recycle.

## Provider lifecycle

The later production revisions changed provider handling substantially:

- an initially unready Gemini worker gets a bounded startup grace and isolated reconnects with backoff rather than restarting Home Assistant;
- provider `GoAway` is treated as planned session rotation and reconnects after the active turn becomes idle;
- the latest session-resumption handle is preserved so context can resume instead of always starting a fresh conversation;
- the old fixed 120 s proactive reconnect is disabled;
- production enables Gemini Live context-window compression using the provider/Pipecat sliding-window integration;
- route probing is event-driven around wake/readiness failure rather than continuously adding setup traffic to ordinary watchdog probes.

## Voice control guard

A critical production change is the low-latency guard in front of mutating Home Assistant actions.

Reason: Gemini Live can propose a function call before the final STT transcript is committed.

Production rules:

- use both committed final STT and the live unflushed transcription buffer when available;
- wait for a fresh actionable transcript instead of automatically cancelling every mid-utterance proposal;
- verify direction: `HassTurnOn` requires explicit on-intent, `HassTurnOff` requires explicit off-intent;
- exact command-token logic prevents status/past-tense phrases such as `выключен` / `выключил` from authorizing a side effect;
- speech quiet debounce is about **0.20 s**;
- total guard deadline is capped at about **0.80 s**;
- if speech is stale or ambiguous, fail closed quickly rather than waiting 8-10 seconds;
- while a mutating MCP call is pending, drop Gemini PCM generated before the real tool result so the assistant cannot audibly claim success before Home Assistant accepts the action.

Critical invariant: any spoken confirmation that something was turned on/off or is already in a state must be backed by a successful matching control call or a fresh trustworthy state check in that turn.

## Deterministic on/off success tones

Production `proxy39` replaces spoken success acknowledgements for simple `HassTurnOn` / `HassTurnOff` actions with local tones played only after a real successful Home Assistant result:

- ON: soft rising C5 -> E5;
- OFF: the same interval descending E5 -> C5;
- duration: about 247 ms;
- Gemini is informed that local feedback already played;
- speculative/duplicate acknowledgement audio is suppressed briefly so it should not say `включила`, `выключила`, or `готово` for those actions.

Exact deployed assets are `control_on.wav` and `control_off.wav`. They should be copied byte-for-byte from the canonical local runbook rather than regenerated and silently treated as identical.

## Relevant production revision history

The canonical local runbook records these important later revisions:

- **proxy29** — prevents the P610 from waking itself on `session_end.wav`; carries the end-cue acoustic-tail guard across worker recycle; includes 30 s active idle timeout behavior.
- **proxy32** — recovers initial-standby provider deadlock with bounded isolated reconnect/backoff.
- **proxy33** — planned `GoAway` rotation/session resumption; event-driven route probing; old fixed proactive reconnect disabled.
- **proxy34** — Gemini Live context-window compression.
- **proxy35** — fixes realtime function-call/final-STT ordering by allowing the final transcript time to arrive instead of poisoning the turn with an artificial tool failure.
- **proxy37** — uses live unflushed transcription, waits for fresh speech, verifies on/off direction, blocks status/past-tense authorization, and suppresses premature success PCM.
- **proxy38** — removes multi-second guard latency: ~0.20 s quiet debounce, ~0.80 s total deadline.
- **proxy39** — deterministic local ON/OFF success tones after real Home Assistant success; suppresses spoken duplicate acknowledgements.

## Diagnostics worth preserving

The local diagnostics/runbooks contain valuable incident evidence that should remain local rather than be blindly published:

- P610 guard race capture;
- P610 wake/tool latency capture;
- P610 VAD latency capture;
- synchronized `mic_raw.wav`, `gemini_input.wav`, `assistant_raw.wav`, `assistant_played.wav`, `mix_stereo.wav`, and `timeline.json`;
- hourly Home Assistant health logs and rollback bundles.

Do not publish raw room recordings or conversation logs to this public repository. Preserve only sanitized technical findings, regression tests, and reproducible fixes.

## Next exact-source sync checklist

Before declaring GitHub equal to production:

1. copy the exact canonical production text sources from the local runbook tree;
2. diff them against the GitHub add-on;
3. remove local secrets/endpoints while preserving behavior through configuration;
4. copy `control_on.wav` and `control_off.wav` byte-for-byte;
5. bring over the matching regression tests;
6. run syntax/tests and compare hashes of the staged deployable files;
7. only then advance the public add-on version from the older snapshot to `proxy39`;
8. update this document from "behavior/recovery contract" to "exact source synchronized".
