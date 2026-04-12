"""Async ICV6 protocol client for Maxspect integration.

The ICV6 uses two TCP protocols on port 80:
  - Old protocol (FF EE DD CC): initial bus-prime / search
  - New protocol (DD EE FF): device-level commands

All blocking socket I/O is run in a thread-pool executor so it never
blocks the HA event loop.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time
from dataclasses import dataclass, field

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

ICV6_TCP_PORT = 80
ICV6_TIMEOUT = 5

# Device type table:  prefix → (display_name, proto_cmd, num_channels)
ICV6_DEVICE_TYPES: dict[str, tuple[str, int, int]] = {
    "R5": ("RSX R5 LED", 0x0F, 4),
    "R6": ("RSX R6 LED", 0x0E, 6),
    "E5": ("Ethereal E5 LED", 0x0B, 5),
    "F2": ("Floodlight LED", 0x0D, 4),
    "T1": ("Turbine Pump", 0x0C, 0),
    "G2": ("Gyre 2 Pump", 0x10, 0),
    "G3": ("Gyre 3 Pump", 0x11, 0),
    "A1": ("EggPoints A1", 0x0C, 0),
}

ICV6_MODE_NAMES: dict[int, str] = {0: "Manual", 1: "Auto Schedule"}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ICV6ChildDevice:
    """Runtime state for a single device connected to the ICV6 hub."""

    device_id: str
    device_type: str          # e.g. "R5", "G2"
    type_name: str            # human-readable, e.g. "RSX R5 LED"
    proto_cmd: int            # protocol command byte for this device type
    num_channels: int         # 0 for pumps, 4-6 for LEDs
    area: int = 0
    # Runtime state (updated on each poll)
    is_on: bool = True
    mode: int = 0
    manual_channels: list[int] = field(default_factory=list)
    schedule: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ICV6ConnectionError(Exception):
    """Raised when the ICV6 is unreachable."""


# ---------------------------------------------------------------------------
# Pure protocol helpers (blocking, to be called from executor)
# ---------------------------------------------------------------------------

def _build_new(device_id: bytes, module: int, cmd: int, sub: int,
               payload: bytes = b"") -> bytes:
    """Build a new-protocol (DD EE FF) packet."""
    dev = device_id.ljust(11, b"\xff")[:11]
    body = bytes([0xFF]) + dev + bytes([module, cmd, sub]) + payload
    length = len(body) + 1          # +1 for checksum
    header = bytes([0xDD, 0xEE, 0xFF]) + struct.pack(">H", length)
    raw = header + body
    return raw + bytes([sum(raw[3:]) & 0xFF])


def _find_new_packet(resp: bytes, expected_sub: int) -> bytes | None:
    """Return the first new-protocol packet matching *expected_sub*."""
    idx = 0
    while idx < len(resp) - 5:
        if resp[idx:idx + 3] == b"\xdd\xee\xff":
            pkt_len = struct.unpack(">H", resp[idx + 3:idx + 5])[0]
            pkt_end = idx + 5 + pkt_len
            if pkt_end <= len(resp):
                pkt = resp[idx:pkt_end]
                if len(pkt) > 19 and pkt[19] == expected_sub:
                    return pkt
            idx = pkt_end if pkt_end > idx else idx + 1
        elif resp[idx:idx + 4] == b"\xff\xee\xdd\xcc":
            pkt_len = resp[idx + 4] if idx + 4 < len(resp) else 0
            idx = idx + 5 + pkt_len + 1
        else:
            idx += 1
    return None


def _extract_new_payload(resp: bytes) -> bytes | None:
    """Extract the data payload from the first new-protocol packet found."""
    if not resp:
        return None
    for start in range(len(resp)):
        if resp[start:start + 3] == b"\xdd\xee\xff":
            pkt_len = struct.unpack(">H", resp[start + 3:start + 5])[0]
            pkt = resp[start:start + 5 + pkt_len]
            if len(pkt) > 20:
                return pkt[20:-1]   # between sub-command and checksum
    return None


def _parse_search_result(resp: bytes) -> list[dict]:
    """Parse a getSearchResult response → list of raw device dicts."""
    devices: list[dict] = []
    data = resp[20:-1]
    if len(data) < 3:
        return devices

    area = data[0]
    count = data[1]
    offset = 2

    for _ in range(count):
        if offset >= len(data):
            break
        offset += 1                         # type_code byte (unused here)

        if offset + 12 > len(data):
            break
        raw_id = data[offset:offset + 12]
        offset += 12

        device_id = raw_id[1:].decode("ascii", errors="replace")

        attrs: dict = {}
        if offset + 5 <= len(data):
            attrs["power_state"] = data[offset + 4]
            offset += 5

        dev_type: str | None = None
        for t in ICV6_DEVICE_TYPES:
            if device_id.startswith(t):
                dev_type = t
                break

        devices.append({
            "area": area,
            "device_id": device_id,
            "device_type": dev_type,
            "attrs": attrs,
        })

        while offset < len(data) and data[offset] == 0:
            offset += 1

    return devices


# ---------------------------------------------------------------------------
# Low-level blocking I/O helpers
# ---------------------------------------------------------------------------

class _ICV6Connection:
    """Blocking TCP connection to the ICV6."""

    def __init__(self, host: str, port: int = ICV6_TCP_PORT,
                 timeout: int = ICV6_TIMEOUT) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock: socket.socket | None = None

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect((self.host, self.port))
        self.sock.sendall(b"heartbeat")
        time.sleep(0.3)
        try:
            self.sock.recv(4096)
        except socket.timeout:
            pass

    def close(self) -> None:
        if self.sock:
            self.sock.close()
            self.sock = None

    def send_recv(self, pkt: bytes, wait: float = 1.0) -> bytes | None:
        assert self.sock is not None
        self.sock.sendall(pkt)
        time.sleep(wait)
        all_data = b""
        self.sock.settimeout(0.5)
        try:
            while True:
                try:
                    chunk = self.sock.recv(4096)
                    if chunk:
                        all_data += chunk
                    else:
                        break
                except socket.timeout:
                    break
        finally:
            self.sock.settimeout(self.timeout)
        clean = all_data.replace(b"heartbeat", b"")
        return clean if clean else None

    def __enter__(self) -> "_ICV6Connection":
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Blocking worker functions (called via run_in_executor)
# ---------------------------------------------------------------------------

_CTRL_ID = b"I6A1A\xff\xff\xff\xff\xff\xff"


def _sync_validate(host: str, port: int) -> None:
    """Raise OSError / socket.timeout if the ICV6 is not reachable."""
    with _ICV6Connection(host, port):
        pass


def _sync_discover(host: str, port: int) -> list[dict]:
    """Blocking device discovery — may take up to ~10 s on a cold bus."""
    conn = _ICV6Connection(host, port)
    conn.connect()

    all_devices: list[dict] = []

    for attempt in range(8):
        _LOGGER.debug("ICV6 search attempt %d/8 …", attempt + 1)

        # Prime the serial bus
        conn.send_recv(_build_new(_CTRL_ID, 1, 2, 0x21), wait=0.8)

        # Fresh connection for area 1 query
        conn.close()
        time.sleep(0.3)
        conn.connect()

        resp = conn.send_recv(
            _build_new(_CTRL_ID, 1, 2, 0x22, bytes([1])), wait=1.5
        )
        if resp:
            pkt = _find_new_packet(resp, 0x22)
            if pkt:
                all_devices.extend(_parse_search_result(pkt))

        if all_devices:
            # Bus is warm — query areas 2-4
            for area_num in range(2, 5):
                conn.close()
                time.sleep(0.3)
                conn.connect()
                resp = conn.send_recv(
                    _build_new(_CTRL_ID, 1, 2, 0x22, bytes([area_num])),
                    wait=1.0,
                )
                if resp:
                    pkt = _find_new_packet(resp, 0x22)
                    if pkt:
                        all_devices.extend(_parse_search_result(pkt))
            conn.close()
            return all_devices

        conn.close()
        time.sleep(0.3)
        conn.connect()

    conn.close()
    return []


def _sync_read_device_all(host: str, port: int, device_id: str,
                          proto_cmd: int, num_channels: int) -> dict | None:
    """Blocking read of all device config; retry once if first attempt fails."""
    for _ in range(2):
        try:
            with _ICV6Connection(host, port) as conn:
                resp = conn.send_recv(
                    _build_new(device_id.encode(), 1, proto_cmd, 0x14),
                    wait=1.0,
                )
            payload = _extract_new_payload(resp)
            if not payload or len(payload) < num_channels + 2:
                continue

            result: dict = {
                "mode": payload[0],
                "manual_channels": list(payload[1:1 + num_channels]),
            }

            sched_start = 1 + num_channels
            if sched_start < len(payload):
                num_points = payload[sched_start]
                pt_size = 3 + num_channels
                points = []
                for i in range(num_points):
                    off = sched_start + 1 + i * pt_size
                    if off + pt_size <= len(payload):
                        points.append({
                            "point": payload[off],
                            "time": f"{payload[off + 1]:02d}:{payload[off + 2]:02d}",
                            "channels": list(payload[off + 3:off + 3 + num_channels]),
                        })
                result["schedule"] = points

            return result
        except (OSError, socket.timeout):
            pass
    return None


def _sync_set_power(host: str, port: int, device_id: str,
                    proto_cmd: int, on: bool) -> bool:
    """Blocking power on/off command. Returns True on success."""
    try:
        with _ICV6Connection(host, port) as conn:
            conn.send_recv(
                _build_new(device_id.encode(), 1, proto_cmd, 0x02,
                           bytes([1 if on else 0])),
                wait=1.5,
            )
        return True
    except (OSError, socket.timeout):
        return False


def _sync_set_brightness(host: str, port: int, device_id: str,
                         proto_cmd: int, channels: list[int]) -> bool:
    """Blocking brightness write. Values 0-100 (%). Returns True on success."""
    payload = bytes(max(0, min(100, v)) for v in channels)
    try:
        with _ICV6Connection(host, port) as conn:
            conn.send_recv(
                _build_new(device_id.encode(), 1, proto_cmd, 0x0C, payload),
                wait=2.0,
            )
        return True
    except (OSError, socket.timeout):
        return False


# ---------------------------------------------------------------------------
# Public async client
# ---------------------------------------------------------------------------

class ICV6Client:
    """Async interface to the Maxspect ICV6 aquarium controller."""

    def __init__(self, host: str, port: int = ICV6_TCP_PORT) -> None:
        self.host = host
        self.port = port

    async def async_validate_connection(self) -> None:
        """Raise ICV6ConnectionError if the device is not reachable."""
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, _sync_validate, self.host, self.port)
        except (OSError, socket.timeout) as err:
            raise ICV6ConnectionError(
                f"Cannot connect to ICV6 at {self.host}:{self.port}"
            ) from err

    async def async_discover_devices(self) -> list[ICV6ChildDevice]:
        """Discover all devices connected to the ICV6 hub.

        This may take up to ~10 s on a cold bus (8 warm-up attempts).
        """
        loop = asyncio.get_running_loop()
        try:
            raw = await loop.run_in_executor(
                None, _sync_discover, self.host, self.port
            )
        except (OSError, socket.timeout) as err:
            _LOGGER.error("ICV6 discovery failed: %s", err)
            return []

        devices: list[ICV6ChildDevice] = []
        for d in raw:
            dev_type = d.get("device_type")
            if not dev_type or dev_type not in ICV6_DEVICE_TYPES:
                _LOGGER.debug(
                    "Skipping unknown ICV6 device_id=%s (no matching type prefix)",
                    d.get("device_id"),
                )
                continue
            type_name, proto_cmd, num_channels = ICV6_DEVICE_TYPES[dev_type]
            attrs = d.get("attrs", {})
            power_state = attrs.get("power_state", 1)
            devices.append(ICV6ChildDevice(
                device_id=d["device_id"],
                device_type=dev_type,
                type_name=type_name,
                proto_cmd=proto_cmd,
                num_channels=num_channels,
                area=d.get("area", 0),
                is_on=bool(power_state),
            ))

        _LOGGER.debug("ICV6 discovered %d device(s): %s",
                      len(devices), [d.device_id for d in devices])
        return devices

    async def async_read_device(self, device_id: str, proto_cmd: int,
                                num_channels: int) -> dict | None:
        """Read all config from a single child device."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, _sync_read_device_all,
            self.host, self.port, device_id, proto_cmd, num_channels,
        )

    async def async_set_power(self, device_id: str,
                              proto_cmd: int, on: bool) -> bool:
        """Turn a child device on or off. Returns True on success."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, _sync_set_power,
            self.host, self.port, device_id, proto_cmd, on,
        )

    async def async_set_brightness(self, device_id: str, proto_cmd: int,
                                   channels: list[int]) -> bool:
        """Set LED channel brightness (0-100 %). Returns True on success."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, _sync_set_brightness,
            self.host, self.port, device_id, proto_cmd, channels,
        )
