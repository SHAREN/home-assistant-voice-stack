#!/usr/bin/with-contenv bashio
set -e

RUNNER_HOST="0.0.0.0"
RUNNER_PORT="$(bashio::config 'runner_port')"
LOG_LEVEL="$(bashio::config 'log_level')"
GEMINI_PROXY_URL="$(bashio::config 'gemini_proxy_url')"
P610_LOCAL_AUDIO_ENABLED="$(bashio::config 'p610_local_audio')"
P610_LOCAL_FLOW_ID="$(bashio::config 'p610_flow_id')"
P610_WAKE_THRESHOLD="$(bashio::config 'p610_wake_threshold')"
P610_STOP_THRESHOLD="$(bashio::config 'p610_stop_threshold')"
P610_REFRACTORY_SECONDS="$(bashio::config 'p610_refractory_seconds')"
P610_STOP_GUARD_SECONDS="$(bashio::config 'p610_stop_guard_seconds')"
P610_METADATA_STALE="false"
if [[ -z "$P610_LOCAL_AUDIO_ENABLED" || "$P610_LOCAL_AUDIO_ENABLED" == "null" ]]; then
    P610_METADATA_STALE="true"
    P610_LOCAL_AUDIO_ENABLED="true"
fi
if [[ -z "$P610_LOCAL_FLOW_ID" || "$P610_LOCAL_FLOW_ID" == "null" ]]; then
    P610_LOCAL_FLOW_ID="home-default"
fi
if [[ -z "$P610_WAKE_THRESHOLD" || "$P610_WAKE_THRESHOLD" == "null" ]]; then
    P610_WAKE_THRESHOLD="0.6"
fi
if [[ -z "$P610_STOP_THRESHOLD" || "$P610_STOP_THRESHOLD" == "null" ]]; then
    P610_STOP_THRESHOLD="0.5"
fi
if [[ -z "$P610_REFRACTORY_SECONDS" || "$P610_REFRACTORY_SECONDS" == "null" ]]; then
    P610_REFRACTORY_SECONDS="2.0"
fi
if [[ -z "$P610_STOP_GUARD_SECONDS" || "$P610_STOP_GUARD_SECONDS" == "null" ]]; then
    P610_STOP_GUARD_SECONDS="0.5"
fi

export RUNNER_HOST
export RUNNER_PORT
export LOG_LEVEL
export GEMINI_PROXY_URL
export P610_LOCAL_AUDIO_ENABLED
export P610_LOCAL_FLOW_ID
export P610_WAKE_THRESHOLD
export P610_STOP_THRESHOLD
export P610_REFRACTORY_SECONDS
export P610_STOP_GUARD_SECONDS

if [[ "$P610_METADATA_STALE" == "true" && -n "${SUPERVISOR_TOKEN:-}" ]]; then
    curl --fail --silent --show-error --max-time 15 \
        --request POST \
        --header "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
        "${SUPERVISOR:-http://supervisor}/store/reload" >/dev/null || true
fi

if [[ -n "$GEMINI_PROXY_URL" ]]; then
    export HTTP_PROXY="$GEMINI_PROXY_URL"
    export HTTPS_PROXY="$GEMINI_PROXY_URL"
    export ALL_PROXY="$GEMINI_PROXY_URL"
    export http_proxy="$GEMINI_PROXY_URL"
    export https_proxy="$GEMINI_PROXY_URL"
    export all_proxy="$GEMINI_PROXY_URL"
    export NO_PROXY="127.0.0.1,localhost,supervisor,homeassistant,172.30.32.1,172.30.32.2"
    export no_proxy="$NO_PROXY"
fi

ARGS=(--host "$RUNNER_HOST" --port "$RUNNER_PORT" -t webrtc)

exec python3 -m app.main "${ARGS[@]}"
