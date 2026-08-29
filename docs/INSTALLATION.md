# Installation

## Requirements

- Home Assistant OS or a supervised installation that can run local add-ons.
- Network access from the add-on to the Gemini API or to an optional HTTP proxy that can reach Gemini from a supported region.
- A Google AI Studio API key for Gemini Live.
- HTTPS for browser microphone access unless the browser is using localhost.
- Optional Plantronics P610 attached to the Home Assistant host audio subsystem.

## Add the add-on repository

Open Home Assistant:

1. Settings > Add-ons > Add-on Store.
2. Open Repositories.
3. Add this repository URL:

    https://github.com/SHAREN/home-assistant-voice-stack

4. Refresh the store.
5. Install Pipecat Assist Proxy.
6. Optional: install P610 Microphone Monitor.

The main add-on defaults to browser-only mode so it can start on hosts without a P610. Enable physical P610 audio only after the device is visible to the host.

## Install the Home Assistant integration

### HACS

Add this GitHub repository to HACS as a custom Integration repository, then install Pipecat Assist and restart Home Assistant.

### Manual

Copy:

    custom_components/pipecat_assist

to:

    /config/custom_components/pipecat_assist

Restart Home Assistant, then open Settings > Devices & services > Add integration and choose Pipecat Assist.

## First provider configuration

1. Start Pipecat Assist Proxy.
2. Open its Ingress UI.
3. Open Integrations in the Pipecat UI.
4. Add Google Gemini Live.
5. Paste a Google AI Studio API key.
6. Select a Gemini Live audio-capable model. The currently tested model is:

    models/gemini-3.1-flash-live-preview

7. Keep the default realtime flow or create a flow named home-default.
8. Verify the Home Assistant MCP integration.

Inside a normal Home Assistant add-on, MCP authentication uses the Supervisor token and the local endpoint:

    http://supervisor/core/api/mcp

No long-lived HA token should be required for the normal path.

## Optional Gemini proxy

If Gemini Live is not directly available from the Home Assistant host region, configure the add-on option gemini_proxy_url with an HTTP proxy that exits in a supported region.

Example shape only:

    http://proxy.example.net:3128

Do not publish proxy credentials or private proxy addresses. If authentication is required, keep it in Home Assistant add-on options or another secret store, not in Git.

## Browser voice test

Open the add-on assistant UI or the Lovelace Pipecat card over HTTPS. Start a voice test and verify:

- microphone permission succeeds;
- WebRTC connects;
- Gemini reports connected;
- a spoken request returns audio;
- Home Assistant MCP tools can read or control an exposed entity.

If the browser is opened over plain HTTP on a non-localhost hostname, modern browsers will block microphone access.

## Install the Lovelace card

The Pipecat Assist integration serves its card asset automatically. A minimal card is:

~~~yaml
type: custom:pipecat-assist-card
name: Pipecat Assist
~~~

Optional secure-host redirect:

~~~yaml
type: custom:pipecat-assist-card
name: Pipecat Assist
secure_host: ha.example.com
~~~

Use secure_host only when the same dashboard has a known HTTPS hostname.

## Enable Plantronics P610 local audio

1. Attach the P610 to the Home Assistant host.
2. Confirm that the host exposes a capture source and playback sink.
3. Optional: start P610 Microphone Monitor to confirm the microphone level.
4. Configure Pipecat Assist Proxy:

~~~yaml
p610_local_audio: true
p610_flow_id: home-default
p610_wake_threshold: 0.6
p610_stop_threshold: 0.5
p610_refractory_seconds: 2.0
p610_stop_guard_seconds: 0.5
~~~

5. Restart the add-on.
6. Open the status endpoint or UI and verify:

    p610_local_audio.running = true
    p610_local_audio.standby_state = ready
    p610_local_audio.provider_healthy = true

7. Say one continuous phrase without waiting for the activation sound:

    Okay Nabu, what is the weather?

For Russian speech, a typical test is:

    Окей Набу, какая погода?

If speech begins soon after the wake phrase, the wake cue should be skipped and the command should be forwarded immediately.

## Verify audio debug

Enable audio debug in the Pipecat UI. After one P610 conversation, the audio debug list should contain the session package and report a 24-hour retention value.

See DEBUGGING.md for the file layout and latency analysis.
