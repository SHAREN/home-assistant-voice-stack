from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import signal
import struct
import wave
from contextlib import suppress
from io import BytesIO
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

OPTIONS_PATH = Path("/data/options.json")
INDEX_PATH = Path("/app/index.html")
HOST = "0.0.0.0"
PORT = 8099
SUPERVISOR_URL = os.getenv("SUPERVISOR", "http://supervisor").rstrip("/")
SUPERVISOR_TOKEN = os.getenv("SUPERVISOR_TOKEN", "").strip()
ASSIST_SATELLITE_SLUG = "local_assist_satellite_session_end"
MONITOR_SLUG = "local_p610_mic_monitor"
SUPERVISOR_TIMEOUT = ClientTimeout(total=20)

DEFAULTS: dict[str, Any] = {
    "input_device": "default",
    "sample_rate": 48_000,
    "channels": 1,
    "chunk_ms": 100,
}


def load_options() -> dict[str, Any]:
    options = dict(DEFAULTS)
    try:
        stored = json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        stored = {}
    if isinstance(stored, dict):
        options.update(stored)

    options["input_device"] = str(options.get("input_device") or "default")
    options["sample_rate"] = int(options.get("sample_rate") or 48_000)
    options["channels"] = int(options.get("channels") or 1)
    options["chunk_ms"] = int(options.get("chunk_ms") or 100)

    if options["sample_rate"] not in {16_000, 24_000, 32_000, 44_100, 48_000}:
        options["sample_rate"] = 48_000
    if options["channels"] not in {1, 2}:
        options["channels"] = 1
    if options["chunk_ms"] not in {20, 40, 60, 80, 100, 120, 160, 200}:
        options["chunk_ms"] = 100
    return options


OPTIONS = load_options()
FFMPEG = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
ACTIVE_PROCESSES: set[asyncio.subprocess.Process] = set()
ASSIST_RESTART_TASK: asyncio.Task[None] | None = None


class SupervisorError(RuntimeError):
    """Raised when the Home Assistant Supervisor API rejects a request."""


async def supervisor_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    if not SUPERVISOR_TOKEN:
        raise SupervisorError(
            "Supervisor token is unavailable. Enable hassio_api for this App."
        )

    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
    if payload is not None:
        headers["Content-Type"] = "application/json"

    async with ClientSession(timeout=SUPERVISOR_TIMEOUT, headers=headers) as session:
        async with session.request(
            method,
            f"{SUPERVISOR_URL}{path}",
            json=payload,
        ) as response:
            text = await response.text()
            try:
                body = json.loads(text) if text else {}
            except json.JSONDecodeError:
                body = {"message": text}

            if response.status >= 400 or body.get("result") == "error":
                message = body.get("message") or body.get("error") or text
                raise SupervisorError(
                    f"Supervisor {method} {path} failed: HTTP {response.status}: {message}"
                )
            return body.get("data", body)


def select_microphone_input(audio_data: dict[str, Any]) -> dict[str, Any]:
    audio = audio_data.get("audio") if isinstance(audio_data, dict) else None
    inputs = audio.get("input", []) if isinstance(audio, dict) else []
    if not isinstance(inputs, list) or not inputs:
        raise SupervisorError("Home Assistant Audio returned no microphone inputs")

    plantronics = [
        item
        for item in inputs
        if "plantronics" in str(item.get("description", "")).lower()
        or "plantronics" in str(item.get("name", "")).lower()
    ]
    candidates = plantronics or inputs
    return next((item for item in candidates if item.get("default")), candidates[0])


def validate_sensitivity_payload(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        system_volume = int(round(float(payload.get("system_volume_percent"))))
        mic_volume = int(round(float(payload.get("mic_volume"))))
        noise_suppression = int(payload.get("mic_noise_suppression"))
        auto_gain = int(payload.get("mic_auto_gain"))
    except (TypeError, ValueError) as err:
        raise web.HTTPBadRequest(text=f"Invalid microphone setting: {err}") from err

    if not 0 <= system_volume <= 100:
        raise web.HTTPBadRequest(text="System microphone volume must be 0-100%")
    if not 1 <= mic_volume <= 100:
        raise web.HTTPBadRequest(text="Assist mic volume must be 1-100%")
    if not 0 <= noise_suppression <= 4:
        raise web.HTTPBadRequest(text="Noise suppression must be 0-4")
    if not 0 <= auto_gain <= 31:
        raise web.HTTPBadRequest(text="Automatic gain must be 0-31")

    return {
        "system_volume_percent": system_volume,
        "mic_volume": mic_volume,
        "mic_noise_suppression": noise_suppression,
        "mic_auto_gain": auto_gain,
        "restart_assist_satellite": bool(payload.get("restart_assist_satellite", True)),
    }


async def read_sensitivity_settings() -> dict[str, Any]:
    audio_data, addon_data = await asyncio.gather(
        supervisor_request("GET", "/audio/info"),
        supervisor_request("GET", f"/addons/{ASSIST_SATELLITE_SLUG}/info"),
    )
    microphone = select_microphone_input(audio_data)
    options = addon_data.get("options", {}) if isinstance(addon_data, dict) else {}
    return {
        "ok": True,
        "microphone": {
            "index": int(microphone.get("index", 0)),
            "name": str(microphone.get("name", "")),
            "description": str(microphone.get("description", "Microphone")),
            "default": bool(microphone.get("default")),
            "muted": bool(microphone.get("mute")),
        },
        "system_volume_percent": round(float(microphone.get("volume", 0.0)) * 100, 1),
        "mic_volume": int(round(float(options.get("mic_volume", 100)))),
        "mic_noise_suppression": int(options.get("mic_noise_suppression", 0)),
        "mic_auto_gain": int(options.get("mic_auto_gain", 0)),
        "assist_satellite_state": str(addon_data.get("state", "unknown")),
        "limits": {
            "system_volume_percent": [0, 100],
            "mic_volume": [1, 100],
            "mic_noise_suppression": [0, 4],
            "mic_auto_gain": [0, 31],
        },
    }


async def settings_get_handler(_: web.Request) -> web.Response:
    try:
        settings = await read_sensitivity_settings()
    except SupervisorError as err:
        raise web.HTTPServiceUnavailable(text=str(err)) from err
    return web.json_response(settings, headers={"Cache-Control": "no-store"})


async def restart_assist_satellite_background() -> None:
    global ASSIST_RESTART_TASK
    try:
        await supervisor_request("POST", f"/addons/{ASSIST_SATELLITE_SLUG}/restart")
        print("Assist Satellite restart completed", flush=True)
    except Exception as err:
        print(f"Assist Satellite restart failed: {err}", flush=True)
    finally:
        ASSIST_RESTART_TASK = None


def schedule_assist_satellite_restart() -> bool:
    global ASSIST_RESTART_TASK
    if ASSIST_RESTART_TASK is not None and not ASSIST_RESTART_TASK.done():
        return False
    ASSIST_RESTART_TASK = asyncio.create_task(restart_assist_satellite_background())
    return True


async def settings_post_handler(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, TypeError) as err:
        raise web.HTTPBadRequest(text="Expected a JSON settings object") from err
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text="Expected a JSON settings object")

    settings = validate_sensitivity_payload(payload)
    try:
        audio_data = await supervisor_request("GET", "/audio/info")
        microphone = select_microphone_input(audio_data)
        input_index = int(microphone.get("index", 0))
        await supervisor_request(
            "POST",
            "/audio/volume/input",
            {
                "index": input_index,
                "volume": settings["system_volume_percent"] / 100.0,
            },
        )

        addon_data = await supervisor_request(
            "GET", f"/addons/{ASSIST_SATELLITE_SLUG}/info"
        )
        options = dict(addon_data.get("options", {}))
        options.update(
            {
                "mic_volume": settings["mic_volume"],
                "mic_noise_suppression": settings["mic_noise_suppression"],
                "mic_auto_gain": settings["mic_auto_gain"],
            }
        )
        await supervisor_request(
            "POST",
            f"/addons/{ASSIST_SATELLITE_SLUG}/options",
            {"options": options},
        )

        restart_scheduled = False
        if settings["restart_assist_satellite"]:
            restart_scheduled = schedule_assist_satellite_restart()
    except SupervisorError as err:
        raise web.HTTPBadGateway(text=str(err)) from err

    return web.json_response(
        {
            "ok": True,
            "applied": settings,
            "microphone_index": input_index,
            "assist_satellite_restart_scheduled": restart_scheduled,
            "message": (
                "Settings applied; Assist Satellite restart scheduled"
                if restart_scheduled
                else "Settings applied; an Assist Satellite restart is already running"
                if settings["restart_assist_satellite"]
                else "Settings applied"
            ),
        },
        headers={"Cache-Control": "no-store"},
    )


async def ingress_panel_handler(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, TypeError) as err:
        raise web.HTTPBadRequest(text="Expected a JSON object") from err
    if not isinstance(payload, dict) or "enabled" not in payload:
        raise web.HTTPBadRequest(text="Expected {\"enabled\": true|false}")

    enabled = bool(payload["enabled"])
    try:
        await supervisor_request(
            "POST",
            f"/addons/{MONITOR_SLUG}/options",
            {"ingress_panel": enabled},
        )
        addon_data = await supervisor_request("GET", f"/addons/{MONITOR_SLUG}/info")
    except SupervisorError as err:
        raise web.HTTPBadGateway(text=str(err)) from err

    actual = bool(addon_data.get("ingress_panel"))
    return web.json_response(
        {
            "ok": actual == enabled,
            "requested": enabled,
            "ingress_panel": actual,
            "message": "Native Home Assistant Ingress panel updated",
        },
        headers={"Cache-Control": "no-store"},
    )


def configure_pulse_server() -> None:
    if os.getenv("PULSE_SERVER"):
        return
    candidates = (
        "/run/audio/pulse.sock",
        "/run/audio/pulse/native",
        "/run/audio/native",
    )
    for candidate in candidates:
        if Path(candidate).exists():
            os.environ["PULSE_SERVER"] = f"unix:{candidate}"
            return


async def resolve_capture_input_device() -> str:
    configured = str(OPTIONS.get("input_device") or "default").strip()
    if configured and configured.lower() != "default":
        return configured

    audio_data = await supervisor_request("GET", "/audio/info")
    microphone = select_microphone_input(audio_data)
    source_name = str(microphone.get("name") or "").strip()
    if not source_name:
        raise SupervisorError("Selected microphone has no PulseAudio source name")
    return source_name


def ffmpeg_capture_command(
    input_device: str,
    *,
    duration: float | None = None,
) -> list[str]:
    command = [
        FFMPEG,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-f",
        "pulse",
        "-i",
        input_device,
    ]
    if duration is not None:
        command.extend(["-t", f"{duration:.3f}"])
    command.extend(
        [
            "-ac",
            str(OPTIONS["channels"]),
            "-ar",
            str(OPTIONS["sample_rate"]),
            "-f",
            "s16le",
            "pipe:1",
        ]
    )
    return command


def ffmpeg_mp3_command(
    input_device: str,
    *,
    monitor_gain: float,
    duration: float | None = None,
) -> list[str]:
    command = [
        FFMPEG,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-f",
        "pulse",
        "-i",
        input_device,
    ]
    if duration is not None:
        command.extend(["-t", f"{duration:.3f}"])
    command.extend(
        [
            "-ac",
            "1",
            "-ar",
            "48000",
            "-af",
            f"volume={monitor_gain:.3f}",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "64k",
            "-write_xing",
            "0",
            "-id3v2_version",
            "0",
            "-flush_packets",
            "1",
            "-f",
            "mp3",
            "pipe:1",
        ]
    )
    return command


async def stop_process(process: asyncio.subprocess.Process | None) -> None:
    if process is None:
        return
    ACTIVE_PROCESSES.discard(process)
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except TimeoutError:
        process.kill()
        with suppress(Exception):
            await process.wait()


async def start_capture(*, duration: float | None = None) -> asyncio.subprocess.Process:
    configure_pulse_server()
    try:
        input_device = await resolve_capture_input_device()
    except SupervisorError as err:
        raise web.HTTPServiceUnavailable(text=str(err)) from err

    process = await asyncio.create_subprocess_exec(
        *ffmpeg_capture_command(input_device, duration=duration),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
    )
    ACTIVE_PROCESSES.add(process)
    return process


async def start_mp3_capture(
    *,
    monitor_gain: float,
    duration: float | None = None,
) -> asyncio.subprocess.Process:
    configure_pulse_server()
    try:
        input_device = await resolve_capture_input_device()
    except SupervisorError as err:
        raise web.HTTPServiceUnavailable(text=str(err)) from err

    process = await asyncio.create_subprocess_exec(
        *ffmpeg_mp3_command(
            input_device,
            monitor_gain=monitor_gain,
            duration=duration,
        ),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
    )
    ACTIVE_PROCESSES.add(process)
    return process


def pcm_metrics(pcm: bytes) -> dict[str, Any]:
    sample_count = len(pcm) // 2
    if sample_count <= 0:
        return {
            "samples": 0,
            "rms": 0.0,
            "peak": 0,
            "rms_dbfs": -120.0,
            "peak_dbfs": -120.0,
            "clipped_samples": 0,
            "clipped_percent": 0.0,
        }

    samples = struct.unpack(f"<{sample_count}h", pcm[: sample_count * 2])
    sum_squares = 0
    peak = 0
    clipped = 0
    for sample in samples:
        absolute = abs(sample)
        sum_squares += sample * sample
        if absolute > peak:
            peak = absolute
        if absolute >= 32_700:
            clipped += 1

    rms = math.sqrt(sum_squares / sample_count)
    rms_dbfs = 20.0 * math.log10(max(rms, 1.0) / 32_768.0)
    peak_dbfs = 20.0 * math.log10(max(float(peak), 1.0) / 32_768.0)
    return {
        "samples": sample_count,
        "rms": round(rms, 2),
        "peak": peak,
        "rms_dbfs": round(rms_dbfs, 2),
        "peak_dbfs": round(peak_dbfs, 2),
        "clipped_samples": clipped,
        "clipped_percent": round((clipped / sample_count) * 100.0, 4),
    }


def wav_bytes(pcm: bytes) -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(OPTIONS["channels"])
        writer.setsampwidth(2)
        writer.setframerate(OPTIONS["sample_rate"])
        writer.writeframes(pcm)
    return output.getvalue()


async def index_handler(_: web.Request) -> web.Response:
    return web.Response(
        text=INDEX_PATH.read_text(encoding="utf-8"),
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


async def health_handler(_: web.Request) -> web.Response:
    try:
        effective_input_device = await resolve_capture_input_device()
        input_error = ""
    except (SupervisorError, web.HTTPException) as err:
        effective_input_device = ""
        input_error = str(err)

    return web.json_response(
        {
            "ok": Path(FFMPEG).exists() and bool(effective_input_device),
            "ffmpeg": FFMPEG,
            "pulse_server": os.getenv("PULSE_SERVER", "auto"),
            "configured_input_device": OPTIONS["input_device"],
            "effective_input_device": effective_input_device,
            "input_error": input_error,
            "sample_rate": OPTIONS["sample_rate"],
            "channels": OPTIONS["channels"],
            "chunk_ms": OPTIONS["chunk_ms"],
            "active_streams": sum(1 for item in ACTIVE_PROCESSES if item.returncode is None),
            "stores_audio": False,
        },
        headers={"Cache-Control": "no-store"},
    )


async def probe_handler(request: web.Request) -> web.Response:
    try:
        seconds = float(request.query.get("seconds", "1.0"))
    except ValueError:
        seconds = 1.0
    seconds = min(5.0, max(0.25, seconds))

    process = await start_capture(duration=seconds)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=seconds + 5.0)
    except TimeoutError:
        await stop_process(process)
        raise web.HTTPGatewayTimeout(text="Microphone probe timed out")
    finally:
        ACTIVE_PROCESSES.discard(process)

    if process.returncode != 0 or not stdout:
        detail = stderr.decode("utf-8", errors="replace")[-2000:].strip()
        raise web.HTTPServiceUnavailable(
            text=detail or "ffmpeg did not return microphone audio"
        )

    metrics = pcm_metrics(stdout)
    metrics.update(
        {
            "ok": True,
            "seconds": seconds,
            "bytes": len(stdout),
            "sample_rate": OPTIONS["sample_rate"],
            "channels": OPTIONS["channels"],
        }
    )
    return web.json_response(metrics, headers={"Cache-Control": "no-store"})


async def sample_handler(request: web.Request) -> web.Response:
    try:
        seconds = float(request.query.get("seconds", "5.0"))
    except ValueError:
        seconds = 5.0
    seconds = min(15.0, max(1.0, seconds))

    process = await start_capture(duration=seconds)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=seconds + 5.0)
    except TimeoutError:
        await stop_process(process)
        raise web.HTTPGatewayTimeout(text="Microphone sample timed out")
    finally:
        ACTIVE_PROCESSES.discard(process)

    if process.returncode != 0 or not stdout:
        detail = stderr.decode("utf-8", errors="replace")[-2000:].strip()
        raise web.HTTPServiceUnavailable(text=detail or "No microphone audio")

    return web.Response(
        body=wav_bytes(stdout),
        content_type="audio/wav",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'inline; filename="p610-microphone-sample.wav"',
        },
    )


def parse_monitor_gain(request: web.Request, *, default: float = 8.0) -> float:
    try:
        gain = float(request.query.get("gain", str(default)))
    except ValueError:
        gain = default
    return min(20.0, max(0.25, gain))


async def live_mp3_handler(request: web.Request) -> web.StreamResponse:
    monitor_gain = parse_monitor_gain(request)
    process = await start_mp3_capture(monitor_gain=monitor_gain)
    if process.stdout is None:
        await stop_process(process)
        raise web.HTTPServiceUnavailable(text="ffmpeg MP3 stdout is unavailable")

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "audio/mpeg",
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
    await response.prepare(request)

    try:
        while True:
            chunk = await process.stdout.read(4096)
            if not chunk:
                break
            await response.write(chunk)
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    finally:
        await stop_process(process)
        with suppress(Exception):
            await response.write_eof()
    return response


async def mp3_check_handler(request: web.Request) -> web.Response:
    monitor_gain = parse_monitor_gain(request)
    process = await start_mp3_capture(monitor_gain=monitor_gain, duration=0.75)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=7.0)
    except TimeoutError:
        await stop_process(process)
        raise web.HTTPGatewayTimeout(text="MP3 encoder check timed out")
    finally:
        ACTIVE_PROCESSES.discard(process)

    if process.returncode != 0 or not stdout:
        detail = stderr.decode("utf-8", errors="replace")[-2000:].strip()
        raise web.HTTPServiceUnavailable(text=detail or "MP3 encoder returned no audio")

    return web.json_response(
        {
            "ok": True,
            "bytes": len(stdout),
            "header_hex": stdout[:16].hex(),
            "monitor_gain": monitor_gain,
            "content_type": "audio/mpeg",
        },
        headers={"Cache-Control": "no-store"},
    )


async def stream_stderr(
    process: asyncio.subprocess.Process,
    tail: list[str],
) -> None:
    if process.stderr is None:
        return
    while True:
        line = await process.stderr.readline()
        if not line:
            return
        text = line.decode("utf-8", errors="replace").strip()
        if text:
            tail.append(text)
            del tail[:-20]


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    websocket = web.WebSocketResponse(heartbeat=20, autoping=True, max_msg_size=64 * 1024)
    await websocket.prepare(request)

    process: asyncio.subprocess.Process | None = None
    pump_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    stderr_tail: list[str] = []
    send_lock = asyncio.Lock()

    async def send_json(payload: dict[str, Any]) -> None:
        if websocket.closed:
            return
        async with send_lock:
            await websocket.send_json(payload)

    async def pump_audio(active: asyncio.subprocess.Process) -> None:
        if active.stdout is None:
            await send_json({"type": "error", "message": "ffmpeg stdout is unavailable"})
            return

        frame_bytes = 2 * OPTIONS["channels"]
        chunk_samples = max(1, int(OPTIONS["sample_rate"] * OPTIONS["chunk_ms"] / 1000))
        chunk_bytes = chunk_samples * frame_bytes
        try:
            while not websocket.closed:
                chunk = await active.stdout.read(chunk_bytes)
                if not chunk:
                    break
                complete = len(chunk) - (len(chunk) % frame_bytes)
                if complete <= 0:
                    continue
                async with send_lock:
                    await websocket.send_bytes(chunk[:complete])
        except (ConnectionResetError, RuntimeError, asyncio.CancelledError):
            raise
        finally:
            with suppress(Exception):
                await active.wait()
            if not websocket.closed and active.returncode not in (None, 0):
                await send_json(
                    {
                        "type": "error",
                        "message": "Microphone capture stopped",
                        "detail": "\n".join(stderr_tail[-5:]),
                    }
                )

    async def begin_stream() -> None:
        nonlocal process, pump_task, stderr_task
        if process is not None and process.returncode is None:
            await send_json({"type": "started"})
            return

        stderr_tail.clear()
        process = await start_capture()
        await asyncio.sleep(0.15)
        if process.returncode is not None:
            stdout, stderr = await process.communicate()
            ACTIVE_PROCESSES.discard(process)
            detail = stderr.decode("utf-8", errors="replace")[-2000:].strip()
            await send_json(
                {
                    "type": "error",
                    "message": "Could not open the microphone",
                    "detail": detail,
                }
            )
            process = None
            return

        stderr_task = asyncio.create_task(stream_stderr(process, stderr_tail))
        pump_task = asyncio.create_task(pump_audio(process))
        await send_json(
            {
                "type": "started",
                "sample_rate": OPTIONS["sample_rate"],
                "channels": OPTIONS["channels"],
                "sample_format": "s16le",
                "chunk_ms": OPTIONS["chunk_ms"],
                "stores_audio": False,
            }
        )

    async def end_stream() -> None:
        nonlocal process, pump_task, stderr_task
        if pump_task is not None:
            pump_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await pump_task
            pump_task = None
        if stderr_task is not None:
            stderr_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await stderr_task
            stderr_task = None
        await stop_process(process)
        process = None
        await send_json({"type": "stopped"})

    await send_json(
        {
            "type": "ready",
            "sample_rate": OPTIONS["sample_rate"],
            "channels": OPTIONS["channels"],
            "chunk_ms": OPTIONS["chunk_ms"],
            "stores_audio": False,
        }
    )

    try:
        async for message in websocket:
            if message.type == WSMsgType.TEXT:
                try:
                    payload = json.loads(message.data)
                except json.JSONDecodeError:
                    payload = {"type": message.data}
                command = str(payload.get("type") or "").lower()
                if command == "start":
                    await begin_stream()
                elif command == "stop":
                    await end_stream()
                elif command == "ping":
                    await send_json({"type": "pong"})
            elif message.type in {WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSED}:
                break
    finally:
        if pump_task is not None:
            pump_task.cancel()
        if stderr_task is not None:
            stderr_task.cancel()
        await stop_process(process)

    return websocket


async def on_shutdown(_: web.Application) -> None:
    processes = list(ACTIVE_PROCESSES)
    for process in processes:
        await stop_process(process)


def create_app() -> web.Application:
    configure_pulse_server()
    application = web.Application(client_max_size=256 * 1024)
    application.router.add_get("/", index_handler)
    application.router.add_get("/api/health", health_handler)
    application.router.add_get("/api/settings", settings_get_handler)
    application.router.add_post("/api/settings", settings_post_handler)
    application.router.add_post("/api/ingress-panel", ingress_panel_handler)
    application.router.add_post("/api/probe", probe_handler)
    application.router.add_get("/api/sample.wav", sample_handler)
    application.router.add_get("/api/live.mp3", live_mp3_handler)
    application.router.add_get("/api/mp3-check", mp3_check_handler)
    application.router.add_get("/ws", websocket_handler)
    application.on_shutdown.append(on_shutdown)
    return application


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for signame in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signame, lambda: None)
    web.run_app(create_app(), host=HOST, port=PORT, access_log=None)
