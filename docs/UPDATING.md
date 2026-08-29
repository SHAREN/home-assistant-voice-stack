# Updating and rollback

## Why updates need a preflight

The main add-on inherits an upstream pipecat-homeassistant image and then applies local runtime files plus compatibility patches. A new upstream version can change Pipecat internals, Gemini service behavior, UI assets, or patch target lines.

Do not blindly bump the base image and deploy directly to a working voice endpoint.

## Update preflight

Before changing the base image:

1. Read upstream pipecat-homeassistant release notes and commits.
2. Check the bundled Pipecat version.
3. Check Gemini Live service changes.
4. Verify patch_gemini_idle_reconnect.py still matches the intended upstream code.
5. Verify patch_webrtc_ice.py still matches the current transport implementation.
6. Run Python syntax checks.
7. Run JavaScript syntax checks.
8. Run the P610 regression tests.
9. Build the add-on image.
10. Keep a copy of the currently installed add-on directory for rollback.

## Minimum regression checks

At minimum test:

- browser WebRTC connection;
- Gemini Live connection and first response;
- Home Assistant MCP tool call;
- P610 standby reaches ready;
- continuous wake phrase and command;
- short command turn completion;
- assistant playback has no large unexpected PCM gaps;
- Stop interrupts and recycles to a fresh ready standby;
- debug recording creates all expected tracks;
- debug retention remains 24 hours;
- repeated idle Gemini reconnects do not accumulate into a false 3/3 fatal failure.

## Suggested local checks

From the repository root:

    python3 -m py_compile addons/pipecat_assist_proxy/app/main.py
    python3 -m py_compile addons/pipecat_assist_proxy/app/audio_debug.py

Run the dedicated tests:

    python3 -m pytest tests/test_p610_pipecat_local_audio.py
    python3 -m pytest tests/test_p610_session_audio_debug.py

Where Node.js is available:

    node --check lovelace/pipecat-assist-card-v5.js

## Rollback

Before replacing a working local add-on, save the current installed directory or create a normal Home Assistant backup.

A simple source-directory rollback strategy is:

1. stop the add-on;
2. restore the previously known-good add-on directory;
3. reload the Home Assistant add-on store metadata;
4. rebuild/update the local add-on;
5. start it;
6. verify status, provider connection, audio device, logs, and a real voice turn.

Do not delete debug evidence before diagnosing a failed update unless storage pressure requires it.

## Versioning

The local add-on version uses a suffix such as proxy23. Increment the suffix when the local application layer changes even if the inherited upstream image version does not.

A public release should document both:

- upstream pipecat-homeassistant base image version;
- local proxy/application suffix.
