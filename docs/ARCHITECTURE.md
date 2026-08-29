# Architecture

## Relationship to Pipecat

This repository is not a fork of the Pipecat core repository. The layers are:

1. Pipecat - realtime voice framework.
2. kyvaith/pipecat-homeassistant - upstream Home Assistant application and base image.
3. SHAREN/home-assistant-voice-stack - this derivative application layer.

The main Dockerfile currently starts from the upstream image and then copies the modified runtime plus targeted compatibility patches.

## Main data paths

~~~mermaid
flowchart TD
    P610Mic["P610 mic"] --> LocalWake["microWakeWord Okay Nabu / Stop"]
    LocalWake --> Gate["P610 local audio gate"]
    Gate --> UserAgg["Pipecat user aggregator"]
    WebRTC["Browser WebRTC"] --> UserAgg
    UserAgg --> Gemini["Gemini Live"]
    Gemini --> Tools["MCP tool calls"]
    Tools --> HAMCP["Home Assistant MCP"]
    HAMCP --> HA["Home Assistant entities/services"]
    Gemini --> RawOut["assistant raw PCM"]
    RawOut --> P610Out["P610 output gate"]
    P610Out --> Pacat["pacat / PulseAudio"]
    Pacat --> P610Speaker["P610 speaker"]
~~~

## Warm standby

A P610 worker is created before the wake word is spoken. It prepares:

- local audio transport;
- wake/stop models;
- MCP tool schema;
- Gemini Live WebSocket;
- initial system/context state.

While in standby, P610 microphone frames are consumed locally for wake-word detection and a small pre-roll ring. Raw microphone PCM is not forwarded to Gemini before wake.

This reduces wake-to-model startup time without continuously streaming room audio to the provider.

## Wake flow

1. Local Okay Nabu detection activates the gate.
2. The gate retains a short overlap from the pre-roll ring.
3. A cue-decision timer starts.
4. If the user continues speaking within the decision window, the wake cue is skipped.
5. If the user pauses, the fixed wake cue is played as audible feedback.
6. If Gemini is ready, buffered audio is flushed in order.
7. If Gemini is reconnecting, audio remains queued up to the activation-buffer cap.

The design goal is to support a continuous phrase with no artificial pause after the wake word.

## Turn detection

The browser and P610 do not need identical VAD tuning.

For the P610, the microphone is already gated by a local wake word, so the Gemini end-of-speech detector can be more eager. The current P610 path uses high EOS sensitivity and roughly 350 ms of non-speech before turn completion.

Server-side VAD remains enabled because it is useful for realtime interruption behavior and acoustic robustness.

## Output flow

Gemini audio frames are recorded for diagnostics before the physical output gate. The P610 output path writes raw PCM to pacat with a small PulseAudio buffer.

Why pacat instead of mpv for streamed speech:

- raw PCM is the native format of the Gemini audio stream;
- a small PulseAudio latency buffer smooths irregular network chunk timing;
- there is no media-container parsing or no-cache playback behavior;
- the runtime can measure inter-frame gaps before writing audio.

The output gate tracks gap counts above 100, 200, and 500 ms plus the maximum gap for the last response.

## Stop and recycle

Stop is detected locally while the conversation is active. It:

1. emits an interruption frame;
2. stops current playback;
3. plays the end cue;
4. cancels the current P610 worker;
5. immediately starts and warms a fresh worker.

Provider failures use a separate recovery path so an intentional Stop is not reported as an error.

## Gemini idle reconnect patch

Long-idle Gemini Live WebSockets may close with code 1008. The upstream Pipecat 1.4.0 failure counter can remain non-zero when a connection was healthy but completely idle because its reset logic depends on receiving a provider message after a stable period.

The build-time patch changes this behavior so a sufficiently long-lived connection resets the failure history before the next normal idle close is counted. The provider is still allowed to reconnect; the reconnect simply does not escalate 1/3, 2/3, 3/3 into a false fatal failure.

## Debug architecture

The session recorder collects separate views of the audio path:

- physical microphone input;
- post-gate Gemini input;
- raw provider output;
- successfully written physical output;
- synchronized stereo mix;
- event timeline.

That makes it possible to distinguish microphone/VAD problems, provider latency, tool latency, PCM chunk gaps, and physical playback issues.
