# Pipecat Assist Proxy

Home Assistant add-on for realtime Pipecat voice pipelines with Gemini Live, Home Assistant MCP, browser WebRTC, and optional Plantronics P610 local full-duplex audio.

The public default keeps P610 disabled so the add-on can start on systems without the physical device.

For complete installation and configuration, see the repository README and docs/INSTALLATION.md.

Key optional P610 features:

- local Okay Nabu wake word;
- local Stop command;
- warm Gemini standby with local readiness recovery;
- pre-roll / activation buffering;
- model-driven semantic conversation end after a short farewell;
- optional nonverbal thinking cue for slow multi-tool/search work;
- pacat/PulseAudio streaming output;
- 24-hour session audio debugging.

## Production-state note

The exact deployed P610 production baseline has advanced beyond this public runtime snapshot. As of 2026-09-23 the local deployment is `0.1.75-proxy39`; see [`docs/P610_PRODUCTION_STATE_2026-09-23.md`](../../docs/P610_PRODUCTION_STATE_2026-09-23.md). Do not change this add-on's public version number to proxy39 until the exact deployed source files have been copied and verified byte-for-byte.
