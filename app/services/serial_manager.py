import serial
import asyncio
import os
import logging
from collections import deque
from .log_names import SERIAL_TX_LOGGER, SERIAL_RX_LOGGER

logger  = logging.getLogger("hardware_comm.serial")
tx_log  = logging.getLogger(SERIAL_TX_LOGGER)
rx_log  = logging.getLogger(SERIAL_RX_LOGGER)

_RECONNECT_DELAYS = [1, 2, 5, 10, 30]   # seconds between retries (caps at last value)


class SerialManager:
    def __init__(self):
        self.port     = os.getenv("COMM_SERIAL_PORT", "socket://host.docker.internal:23")
        self.baudrate = int(os.getenv("COMM_BAUD_RATE", 115200))
        self.serial_conn  = None
        self.is_connected = False
        self._read_task   = None
        self.callbacks    = []
        self._write_lock  = asyncio.Lock()
        self._running     = False   # set False only on graceful shutdown
        self._drain_buf: deque[str] = deque()  # lines captured during drain windows

    # ── Connection ─────────────────────────────────────────────────────────────

    def _try_connect(self) -> bool:
        """Attempt a single synchronous connection. Returns True on success."""
        try:
            if self.serial_conn:
                try:
                    self.serial_conn.close()
                except Exception:
                    pass
            logger.info(f"Connecting to {self.port} at {self.baudrate} baud…")
            self.serial_conn = serial.serial_for_url(
                self.port, baudrate=self.baudrate, timeout=0.1
            )
            self.is_connected = True
            logger.info("Serial connection established.")
            return True
        except Exception as exc:
            logger.error(f"Connection failed: {exc}")
            self.is_connected = False
            return False

    def connect(self):
        """Initial synchronous connect called at startup."""
        self._running = True
        self._try_connect()

    # ── Read loop with auto-reconnect ──────────────────────────────────────────

    async def start_reading(self):
        """Start the persistent read loop. Reconnects automatically on disconnect."""
        self._running = True
        loop = asyncio.get_event_loop()
        self._read_task = loop.create_task(self._read_loop())

    async def _read_loop(self):
        """
        Main read loop. On any read error or disconnect the loop waits with
        exponential backoff and then reconnects — without restarting the service.
        """
        retry_index = 0

        while self._running:
            # ── Ensure we have a connection ────────────────────────────────────
            if not self.is_connected:
                delay = _RECONNECT_DELAYS[min(retry_index, len(_RECONNECT_DELAYS) - 1)]
                logger.warning(f"Serial disconnected. Reconnecting in {delay}s…")
                await asyncio.sleep(delay)
                ok = await asyncio.to_thread(self._try_connect)
                if ok:
                    retry_index = 0
                    # Give the MCU a moment to send its greeting
                    await asyncio.sleep(0.5)
                else:
                    retry_index += 1
                continue

            # ── Normal read tick ───────────────────────────────────────────────
            try:
                line = await asyncio.to_thread(self._read_line_blocking)
                if line:
                    # Capture into the drain buffer if a drain window is open
                    self._drain_buf.append(line)
                    for cb in self.callbacks:
                        await cb(line)
            except Exception as exc:
                logger.error(f"Read error: {exc}")
                self.is_connected = False
                retry_index = 0   # start reconnect sequence immediately
                continue

            await asyncio.sleep(0.001)

    # ── Read helpers ───────────────────────────────────────────────────────────

    def _read_line_blocking(self) -> str | None:
        if self.serial_conn and self.serial_conn.in_waiting > 0:
            raw  = self.serial_conn.readline()
            line = raw.decode("utf-8", errors="ignore").strip()
            if line:
                rx_log.debug("← RX  %s", line)
            return line or None
        return None

    def register_callback(self, callback):
        self.callbacks.append(callback)

    # ── Drain helper ───────────────────────────────────────────────────────────

    async def drain_response_lines(self, timeout_ms: int = 400) -> list[str]:
        """
        Wait up to `timeout_ms` milliseconds for lines arriving from the MCU
        and return everything received in that window.

        The drain buffer is shared with the normal read loop — lines still flow
        to all registered callbacks.  This method simply snapshots the buffer
        accumulated since the call started.
        """
        self._drain_buf.clear()
        await asyncio.sleep(timeout_ms / 1000)
        captured = list(self._drain_buf)
        self._drain_buf.clear()
        return captured

    # ── Write helpers ──────────────────────────────────────────────────────────

    async def write_line(self, line: str):
        if self.is_connected and self.serial_conn:
            async with self._write_lock:
                encoded = (line + "\n").encode("utf-8")
                tx_log.debug("→ TX  %s", line)
                await asyncio.to_thread(self.serial_conn.write, encoded)

    async def write_realtime(self, char: str):
        """Real-time commands ('?', '!', '~', 0x18, 0x85) — no newline appended."""
        if self.is_connected and self.serial_conn:
            async with self._write_lock:
                if char not in ("?",):   # suppress high-frequency '?' poll noise
                    tx_log.debug("→ TX  [RT] %r", char)
                await asyncio.to_thread(self.serial_conn.write, char.encode("utf-8"))

    # ── Shutdown ───────────────────────────────────────────────────────────────

    def close(self):
        """Graceful shutdown — stops the reconnect loop and closes the port."""
        self._running     = False
        self.is_connected = False
        if self._read_task:
            self._read_task.cancel()
        if self.serial_conn:
            try:
                self.serial_conn.close()
            except Exception:
                pass
