# hardware_comm/app/services/mdns_advertiser.py
"""
mDNS service advertisement for NeonBeam Core.

Registers this service as  _http._tcp.local.  with a TXT record
    service=hardware_comm
so the NeonBeam OS Discovery Sidecar can find it automatically via its
mDNS browser without any manual IP entry.

Important: mDNS uses multicast UDP (224.0.0.251:5353).
  - On Linux (Raspberry Pi) with network_mode: host in Docker Compose, the
    container shares the host's network stack and multicast reaches the LAN.
  - On Docker Desktop (Windows / macOS), the container is behind a NAT and
    multicast does NOT cross to the LAN, so this advertisement is invisible
    to remote devices.  The sidecar's subnet scan covers this gap.
"""

from __future__ import annotations

import logging
import os
import socket

from zeroconf import ServiceInfo, Zeroconf

logger = logging.getLogger("hardware_comm.mdns")

_SERVICE_TYPE = "_http._tcp.local."
_SERVICE_NAME = "NeonBeam Core._http._tcp.local."
_SERVICE_TAG  = "hardware_comm"


def _get_lan_ip() -> str:
    """Return the host's primary LAN IP (the IP other devices can reach)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class MdnsAdvertiser:
    """
    Registers and unregisters the NeonBeam Core mDNS service record.

    Usage (in FastAPI lifespan):
        advertiser = MdnsAdvertiser()
        advertiser.start()
        yield
        advertiser.stop()
    """

    def __init__(self) -> None:
        self._zc: Zeroconf | None = None
        self._info: ServiceInfo | None = None

    def start(self) -> None:
        port = int(os.getenv("COMM_PORT", "8000"))
        lan_ip = _get_lan_ip()
        hostname = socket.gethostname() + ".local."

        self._info = ServiceInfo(
            _SERVICE_TYPE,
            _SERVICE_NAME,
            addresses=[socket.inet_aton(lan_ip)],
            port=port,
            properties={
                "service": _SERVICE_TAG,
                "version": "1.0",
            },
            server=hostname,
        )

        try:
            self._zc = Zeroconf()
            self._zc.register_service(self._info)
            logger.info(
                f"mDNS: advertising '{_SERVICE_TAG}' as '{_SERVICE_NAME}' "
                f"at {lan_ip}:{port}  (hostname={hostname})"
            )
        except Exception as exc:
            logger.warning(
                f"mDNS: advertisement failed — {exc}. "
                "Service will still be discoverable via the sidecar's subnet scan."
            )
            self._zc = None

    def stop(self) -> None:
        if self._zc and self._info:
            try:
                self._zc.unregister_service(self._info)
                logger.info("mDNS: service record unregistered.")
            except Exception as exc:
                logger.warning(f"mDNS: error during unregister — {exc}")
            finally:
                self._zc.close()
                self._zc = None
                self._info = None
