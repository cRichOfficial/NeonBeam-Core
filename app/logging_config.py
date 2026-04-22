"""
NeonBeam Core — Logging Configuration
======================================
Reads configuration from environment variables and sets up the Python
logging system with the requested handlers and level.

Environment variables (all optional, with sane defaults):
─────────────────────────────────────────────────────────
COMM_LOG_LEVEL          Log level for all hardware_comm loggers.
                        Values: DEBUG | INFO | WARNING | ERROR | CRITICAL
                        Default: INFO

COMM_LOG_OUTPUT         Where to send log output.
                        Values: stdout | file | both
                        Default: stdout

COMM_LOG_FILE           Absolute path for the log file.
                        Default: /app/logs/neonbeam_core.log
                        (Only used when COMM_LOG_OUTPUT is 'file' or 'both')

COMM_LOG_MAX_BYTES      Maximum log file size before rotation (bytes).
                        Default: 10485760  (10 MB)

COMM_LOG_BACKUP_COUNT   Number of rotated log files to keep.
                        Default: 5

COMM_LOG_VERBOSE_SERIAL Enable raw TX/RX logging for every byte sent to
                        and received from the grblHAL MCU.
                        Values: true | false   (case-insensitive)
                        Default: false
                        NOTE: Only active when COMM_LOG_LEVEL=DEBUG.
                        At any higher level this setting has no effect.
"""

import logging
import logging.handlers
import os
import sys
from pathlib import Path
from .services.log_names import SERIAL_TX_LOGGER, SERIAL_RX_LOGGER


# ── Named logger for this module ──────────────────────────────────────────────
_log = logging.getLogger("hardware_comm.logging_config")

# ── Public constants ───────────────────────────────────────────────────────────


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def configure_logging() -> None:
    """
    Call once at application startup (before any other code runs).
    Sets up handlers on the root 'hardware_comm' logger so that every
    child logger (serial, streamer, telemetry, api …) inherits the config.
    """
    # ── Read env vars ──────────────────────────────────────────────────────────
    raw_level    = os.getenv("COMM_LOG_LEVEL",          "INFO").upper()
    output_mode  = os.getenv("COMM_LOG_OUTPUT",         "stdout").lower().strip()
    log_file     = os.getenv("COMM_LOG_FILE",           "/app/logs/neonbeam_core.log")
    max_bytes    = int(os.getenv("COMM_LOG_MAX_BYTES",  str(10 * 1024 * 1024)))   # 10 MB
    backup_count = int(os.getenv("COMM_LOG_BACKUP_COUNT", "5"))
    verbose_serial = _truthy(os.getenv("COMM_LOG_VERBOSE_SERIAL", "false"))

    # Map string → logging level constant (fall back to INFO if unrecognised)
    numeric_level = getattr(logging, raw_level, None)
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO
        raw_level = "INFO"

    # ── Formatter ─────────────────────────────────────────────────────────────
    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-35s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── Build handler list ─────────────────────────────────────────────────────
    handlers: list[logging.Handler] = []

    use_stdout = output_mode in ("stdout", "both")
    use_file   = output_mode in ("file",   "both")

    if use_stdout:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        handlers.append(sh)

    if use_file:
        log_path = Path(log_file)
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                filename=str(log_path),
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            fh.setFormatter(fmt)
            handlers.append(fh)
        except OSError as exc:
            # If we cannot open the file, fall back to stdout and warn
            fallback = logging.StreamHandler(sys.stdout)
            fallback.setFormatter(fmt)
            handlers.append(fallback)
            # Log the warning after the root logger is configured (below)
            _deferred_file_warn = f"Cannot open log file '{log_file}': {exc}. Falling back to stdout."
        else:
            _deferred_file_warn = None
    else:
        _deferred_file_warn = None

    if not handlers:
        # Safety net — always have at least one handler
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(fmt)
        handlers.append(h)

    # ── Configure root 'hardware_comm' logger ──────────────────────────────────
    root = logging.getLogger("hardware_comm")
    root.setLevel(numeric_level)
    root.handlers.clear()           # remove any handlers added before configure_logging()
    root.propagate = False          # don't bubble up to the Python root logger

    for h in handlers:
        h.setLevel(numeric_level)
        root.addHandler(h)

    # ── Serial TX/RX verbose loggers ──────────────────────────────────────────
    # These exist as child loggers of 'hardware_comm' and inherit its handlers.
    # We control them by clamping their effective level:
    #   - verbose_serial=true  AND level=DEBUG  → DEBUG (all TX/RX messages visible)
    #   - any other combination                 → above CRITICAL (effectively silent)
    #
    # The messages themselves are emitted at DEBUG level in serial_manager.py,
    # so they are naturally hidden when the root logger is set to INFO or above.
    serial_effective = logging.DEBUG if (verbose_serial and numeric_level == logging.DEBUG) else logging.CRITICAL + 1

    for name in (SERIAL_TX_LOGGER, SERIAL_RX_LOGGER):
        sl = logging.getLogger(name)
        sl.setLevel(serial_effective)
        # Inherit handlers from parent — no need to add them here

    # ── Emit startup summary ───────────────────────────────────────────────────
    _log.info("─" * 60)
    _log.info("NeonBeam Core logging initialised")
    _log.info(f"  Level          : {raw_level}")
    _log.info(f"  Output         : {output_mode}")
    if use_file:
        _log.info(f"  Log file       : {log_file}  (max {max_bytes // 1024} KB × {backup_count} backups)")
    _log.info(f"  Verbose serial : {'ENABLED (TX + RX)' if verbose_serial and numeric_level == logging.DEBUG else 'disabled'}")
    if _deferred_file_warn:
        _log.warning(_deferred_file_warn)
    _log.info("─" * 60)
