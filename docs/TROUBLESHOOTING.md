# Troubleshooting

## Wake works but response takes 8-20 seconds

Check timeline.json before changing random timeouts.

Possible causes:

- the activation cue masked a short command;
- Gemini VAD kept the turn open because the mic contained later speech-like bursts;
- the command was transcribed incorrectly and Gemini made unrelated tool calls;
- Gemini was reconnecting exactly at wake;
- the provider/proxy added network latency.

The current P610 path uses a longer cue-decision window and fast end-of-speech tuning specifically to reduce short-command latency.

## Repeated "Pipecat recovered" notifications around 30 seconds

Older builds could escalate normal Gemini idle closes into a fatal worker failure:

    failure 1/3 -> failure 2/3 -> failure 3/3 -> fatal

The idle-reconnect patch resets the failure history after a connection has been stable long enough, even if no provider message arrived during idle. With the fix, normal idle rotation should look like repeated independent 1/3 reconnects instead of a fatal sequence.

If this still happens, capture the exact first failing health field and provider log before assuming Home Assistant itself restarted.

## Gemini Live 1008 while idle

An isolated 1008 "operation was aborted" during a long idle session can be a normal provider-side connection rotation. The important questions are:

- did reconnect complete quickly?
- did provider_healthy return to true?
- did the failure counter accumulate across healthy idle connections?

Do not restart the whole Home Assistant host for an isolated reconnect.

## Assistant cuts off the last word

Check:

- assistant_raw duration;
- assistant_played duration;
- output process return code;
- whether the normal 200 ms silent tail was written before EOF.

If raw audio itself ends early, the issue is provider-side. If raw is complete but played audio is shorter, investigate local playback.

## Assistant audio stutters

Inspect PCM gap metrics:

- gap_100ms;
- gap_200ms;
- gap_500ms;
- max_gap_ms.

If gaps already exist before pacat, the network/provider is the likely cause. If raw delivery is smooth but playback fails, inspect PulseAudio and device routing.

## Assistant seems to interrupt itself

Check the session mix and interruption event time. Determine whether the microphone captured:

- the user speaking;
- assistant echo;
- a wake/end cue;
- unrelated noise.

A server-side interruption before physical assistant playback begins cannot be caused by echo from that response, so event ordering matters.

## Random foreign-language transcript

Short wake-like or noisy audio can occasionally be decoded as unrelated Latin, Korean, Portuguese, or other text. The Russian P610 path includes a prompt hint and recovery guards so short non-Cyrillic garbage does not automatically trigger aggressive retries or unrelated Home Assistant actions.

The better fix is still to keep wake/cue audio out of Gemini input and improve the physical input signal.

## Browser microphone does not work

Use HTTPS or localhost. Browser getUserMedia is blocked on ordinary insecure HTTP origins.

If using a custom Lovelace card and a separate public HTTPS hostname, configure secure_host.

## P610 worker is not ready

Check in order:

1. add-on is started;
2. p610_local_audio is enabled;
3. physical P610 capture device exists;
4. default capture route points to the intended source;
5. Gemini provider is configured and reachable;
6. standby_state and last_error;
7. provider_healthy;
8. audio debug / microphone monitor.

## Home Assistant tool calls fail

Prefer the built-in Supervisor-backed MCP path. Confirm the Home Assistant MCP server is reachable and the requested entity is exposed.

If a tool call fails validation, inspect the generated arguments. A failed tool followed by a fallback tool can add noticeable latency even when the actual Home Assistant API call is fast.
