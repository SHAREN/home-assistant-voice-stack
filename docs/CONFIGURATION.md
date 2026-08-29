# Configuration

## Home Assistant add-on options

The public add-on intentionally ships without personal network values or proxy endpoints.

| Option | Default | Purpose |
| --- | --- | --- |
| runner_port | 7861 | Pipecat HTTP/WebRTC service port |
| log_level | INFO | Runtime log verbosity |
| gemini_proxy_url | empty | Optional HTTP proxy used for Gemini/provider traffic |
| p610_local_audio | false | Enable the physical Plantronics P610 transport |
| p610_flow_id | home-default | Flow used by the P610 worker |
| p610_wake_threshold | 0.6 | Okay Nabu wake confidence threshold |
| p610_stop_threshold | 0.5 | Local Stop confidence threshold |
| p610_refractory_seconds | 2.0 | Minimum time between wake detections |
| p610_stop_guard_seconds | 0.5 | Ignore Stop detection briefly after the wake cue |

## Gemini Live

Configure the Google Gemini Live provider in the Pipecat Ingress UI rather than putting an API key in source files.

Recommended characteristics:

- speech-to-speech / native audio model;
- Russian or desired spoken language configured in the flow/provider;
- tools enabled for Home Assistant MCP;
- no generated greeting for P610 warm standby unless explicitly desired.

The P610 path quietly primes the Gemini context instead of requesting an initial assistant response.

## Home Assistant MCP

The preferred MCP path is the Home Assistant Supervisor endpoint. The add-on receives the Supervisor token automatically and can connect to:

    http://supervisor/core/api/mcp

Use exposed Home Assistant entities and normal HA permissions to control what the model can access.

The runtime compacts MCP tool descriptions before sending them to Gemini. This reduces prompt size while preserving tool names, parameters, and required constraints.

## P610 wake and command buffering

Important internal defaults:

| Setting | Approximate default | Meaning |
| --- | --- | --- |
| local pre-roll | 2.0 s | Recent mic ring kept locally before wake |
| command overlap | 0.4 s | Trailing audio retained around wake detection |
| cue decision | 0.9 s | Window to detect a continuous command before playing the cue |
| activation buffer cap | 30 s | Maximum queued mic audio while the provider is not ready |
| continuation RMS threshold | 0.012 normalized | Simple post-wake speech/energy continuation check |

The queue preserves the earliest command audio if the provider remains unavailable beyond the cap.

## P610 end-of-speech behavior

The physical P610 path uses a faster Gemini server-VAD configuration than the browser path:

- high end-of-speech sensitivity;
- about 350 ms of detected non-speech before committing the end of the turn.

The browser keeps the flow-level VAD behavior, so P610 latency tuning does not make the browser microphone more trigger-happy.

## Playback

Assistant PCM on the P610 uses pacat / PulseAudio rather than mpv for the streaming response.

Current tuning:

- mono signed 16-bit PCM;
- provider sample rate passed through at runtime;
- around 180 ms PulseAudio latency buffer;
- around 200 ms silent tail before normal EOF to reduce last-phoneme clipping.

Short fixed cue files still use mpv.

## Session memory

P610 Stop recycles into a fresh warm realtime session. The P610 path intentionally avoids reusing browser-style session memory across this recycle, so an old large context does not accumulate indefinitely in an always-on speakerphone session.

Browser session-memory behavior remains controlled by the normal Pipecat Assist configuration.

## Audio debug retention

Audio debugging is temporary diagnostic data. This fork uses a 24-hour retention policy by default and runs periodic cleanup even if no new sessions are created.

Do not increase retention on a shared Home Assistant host without considering privacy and storage use.
