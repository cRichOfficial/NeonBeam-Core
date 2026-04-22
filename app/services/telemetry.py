import asyncio
import logging
from .serial_manager import SerialManager

logger = logging.getLogger("hardware_comm.telemetry")

class TelemetryManager:
    def __init__(self, serial_manager: SerialManager):
        self.serial = serial_manager
        self.is_running = False
        self._ping_task = None
        self.listeners  = []
        self.last_state = "Offline"   # updated by parse_status_report on every '?' response

        # Cached WCO — grblHAL sends WCO periodically; when absent the previous
        # value is reused to derive WPos = MPos - WCO each frame.
        self._wco = {"x": 0.0, "y": 0.0, "z": 0.0}

        self.serial.register_callback(self.handle_line)

    async def handle_line(self, line: str):
        if line.startswith("<") and line.endswith(">"):
            data = self.parse_status_report(line)

            # Broadcast to all WebSocket listeners
            disconnected = []
            for ws in self.listeners:
                try:
                    await ws.send_json(data)
                except Exception:
                    disconnected.append(ws)

            # Clean up dead sockets
            for ws in disconnected:
                self.listeners.remove(ws)

    def _parse_coords(self, raw: str) -> dict:
        """Parse a 'X,Y,Z' string into a {x, y, z} dict of floats."""
        parts = raw.split(",")
        return {
            "x": float(parts[0]) if len(parts) > 0 else 0.0,
            "y": float(parts[1]) if len(parts) > 1 else 0.0,
            "z": float(parts[2]) if len(parts) > 2 else 0.0,
        }

    def parse_status_report(self, line: str) -> dict:
        """
        Parse a grblHAL status report string into a structured dict.

        Example inputs:
          <Idle|MPos:0.000,0.000,0.000|Bf:15,128|FS:0,0|WCO:0.000,0.000,0.000>
          <Run|MPos:180.623,180.624,0.000|Bf:99,127|FS:42,0|Ov:100,100,100|A:C>
          <Alarm|MPos:0.000,135.364,0.000|Bf:100,1023|FS:0,0>

        grblHAL omits WPos and periodically sends WCO instead.
        WPos = MPos - WCO  (computed here, cached across frames).
        """
        line = line.strip("<>")
        parts = line.split("|")

        # ── State ─────────────────────────────────────────────────────────────
        state = parts[0].split(":")[0]   # handles 'Hold:0', 'Door:0', etc.
        self.last_state = state

        result: dict = {"state": state}

        # ── Parse key:value fields ─────────────────────────────────────────────
        raw: dict = {}
        for part in parts[1:]:
            if ":" in part:
                key, _, val = part.partition(":")
                raw[key] = val

        # ── Machine position ───────────────────────────────────────────────────
        mpos = self._parse_coords(raw["MPos"]) if "MPos" in raw else None
        if mpos:
            result["mpos"] = mpos

        # ── Work Coordinate Offset (cached) ────────────────────────────────────
        if "WCO" in raw:
            self._wco = self._parse_coords(raw["WCO"])
        result["wco"] = self._wco

        # ── Work position: prefer explicit WPos, else derive from MPos - WCO ──
        if "WPos" in raw:
            result["wpos"] = self._parse_coords(raw["WPos"])
        elif mpos:
            result["wpos"] = {
                "x": round(mpos["x"] - self._wco["x"], 3),
                "y": round(mpos["y"] - self._wco["y"], 3),
                "z": round(mpos["z"] - self._wco["z"], 3),
            }

        # ── Feed & Spindle ─────────────────────────────────────────────────────
        if "FS" in raw:
            fs = raw["FS"].split(",")
            result["feedRate"]     = float(fs[0]) if len(fs) > 0 else 0.0
            result["spindleSpeed"] = float(fs[1]) if len(fs) > 1 else 0.0
        elif "F" in raw:
            result["feedRate"]     = float(raw["F"])
            result["spindleSpeed"] = 0.0

        # ── Buffer state ───────────────────────────────────────────────────────
        if "Bf" in raw:
            bf = raw["Bf"].split(",")
            result["bufferBlocks"] = int(bf[0]) if len(bf) > 0 else 0
            result["bufferBytes"]  = int(bf[1]) if len(bf) > 1 else 0

        # ── Overrides ─────────────────────────────────────────────────────────
        if "Ov" in raw:
            ov = raw["Ov"].split(",")
            result["overrides"] = {
                "feed":    int(ov[0]) if len(ov) > 0 else 100,
                "rapid":   int(ov[1]) if len(ov) > 1 else 100,
                "spindle": int(ov[2]) if len(ov) > 2 else 100,
            }

        # ── Accessories (A:SFC combinations) ──────────────────────────────────
        if "A" in raw:
            a = raw["A"]
            result["accessories"] = {
                "spindleCCW": "C" in a,   # coolant (mapped as laser on for grblHAL laser)
                "floodCoolant": "F" in a,
                "mistCoolant":  "M" in a,
            }

        # ── Line number (Ln) ──────────────────────────────────────────────────
        if "Ln" in raw:
            try:
                result["lineNumber"] = int(raw["Ln"])
            except ValueError:
                pass

        return result

    async def start_polling(self, hz: int = 10):
        self.is_running = True
        interval = 1.0 / hz
        logger.info(f"Starting Telemetry polling at {hz} Hz")
        _was_connected = False

        while self.is_running:
            if self.serial.is_connected:
                _was_connected = True
                # Issue real-time status request '?'
                await self.serial.write_realtime("?")
            else:
                if _was_connected:
                    # Serial just dropped — push Offline to all clients immediately
                    self.last_state = "Offline"
                    payload = {"state": "Offline"}
                    dead = []
                    for ws in self.listeners:
                        try:
                            await ws.send_json(payload)
                        except Exception:
                            dead.append(ws)
                    for ws in dead:
                        self.listeners.remove(ws)
                    _was_connected = False
            await asyncio.sleep(interval)

    def stop(self):
        self.is_running = False
        logger.info("Telemetry polling stopped")
