import asyncio
import logging
from .serial_manager import SerialManager

logger = logging.getLogger("hardware_comm.streamer")


class GCodeStreamer:
    def __init__(self, serial_manager: SerialManager):
        self.serial = serial_manager
        self.rx_buffer_max = 1024   # Standard grblHAL fallback

        # Buffer accounting
        self.active_chars: int       = 0
        self.sent_queue:   list[int] = []

        # Job state
        self.file_queue:   list[str] = []
        self.is_streaming: bool      = False
        self.is_queued:    bool      = False   # loaded, waiting for Cycle Start
        self._stream_task            = None

        # Job metadata (exposed via /api/gcode/status)
        self.job_name:    str = ""
        self.total_lines: int = 0
        self.lines_sent:  int = 0
        self.programmed_feed_mm_min: int | None = None  # extracted from GCode header comment

        self.serial.register_callback(self.handle_response)

    # ── Buffer size query ──────────────────────────────────────────────────────

    async def fetch_buffer_size(self):
        if self.serial.is_connected:
            await self.serial.write_line("$I")
            logger.info("Requested Build Info ($I) to query dynamic RX_BUFFER_SIZE")

    # ── Serial response handling ───────────────────────────────────────────────

    async def handle_response(self, line: str):
        if line == "ok":
            await self.process_ok()
        elif line.startswith("error:"):
            logger.error(f"Grbl Error Response: {line}")
            # On error, stop streaming to avoid further buffer corruption
            if self.is_streaming:
                logger.warning("Halting stream due to error response from MCU.")
                self.cancel_stream()
        elif "RX_BUFFER_SIZE" in line:
            try:
                parts = line.split(":")
                size = int(parts[-1].strip("]"))
                self.rx_buffer_max = size
                logger.info(f"Dynamically configured RX_BUFFER_MAX to {self.rx_buffer_max}")
            except Exception as exc:
                logger.warning(f"Found RX_BUFFER_SIZE but parse failed: {exc}")

    async def process_ok(self):
        if self.sent_queue:
            cleared_len = self.sent_queue.pop(0)
            self.active_chars -= cleared_len
            if self.active_chars < 0:
                self.active_chars = 0

    # ── Load (queue) a job ─────────────────────────────────────────────────────

    def load_job(self, gcode_lines: list[str], job_name: str = "job.nc"):
        """
        Load a GCode file into the queue WITHOUT starting streaming.
        The operator must press Cycle Start (POST /api/gcode/start) to begin.
        """
        if self.is_streaming:
            raise RuntimeError("Cannot load a new job while one is streaming. Cancel first.")

        self._reset_state()
        self.file_queue  = list(gcode_lines)
        self.job_name    = job_name
        self.total_lines = len(gcode_lines)
        self.is_queued   = True

        # Extract the programmed feed rate from the NeonBeam GCode header comment:
        # "; Power: 1000S  Feed: 4800 mm/min  Passes: 1"
        # Scan only the first 20 lines — the header is always at the top.
        import re
        for line in gcode_lines[:20]:
            m = re.search(r'Feed:\s*(\d+)\s*mm/min', line)
            if m:
                self.programmed_feed_mm_min = int(m.group(1))
                break

        logger.info(
            f"Job '{job_name}' queued with {len(gcode_lines)} lines. "
            f"Programmed feed: {self.programmed_feed_mm_min} mm/min. Waiting for Cycle Start."
        )

    # ── Start streaming (triggered by Cycle Start) ─────────────────────────────

    async def start_stream(self):
        """
        Begin streaming the currently queued job.
        Raises if nothing is queued or a job is already running.
        """
        if self.is_streaming:
            raise RuntimeError("A job is already streaming.")
        if not self.is_queued or not self.file_queue:
            raise RuntimeError("No job queued. Upload a GCode file first.")
        if not self.serial.is_connected:
            raise RuntimeError("Machine not connected.")

        self.is_queued   = False
        self.is_streaming = True
        self.active_chars = 0
        self.sent_queue   = []
        self.lines_sent   = 0

        loop = asyncio.get_event_loop()
        self._stream_task = loop.create_task(self._stream_loop())
        logger.info(f"Streaming started for '{self.job_name}' ({self.total_lines} lines).")

    # ── Internal streaming loop ────────────────────────────────────────────────

    async def _stream_loop(self):
        safety_margin = max(10, int(self.rx_buffer_max * 0.05))
        logger.info(f"Buffer fill loop started. rx_buffer_max={self.rx_buffer_max}, margin={safety_margin}")

        while self.is_streaming and self.file_queue:
            if not self.serial.is_connected:
                # Serial dropped mid-job — pause and wait for reconnect
                logger.warning("Serial lost during stream. Pausing until reconnected…")
                while not self.serial.is_connected and self.is_streaming:
                    await asyncio.sleep(1.0)
                if not self.is_streaming:
                    break   # cancelled while waiting
                logger.info("Serial reconnected. Resuming stream.")

            next_line = self.file_queue[0].strip()
            if not next_line:
                self.file_queue.pop(0)
                continue

            line_len = len(next_line) + 1  # +1 for the \n written by serial_manager

            if self.active_chars + line_len <= (self.rx_buffer_max - safety_margin):
                await self.serial.write_line(next_line)
                self.active_chars += line_len
                self.sent_queue.append(line_len)
                self.lines_sent  += 1
                self.file_queue.pop(0)
            else:
                # Buffer full — yield and wait for 'ok' to drain it
                await asyncio.sleep(0.01)

        if self.is_streaming:
            self.is_streaming = False
            logger.info(f"GCode stream completed. {self.lines_sent} lines sent.")

    # ── Cancel / reset ─────────────────────────────────────────────────────────

    def cancel_stream(self):
        """Stop any in-progress or queued job immediately."""
        was_active = self.is_streaming or self.is_queued
        self.is_streaming = False
        self.is_queued    = False
        if self._stream_task:
            self._stream_task.cancel()
            self._stream_task = None
        self._reset_state()
        if was_active:
            logger.warning("GCode stream/queue cancelled.")

    def _reset_state(self):
        self.active_chars = 0
        self.file_queue.clear()
        self.sent_queue.clear()
        self.job_name                = ""
        self.total_lines             = 0
        self.lines_sent              = 0
        self.programmed_feed_mm_min  = None
