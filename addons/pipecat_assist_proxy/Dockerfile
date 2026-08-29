FROM ghcr.io/kyvaith/pipecat-homeassistant/pipecat-assist:0.1.75

ENV CONVERSATION_LOG_DIR=/data/conversation_logs
ENV NLTK_DATA=/usr/local/share/nltk_data

RUN apt-get update     && apt-get install --yes --no-install-recommends build-essential portaudio19-dev libasound2-plugins mpv pulseaudio-utils     && pip3 install --no-cache-dir "pyaudio~=0.2.14" "pymicro-wakeword>=2,<3"     && python3 -m nltk.downloader -d /usr/local/share/nltk_data punkt punkt_tab     && apt-get clean     && rm -rf /var/lib/apt/lists/*

COPY app/main.py /app/main.py
COPY app/audio_debug.py /app/audio_debug.py
COPY app/mcp_bridge.py /app/mcp_bridge.py
COPY app/session_memory.py /app/session_memory.py
COPY app/conversation_log.py /app/conversation_log.py
COPY app/ui/index.html /app/ui/index.html
COPY wakewords/ /app/wakewords/
COPY sounds/ /app/sounds/
COPY root/run.sh /run.sh
COPY root/patch_webrtc_ice.py /patch_webrtc_ice.py
COPY root/patch_gemini_idle_reconnect.py /patch_gemini_idle_reconnect.py
RUN python3 /patch_webrtc_ice.py && python3 /patch_gemini_idle_reconnect.py && rm -f /patch_webrtc_ice.py /patch_gemini_idle_reconnect.py && chmod a+x /run.sh

LABEL \
    io.hass.version="0.1.75-proxy23" \
    io.hass.type="app" \
    org.opencontainers.image.title="Pipecat Assist Proxy" \
    org.opencontainers.image.source="https://github.com/SHAREN/home-assistant-voice-stack"

WORKDIR /
CMD ["/run.sh"]
