import asyncio
import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Logging MUST be configured before any module-level loggers are obtained
from .logging_config import configure_logging
configure_logging()

from .services.serial_manager import SerialManager
from .services.telemetry import TelemetryManager
from .services.gcode_streamer import GCodeStreamer
from .services.mdns_advertiser import MdnsAdvertiser

logger = logging.getLogger("hardware_comm.api")

# Global services references
serial_mgr = SerialManager()
telemetry_mgr = TelemetryManager(serial_mgr)
streamer_mgr = GCodeStreamer(serial_mgr)

# ── Configuration ─────────────────────────────────────────────────────────────

def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in ("1", "true", "yes", "on")

# If true, any active GCode stream will be cancelled the moment the last
# WebSocket client disconnects. Defaults to false (stream continues).
CANCEL_ON_DISCONNECT = _truthy(os.getenv("COMM_CANCEL_STREAM_ON_DISCONNECT", "false"))

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Initializing Hardware Communication API…")

    # Attempt initial connect — if it fails the read loop will keep retrying
    serial_mgr.connect()

    # Always start the read loop (it handles reconnect internally)
    await serial_mgr.start_reading()

    # If we connected on the first try, fetch dynamic buffer size
    if serial_mgr.is_connected:
        await asyncio.sleep(0.5)   # let the MCU send its greeting
        await streamer_mgr.fetch_buffer_size()

    # Start 10Hz telemetry polling (broadcasts to WS clients)
    loop = asyncio.get_event_loop()
    loop.create_task(telemetry_mgr.start_polling(hz=10))

    # Advertise this service on the LAN via mDNS so the Discovery Sidecar
    # can find it automatically.  Runs in a thread (zeroconf is synchronous).
    # Silently no-ops on Docker Desktop where multicast doesn't cross the NAT.
    mdns = MdnsAdvertiser()
    await asyncio.to_thread(mdns.start)

    yield

    # Shutdown
    logger.info("Shutting down Hardware Communication API…")
    await asyncio.to_thread(mdns.stop)
    telemetry_mgr.stop()
    streamer_mgr.cancel_stream()
    serial_mgr.close()

app = FastAPI(title="NeonBeam Core API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # allow_credentials intentionally omitted: combining it with allow_origins=["*"]
    # is forbidden by the CORS spec — browsers reject the response and the fetch()
    # call throws, appearing as "unreachable" to the caller. Since this API uses
    # no cookies or HTTP auth, credentials mode is not needed.
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/health")
async def health_check():
    return {
        "status": "ok" if serial_mgr.is_connected else "disconnected", 
        "service": "hardware_comm"
    }

@app.get("/api/settings")
async def get_machine_settings():
    return {
        "status": "ok", 
        "data": {
            "max_feed_rate": 5000, 
            "work_area": {"x": 400, "y": 400},
            "buffer_max": streamer_mgr.rx_buffer_max
        }
    }


# ── Firmware (Grbl $$) endpoints ────────────────────────────────────────────────

@app.get("/api/firmware/settings")
async def get_firmware_settings():
    """
    Send the '$$' command to the MCU and capture the response.
    Returns a dict of { "$N": value } pairs parsed from the reply.
    Blocks for ~400ms while the MCU transmits its settings list.
    """
    if not serial_mgr.is_connected:
        return {"status": "error", "message": "Machine not connected", "settings": {}}
    if streamer_mgr.is_streaming:
        return {"status": "error", "message": "Cannot read firmware settings while a job is streaming", "settings": {}}

    await serial_mgr.write_line("$$")
    lines = await serial_mgr.drain_response_lines(timeout_ms=400)

    settings: dict[str, float] = {}
    for line in lines:
        # grblHAL format: $100=80.000 (steps/mm)
        if line.startswith("$") and "=" in line:
            try:
                raw_key, raw_val = line.split("=", 1)
                # Strip inline comment if any (e.g. "80.000 (steps/mm)")
                val_str = raw_val.split("(")[0].strip()
                settings[raw_key.strip()] = float(val_str)
            except (ValueError, IndexError):
                pass

    logger.info(f"Firmware poll returned {len(settings)} settings.")
    return {"status": "ok", "settings": settings}


class FirmwareSettingsRequest(BaseModel):
    settings: dict[str, float]


@app.post("/api/firmware/settings")
async def set_firmware_settings(req: FirmwareSettingsRequest):
    """
    Write one or more firmware settings to the MCU.
    Accepts { "settings": { "$100": 80, "$110": 5000 } }.
    Each key-value pair is sent as a separate write_line call.
    """
    if not serial_mgr.is_connected:
        return {"status": "error", "message": "Machine not connected"}
    if streamer_mgr.is_streaming:
        return {"status": "error", "message": "Cannot write firmware settings while a job is streaming"}

    written = []
    for key, value in req.settings.items():
        cmd = f"{key}={value:g}"
        await serial_mgr.write_line(cmd)
        written.append(cmd)

    logger.info(f"Firmware flash: wrote {len(written)} setting(s).")
    return {"status": "ok", "written": written}

class CommandRequest(BaseModel):
    command: str

# States where normal commands are safe
_COMMAND_SAFE_STATES = {"Idle", "Hold"}

@app.post("/api/gcode/command")
async def send_command(req: CommandRequest):
    """
    Send a single realtime or configuration command.
    Blocked while a GCode job is streaming to prevent buffer corruption.
    Use /api/jog for jog moves during a job.
    """
    if not serial_mgr.is_connected:
        return {"status": "error", "message": "Machine not connected"}
    if streamer_mgr.is_streaming:
        return {"status": "error", "message": "Cannot send freeform command while job is streaming. Use /api/jog for jog moves."}
    await serial_mgr.write_line(req.command)
    return {"status": "sent", "command": req.command}


# ── Live-Jog state (in-memory; resets on service restart) ────────────────────
_live_jog_enabled: bool = False

class JogRequest(BaseModel):
    axis: str    # "X", "Y", or "Z"
    step: float  # mm (signed — positive or negative)
    feed: float  # mm/min

class JogSettingsRequest(BaseModel):
    live_jog_enabled: bool

@app.get("/api/jog/settings")
async def get_jog_settings():
    """Returns the current live-jog toggle state."""
    return {"live_jog_enabled": _live_jog_enabled}

@app.post("/api/jog/settings")
async def set_jog_settings(req: JogSettingsRequest):
    """Enable or disable jogging while a GCode job is streaming."""
    global _live_jog_enabled
    _live_jog_enabled = req.live_jog_enabled
    logger.info(f"Live jog {'ENABLED' if _live_jog_enabled else 'DISABLED'}")
    return {"live_jog_enabled": _live_jog_enabled}

@app.post("/api/jog")
async def jog_machine(req: JogRequest):
    """
    Send one incremental jog move using grblHAL's dedicated $J= jog mode.

    Guard logic:
      - Machine must be connected.
      - Hard block in Alarm or Offline states (regardless of live_jog).
      - If a GCode job is actively streaming AND live_jog is disabled → reject.
      - grblHAL jog commands ($J=) use a separate jog buffer and can be
        cancelled with 0x85 without affecting the main GCode buffer.
    """
    if not serial_mgr.is_connected:
        return {"status": "error", "message": "Machine not connected"}

    state = telemetry_mgr.last_state  # cached from last telemetry poll

    # Hard blocks — cannot jog in these states
    if state in ("Alarm", "Offline"):
        return {"status": "error", "message": f"Cannot jog: machine is in {state} state"}

    # Block during streaming unless live jog is explicitly enabled
    if streamer_mgr.is_streaming and not _live_jog_enabled:
        return {
            "status": "error",
            "message": "Jogging is disabled while a job is streaming. Enable Live Jog first.",
            "live_jog_enabled": False,
        }

    axis = req.axis.upper()
    if axis not in ("X", "Y", "Z"):
        return {"status": "error", "message": f"Invalid axis '{req.axis}'. Must be X, Y, or Z."}

    feed = max(1, req.feed)
    # $J=G21G91 = jog mode, metric, relative positioning
    cmd = f"$J=G21G91{axis}{req.step:.4f}F{feed:.0f}"
    await serial_mgr.write_line(cmd)

    return {
        "status": "jogging",
        "command": cmd,
        "machine_state": state,
        "live_jog": _live_jog_enabled,
    }


@app.post("/api/gcode/upload")
async def upload_gcode(file: UploadFile):
    """
    Accepts a GCode file and queues it for streaming.
    Does NOT start streaming — the operator must call POST /api/gcode/start
    (via Cycle Start in Machine Control) to begin.
    """
    if not serial_mgr.is_connected:
        return {"status": "error", "message": "Machine not connected"}

    if streamer_mgr.is_streaming:
        return {"status": "error", "message": "A job is already streaming. Cancel it first."}

    try:
        content = await file.read()
        gcode_str = content.decode("utf-8", errors="replace")
        lines = gcode_str.splitlines()

        # Strip blank lines and full-line comments, preserve inline code
        valid_lines = [
            line.split(";")[0].strip()
            for line in lines
            if line.strip() and not line.strip().startswith(";")
        ]
        valid_lines = [l for l in valid_lines if l]  # drop now-empty lines

        # Load into queue — operator presses Cycle Start to begin
        streamer_mgr.load_job(valid_lines, job_name=file.filename or "job.nc")

        logger.info(f"Queued '{file.filename}' with {len(valid_lines)} lines. Awaiting Cycle Start.")
        return {
            "status": "queued",
            "job_name":    file.filename,
            "total_lines": len(valid_lines),
            "message":     "Job queued. Press Cycle Start to begin.",
        }
    except Exception as e:
        logger.error(f"Upload processing failed: {str(e)}")
        return {"status": "error", "message": str(e)}


@app.post("/api/gcode/start")
async def start_gcode_stream():
    """
    Begin streaming the currently queued GCode job.
    Called when the operator presses Cycle Start.
    """
    if not serial_mgr.is_connected:
        return {"status": "error", "message": "Machine not connected"}
    if not (streamer_mgr.is_queued or streamer_mgr.file_queue):
        return {"status": "error", "message": "No job queued. Upload a GCode file first."}
    if streamer_mgr.is_streaming:
        return {"status": "error", "message": "Job is already streaming."}
    try:
        await streamer_mgr.start_stream()
        return {"status": "streaming_started", "job_name": streamer_mgr.job_name}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/gcode/status")
async def stream_status():
    """Lightweight polling endpoint — safe to call from a mobile browser while the job runs."""
    return {
        "is_streaming":           streamer_mgr.is_streaming,
        "is_queued":              streamer_mgr.is_queued,
        "job_name":               streamer_mgr.job_name,
        "total_lines":            streamer_mgr.total_lines,
        "lines_sent":             streamer_mgr.lines_sent,
        "lines_pending":          len(streamer_mgr.file_queue),
        "active_chars":           streamer_mgr.active_chars,
        # Programmed feed rate parsed from the GCode header comment once at load time.
        # More reliable than the live FS telemetry value (which is instantaneous
        # and fluctuates wildly during raster boustrophedon scanning).
        "feed_rate_mm_min":       streamer_mgr.programmed_feed_mm_min,
    }


@app.post("/api/gcode/cancel")
async def cancel_stream():
    """Gracefully halts the streaming loop and sends a soft-reset to halt machine motion."""
    streamer_mgr.cancel_stream()
    if serial_mgr.is_connected:
        await serial_mgr.write_realtime("\x18")   # grblHAL soft-reset
    return {"status": "cancelled"}


@app.post("/api/gcode/estop")
async def emergency_stop():
    """
    Emergency stop — immediately sends soft-reset (Ctrl+X) to the MCU,
    cancels any streaming job, and clears the queue.
    Safe to call at any time regardless of machine state.
    """
    streamer_mgr.cancel_stream()
    if serial_mgr.is_connected:
        await serial_mgr.write_realtime("\x18")   # grblHAL soft-reset (Ctrl+X)
    logger.warning("EMERGENCY STOP triggered via API.")
    return {"status": "estop_sent"}

@app.websocket("/ws/telemetry")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    telemetry_mgr.listeners.append(websocket)
    try:
        while True:
            # Keep connection alive while telemetry_mgr pushes data automatically
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in telemetry_mgr.listeners:
            telemetry_mgr.listeners.remove(websocket)
            
        # Optional: cancel stream if no listeners remain and the feature is enabled
        if CANCEL_ON_DISCONNECT and not telemetry_mgr.listeners:
            if streamer_mgr.is_streaming:
                logger.warning("Last client disconnected and COMM_CANCEL_STREAM_ON_DISCONNECT is enabled. Halting job.")
                streamer_mgr.cancel_stream()
                if serial_mgr.is_connected:
                    # Async task because we're in a sync-like except block (but this is an async def)
                    asyncio.create_task(serial_mgr.write_realtime("\x18"))
