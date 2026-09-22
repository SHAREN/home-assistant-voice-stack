# Pipecat Assist Proxy

Home Assistant add-on for realtime Pipecat voice pipelines with Gemini Live, Home Assistant MCP, browser WebRTC, and optional Plantronics P610 local full-duplex audio.

The public default keeps P610 disabled so the add-on can start on systems without the physical device.

For complete installation and configuration, see the repository README and docs/INSTALLATION.md.

Key optional P610 features:

- local Okay Nabu wake word;
- local Stop command;
- warm Gemini standby;
- pre-roll / activation buffering;
- pacat/PulseAudio streaming output;
- 24-hour session audio debugging.

For the current deployed P610 behavior, control-guard rules, success tones, and recovery gap, see [`docs/P610_PRODUCTION_STATE_2026-09-23.md`](../../docs/P610_PRODUCTION_STATE_2026-09-23.md).
