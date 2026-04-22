# NeonBeam Core
### Serial / Telnet Bridge — grblHAL Machine Communication Layer

NeonBeam Core is the Python FastAPI service that bridges the local network to the physical grblHAL laser controller over a serial or Telnet connection. It exposes a REST + WebSocket API consumed by **NeonBeam OS**.

> **Part of the NeonBeam Suite.** See the [root README](../README.md) for the full system architecture.

---

## Stack

| Layer | Technology |
|---|---|
| API Framework | Python 3.11 + FastAPI |
| Async Runtime | asyncio + uvicorn |
| Serial I/O | pyserial (serial/Telnet) |
| Streaming | Buffer-fill GCode streaming engine |
| Logging | Python `logging` + rotating file handler |

---

## Environment Variables

All configuration is supplied via environment variables (`.env` file or Docker `environment:` block).

### Connection

| Variable | Default | Description |
|---|---|---|
| `COMM_PORT` | `8000` | Port the FastAPI service listens on |
| `COMM_SERIAL_PORT` | `socket://host.docker.internal:23` | Serial port or Telnet socket URL — **see serial port mapping below** |
| `COMM_BAUD_RATE` | `115200` | Serial baud rate (ignored for Telnet sockets) |

### Logging

| Variable | Default | Values | Description |
|---|---|---|---|
| `COMM_LOG_LEVEL` | `INFO` | `DEBUG` `INFO` `WARNING` `ERROR` `CRITICAL` | Controls verbosity for all `hardware_comm.*` loggers |
| `COMM_LOG_OUTPUT` | `stdout` | `stdout` `file` `both` | Where log records are written |
| `COMM_LOG_FILE` | `/app/logs/neonbeam_core.log` | Any absolute path | Log file location inside the container; mount a volume to persist on the host |
| `COMM_LOG_MAX_BYTES` | `10485760` (10 MB) | Integer bytes | Rotate the log file when it reaches this size |
| `COMM_LOG_BACKUP_COUNT` | `5` | Integer | Number of rotated backup files to keep |
| `COMM_LOG_VERBOSE_SERIAL` | `false` | `true` `false` | Log every raw byte sent to / received from the MCU. **Only active when `COMM_LOG_LEVEL=DEBUG`.** Produces very high volume during GCode streaming — use for short debugging sessions only. |

#### Log Level Guide

| Level | When to use |
|---|---|
| `DEBUG` | Deep protocol debugging; logs every serial frame. Combine with `COMM_LOG_VERBOSE_SERIAL=true` for full TX/RX visibility. |
| `INFO` | Normal operation; logs job lifecycle events, connection status, and firmware settings changes. |
| `WARNING` | Quiet production mode; logs only recoverable issues (serial drops, buffer retries). |
| `ERROR` | Minimal; logs only unhandled failures that halt a subsystem. |
| `CRITICAL` | Silent except for catastrophic failures (process-level crashes). |

#### Logger Hierarchy

NeonBeam Core uses a structured logger namespace so you can filter precisely in your log viewer:

```
hardware_comm              # root — overall app events
hardware_comm.serial       # serial manager: connect/disconnect/reconnect
hardware_comm.serial.tx    # raw bytes sent → MCU (DEBUG + verbose serial only)
hardware_comm.serial.rx    # raw bytes received ← MCU (DEBUG + verbose serial only)
hardware_comm.streamer     # GCode buffer-fill streaming engine
hardware_comm.settings     # firmware ($Nxx) read / write operations
```

---

## Serial Port Mapping

### Physical Serial (Raspberry Pi)

When NeonBeam Core runs on a Pi 4 wired directly to the grblHAL MCU:

```yaml
# docker-compose.yml or docker-compose.prod.yml
services:
  hardware-comm:
    devices:
      - /dev/ttyACM0:/dev/ttyACM0   # USB-serial adapter → maps to same path inside container
    environment:
      COMM_SERIAL_PORT: /dev/ttyACM0
      COMM_BAUD_RATE: 115200
```

Common Pi serial device names:

| Connection type | Device |
|---|---|
| USB-serial adapter (CH340 / FT232) | `/dev/ttyUSB0` |
| Native USB CDC (grblHAL USB) | `/dev/ttyACM0` |
| Pi UART (GPIO 14/15) | `/dev/ttyAMA0` or `/dev/ttyS0` |

To verify which device the MCU appears on after plugging in:
```bash
dmesg | tail -20
ls /dev/tty*
```

### Telnet Socket (Development / Simulator)

During development without physical hardware, NeonBeam Core can connect to the [grblHAL Simulator](https://github.com/grblHAL/Simulator) over a local Telnet socket:

```
COMM_SERIAL_PORT=socket://host.docker.internal:23
```

`host.docker.internal` resolves to the host machine's IP from inside the Docker container.

### Remote Telnet (grblHAL network-enabled boards)

Some grblHAL boards (e.g. ESP32 with WiFi) expose a raw TCP port:

```
COMM_SERIAL_PORT=socket://192.168.1.20:23
```

---

## Development — Full Stack on One Host

### Prerequisites

- Docker Desktop
- Python 3.11+ (optional — only if running without Docker)
- grblHAL Simulator (optional for hardware-free testing)

### Steps

```bash
# 1. From the repo root, start only NeonBeam Core
docker compose up hardware-comm

# 2. View live logs
docker compose logs -f hardware-comm

# 3. Enable verbose debug logging for a session (without editing .env)
docker compose run --rm \
  -e COMM_LOG_LEVEL=DEBUG \
  -e COMM_LOG_OUTPUT=both \
  -e COMM_LOG_VERBOSE_SERIAL=true \
  hardware-comm
```

The API is available at **http://localhost:8000**.  
Interactive docs: **http://localhost:8000/docs** (Swagger UI).

### Without Docker

```bash
cd hardware_comm
python -m venv .venv && source .venv/bin/activate    # Linux/macOS
python -m venv .venv && .venv\Scripts\activate       # Windows
pip install -r requirements.txt

# Set env vars manually (or use a .env file with python-dotenv)
export COMM_SERIAL_PORT=socket://localhost:23
export COMM_LOG_LEVEL=DEBUG

uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## Production (Raspberry Pi 4)

### Prerequisites

- Raspberry Pi 4 running Raspberry Pi OS (64-bit recommended)
- Docker + Docker Compose installed on the Pi
- grblHAL MCU wired via USB or UART

### Steps

```bash
# 1. Clone the NeonBeam Core repo onto the Pi
git clone <your-remote-url> neonbeam-core
cd neonbeam-core

# 2. Create production environment file
cp .env.example .env
#    Edit .env — critical settings:
#      COMM_SERIAL_PORT=/dev/ttyACM0
#      COMM_LOG_LEVEL=INFO
#      COMM_LOG_OUTPUT=both
#      COMM_LOG_FILE=/app/logs/neonbeam_core.log

# 3. Add the pi user to the dialout group (required for serial port access)
sudo usermod -aG dialout $USER
#    Log out and back in for the group change to take effect.

# 4. Build and start
docker compose up -d --build hardware-comm

# 5. Check logs
docker compose logs -f hardware-comm
#    Or tail the persisted file on the host:
tail -f ./logs/hardware_comm/neonbeam_core.log

# 6. Enable auto-restart on boot
#    (docker compose restart policy: unless-stopped handles this automatically)
```

### Log Persistence on the Pi

The default Docker Compose volume mount persists logs to the host filesystem:

```
./logs/hardware_comm/neonbeam_core.log
```

Logs rotate automatically when they reach `COMM_LOG_MAX_BYTES` (default 10 MB), keeping the last `COMM_LOG_BACKUP_COUNT` (default 5) backup files.

---

## Git Repository

NeonBeam Core is designed to be maintained as its own standalone git repository.

```bash
cd hardware_comm
git init
git remote add origin <your-remote-url>
git add .
git commit -m "Initial commit — NeonBeam Core"
git push -u origin main
```

---

## Project Layout

```
hardware_comm/
├── app/
│   ├── main.py             # FastAPI app + all route definitions
│   └── services/
│       ├── serial_manager.py   # Async serial/Telnet connection manager
│       ├── gcode_streamer.py   # Buffer-fill GCode streaming engine
│       ├── log_config.py       # Logging bootstrap (reads COMM_LOG_* env vars)
│       └── log_names.py        # Logger name constants
├── requirements.txt
└── Dockerfile
```
