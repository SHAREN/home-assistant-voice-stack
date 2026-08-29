FROM ghcr.io/home-assistant/base:latest

ARG BUILD_VERSION=dev
ARG BUILD_ARCH=aarch64

LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}" \
  org.opencontainers.image.title="P610 Microphone Monitor"

RUN apk add --no-cache python3 py3-aiohttp ffmpeg \
    && ffmpeg -hide_banner -devices 2>&1 | grep -q pulse

COPY app.py /app/app.py
COPY index.html /app/index.html
COPY run.sh /run.sh
RUN chmod a+x /run.sh

WORKDIR /app
CMD ["/run.sh"]
