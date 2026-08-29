"""Patch Pipecat's development runner to use public STUN servers for SmallWebRTC."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path


STUN_URLS = [
    "stun:stun.cloudflare.com:3478",
    "stun:stun.l.google.com:19302",
]

spec = importlib.util.find_spec("pipecat.runner.run")
if spec is None or not spec.origin:
    raise RuntimeError("Could not locate pipecat.runner.run")

path = Path(spec.origin)
text = path.read_text(encoding="utf-8")
original = text

# Pipecat imports SmallWebRTCConnection inside _setup_webrtc_routes.
text, import_count = re.subn(
    r"from pipecat\.transports\.smallwebrtc\.connection import SmallWebRTCConnection",
    "from pipecat.transports.smallwebrtc.connection import IceServer, SmallWebRTCConnection",
    text,
    count=1,
)
if import_count == 0 and "import IceServer, SmallWebRTCConnection" not in text:
    raise RuntimeError("Unsupported Pipecat runner: SmallWebRTCConnection import not found")

replacement = (
    "small_webrtc_handler: SmallWebRTCRequestHandler = SmallWebRTCRequestHandler(\n"
    "        esp32_mode=args.esp32,\n"
    "        host=args.host,\n"
    f"        ice_servers=[IceServer(urls={STUN_URLS!r})],\n"
    "    )"
)

pattern = re.compile(
    r"small_webrtc_handler:\s*SmallWebRTCRequestHandler\s*=\s*SmallWebRTCRequestHandler\(\s*"
    r"esp32_mode=args\.esp32,\s*host=args\.host\s*\)",
    flags=re.MULTILINE,
)
text, handler_count = pattern.subn(replacement, text, count=1)

if handler_count == 0:
    # Idempotent rebuild: accept an already patched file.
    if "stun:stun.cloudflare.com:3478" not in text or "ice_servers=[IceServer" not in text:
        raise RuntimeError("Unsupported Pipecat runner: SmallWebRTCRequestHandler creation not found")

if text != original:
    path.write_text(text, encoding="utf-8")

# Fail the image build if the patch is not present in the resulting module.
verified = path.read_text(encoding="utf-8")
for required in (
    "IceServer, SmallWebRTCConnection",
    "ice_servers=[IceServer",
    "stun:stun.cloudflare.com:3478",
    "stun:stun.l.google.com:19302",
):
    if required not in verified:
        raise RuntimeError(f"WebRTC ICE patch verification failed: {required}")

print(f"Patched Pipecat SmallWebRTC ICE servers in {path}")
