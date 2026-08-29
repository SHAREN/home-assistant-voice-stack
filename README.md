# Home Assistant Voice Stack

A full-duplex Home Assistant voice stack built on top of Pipecat and the upstream pipecat-homeassistant project. The primary runtime is a Home Assistant add-on that connects browser WebRTC and an optional physical Plantronics P610 speakerphone to Gemini Live while keeping Home Assistant device control available through MCP.

This repository is not a fork of the Pipecat core library. It is a derivative application layer that extends pipecat-homeassistant with a physical P610 audio path, local wake/stop words, warm Gemini standby, audio diagnostics, latency instrumentation, and several stability fixes.

Russian setup guide: [docs/README.ru.md](docs/README.ru.md)

## Highlights

- Gemini Live speech-to-speech with real full-duplex audio and barge-in.
- Browser voice UI over WebRTC.
- Optional Plantronics P610 local microphone + speaker transport.
- Local wake word: Okay Nabu.
- Local Stop command that ends the conversation and immediately warms a fresh standby session.
- Warm Gemini WebSocket: the provider is connected before wake, while microphone audio remains locally gated.
- Pre-roll and activation buffering so a continuous phrase such as "Okay Nabu, weather" is not lost while the provider is becoming ready.
- P610-specific fast end-of-speech tuning for short commands.
- PulseAudio-native playback through pacat with a small jitter buffer.
- Home Assistant MCP tools using the Supervisor-backed MCP endpoint.
- Session audio debugging with separate microphone, Gemini input, raw assistant, played assistant, stereo mix, and timeline files.
- 24-hour debug-audio retention by default.
- Workaround for Gemini Live idle WebSocket closes that would otherwise accumulate as false fatal failures in Pipecat 1.4.0.
- Optional microphone monitor for checking the P610 capture level and routing.

## Supported entry points

| Client | Transport | Full duplex | Wake word | Recommended use |
| --- | --- | --- | --- | --- |
| Browser / Lovelace | WebRTC | Yes | Browser-controlled | Development and dashboard use |
| Plantronics P610 | Local audio + PulseAudio | Yes | Okay Nabu | Always-on room speakerphone |
| Home Assistant Assist bridge | HTTP bridge | Limited by HA Assist | HA-owned | Compatibility with standard Assist |

## Repository layout

- addons/pipecat_assist_proxy - main Home Assistant add-on and current recommended runtime.
- addons/p610_mic_monitor - optional P610 microphone monitor.
- custom_components/pipecat_assist - Home Assistant integration for Conversation/STT/TTS and the Lovelace bridge.
- lovelace - standalone development versions of the Pipecat cards/dashboards.
- ha_mcp - local overlay/instructions for Home Assistant MCP behavior.
- tests - regression and audio-path tests.
- tools - P610/STT calibration and diagnostic tools.
- experimental/gemini_live - experimental direct Gemini integration; not the recommended runtime.

## Architecture

~~~mermaid
flowchart LR
    Mic["P610 microphone"] --> Wake["local microWakeWord gate"]
    Wake --> Buffer["pre-roll + activation buffer"]
    Buffer --> Pipecat["Pipecat Assist Proxy"]
    Browser["Browser / Lovelace WebRTC"] --> Pipecat
    Pipecat <-->|"Gemini Live WebSocket"| Gemini["Gemini Live"]
    Pipecat <-->|"MCP / Supervisor"| HA["Home Assistant"]
    Gemini --> Out["pacat / PulseAudio jitter buffer"]
    Out --> Speaker["P610 speaker"]
    Pipecat --> Debug["24 h session audio + timeline"]
~~~

When the P610 is idle, the wake-word model listens locally. The Gemini Live connection may remain warm, but raw microphone PCM is not forwarded to Gemini until wake is detected.

## Installation

### 1. Add the Home Assistant add-on repository

In Home Assistant, open Settings > Add-ons > Add-on Store > Repositories and add:

    https://github.com/SHAREN/home-assistant-voice-stack

Install Pipecat Assist Proxy. Install P610 Microphone Monitor only if you need physical-device diagnostics.

### 2. Install the Home Assistant integration

Use HACS as a custom Integration repository pointing at this repository, or copy:

    custom_components/pipecat_assist

into:

    /config/custom_components/pipecat_assist

Then restart Home Assistant and add Pipecat Assist from Settings > Devices & services.

### 3. Configure Gemini Live

Open the Pipecat Assist Proxy add-on UI and configure Google Gemini Live with a Google AI Studio API key. The current stack is tested with:

    models/gemini-3.1-flash-live-preview

The exact available model name can change over time; use a Gemini Live audio-capable model supported by your account and region.

If direct Gemini API access is not available from your region, set the optional gemini_proxy_url add-on option to your own HTTP proxy in a supported region. The repository intentionally ships with this value empty.

### 4. Verify Home Assistant MCP

The normal Home Assistant add-on deployment uses the Supervisor token automatically and connects to:

    http://supervisor/core/api/mcp

Do not commit or paste long-lived Home Assistant tokens into the repository.

### 5. Optional: enable the Plantronics P610

Make the P610 available to the Home Assistant host audio subsystem, then enable:

    p610_local_audio: true

The default flow is:

    p610_flow_id: home-default

The P610 must be the intended default capture/playback device, or the host audio routing must resolve default to the P610.

After restart, status should report the P610 worker as running and standby_state as ready.

### 6. Browser / Lovelace

Browser microphone access requires HTTPS or localhost. Add the Pipecat Assist card to a dashboard. If you intentionally open the dashboard over plain HTTP and have a separate HTTPS hostname, set the optional card secure_host value.

See [docs/INSTALLATION.md](docs/INSTALLATION.md) for the complete setup sequence.

## P610 conversation lifecycle

1. The local wake-word model listens while the provider connection is warm.
2. Okay Nabu activates the conversation.
3. A short pre-roll tail is retained so a command that starts immediately after the wake phrase is not clipped.
4. The system waits briefly for a continuous command. If speech continues, the wake cue is skipped so it cannot mask or contaminate the command.
5. Microphone audio is forwarded to Gemini Live.
6. Gemini can call Home Assistant MCP tools and stream audio back.
7. Assistant PCM is played with pacat/PulseAudio and a small jitter buffer.
8. A local Stop detection interrupts output, ends the conversation, and immediately creates a fresh warm standby session.

Current P610 defaults in this release include approximately:

- 2.0 s local pre-roll ring.
- 0.4 s command overlap around wake.
- 0.9 s cue-decision window.
- 350 ms Gemini end-of-speech silence for the P610 path.
- 180 ms PulseAudio output latency buffer.
- 200 ms silent output tail to avoid clipping the last phoneme.

More detail: [docs/P610.md](docs/P610.md).

## Audio/session debugging

When audio debugging is enabled, a P610 session can produce:

- mic_raw.wav - what the physical microphone captured.
- gemini_input.wav - the PCM actually forwarded to Gemini.
- assistant_raw.wav - assistant PCM returned by Gemini.
- assistant_played.wav - PCM successfully written to the playback path.
- mix_stereo.wav - synchronized microphone/assistant stereo diagnostic mix.
- timeline.json - wake, cue, first assistant audio, PCM gaps, interruption, Stop, and session-close timing.

Recordings are automatically removed after 24 hours by default. Empty warm-standby sessions are not retained as recordings.

See [docs/DEBUGGING.md](docs/DEBUGGING.md).

## Important Gemini Live behavior

Gemini Live may periodically close a long-idle WebSocket with code 1008. In the tested setup this can happen on an approximately 2.5-minute cadence. Reconnecting is normally harmless.

Pipecat 1.4.0 can incorrectly accumulate these idle reconnects as consecutive failures if no provider message arrives during the stable interval. This repository applies patch_gemini_idle_reconnect.py at image build time so a connection that lived long enough is treated as stable even if it was idle.

The patch does not prevent Gemini from rotating idle connections; it prevents normal reconnects from becoming a false fatal P610 failure.

## Security and privacy

- No API keys, Home Assistant tokens, proxy credentials, personal hostnames, or recorded conversations should be committed.
- .gitignore excludes common secret files and runtime recordings.
- Audio debugging is intended for temporary troubleshooting and defaults to a 24-hour retention window.
- Prefer Home Assistant Supervisor-backed MCP authentication inside an add-on instead of manually stored long-lived tokens.
- Browser microphone access should be served over HTTPS.

See [docs/SECURITY.md](docs/SECURITY.md).

## Updating

This project currently derives its base image from:

    ghcr.io/kyvaith/pipecat-homeassistant/pipecat-assist:0.1.75

The application layer then copies its own runtime files and applies targeted compatibility patches. Before changing the upstream base version, review Pipecat and pipecat-homeassistant changes, verify the patch targets, run the regression suite, and keep a rollback copy of the installed add-on.

See [docs/UPDATING.md](docs/UPDATING.md).

## Documentation

- [Installation](docs/INSTALLATION.md)
- [Configuration](docs/CONFIGURATION.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Plantronics P610](docs/P610.md)
- [Debugging and audio capture](docs/DEBUGGING.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Updating and rollback](docs/UPDATING.md)
- [Security](docs/SECURITY.md)
- [Russian guide](docs/README.ru.md)

## Upstream and licenses

This project is derived from kyvaith/pipecat-homeassistant and uses Pipecat by Daily. The upstream pipecat-homeassistant project is MIT licensed. Pipecat and other dependencies retain their own licenses.

See LICENSE and NOTICE.
