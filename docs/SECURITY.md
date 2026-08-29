# Security and privacy

## Never commit secrets

Do not commit:

- Google/Gemini API keys;
- GitHub tokens;
- Home Assistant long-lived access tokens;
- Supervisor tokens;
- proxy credentials;
- private TLS keys;
- SSH keys;
- private .env files;
- Home Assistant .storage data;
- conversation logs or recorded room audio.

The repository .gitignore covers common secret and runtime-data patterns, but it is not a substitute for reviewing changes before pushing.

## Home Assistant MCP

For a normal Home Assistant add-on deployment, use the Supervisor-backed MCP path. This avoids storing a long-lived Home Assistant token in application source or public configuration.

Limit which Home Assistant entities are exposed to Assist/MCP when practical. A voice model with broad tool access can act on exposed devices if a request is interpreted as an action.

## Audio privacy

P610 wake detection is local. Before wake, raw microphone PCM is not intentionally forwarded to Gemini by the P610 gate.

Audio debug changes the privacy profile because it records local diagnostic WAV files. These files can contain:

- the user's command;
- assistant playback captured as echo;
- room noise;
- accidental speech around wake/Stop events.

The default retention is 24 hours and cleanup runs periodically. Disable audio debug when it is not needed.

## Browser security

Use HTTPS for browser microphone access. Do not weaken browser security settings to make getUserMedia work over an insecure remote HTTP origin.

## Proxy security

The optional Gemini proxy is a privileged network path because it carries provider requests. Use a proxy you control or trust. Keep credentials outside Git. Do not ship a personal proxy endpoint as a public default.

## Public issue reports

Before attaching logs or WAV files to a GitHub issue:

1. inspect them for private speech;
2. remove tokens and authorization headers;
3. remove private hostnames/IPs if they are not necessary;
4. prefer timeline.json and redacted logs when audio is not required.

## Dependency updates

The runtime inherits code and packages from the upstream pipecat-homeassistant image. Track upstream security updates and rebuild when required, but preserve the update preflight because local patch compatibility is also security-relevant.
