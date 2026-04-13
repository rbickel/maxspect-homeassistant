"""Async ICV6 protocol client for Maxspect integration.

The ICV6 uses two TCP protocols on port 80:
  - Old protocol (FF EE DD CC): initial bus-prime / search
  - New protocol (DD EE FF): device-level commands

All blocking socket I/O is run in a thread-pool executor so it never
blocks the HA event loop.
"""

from __future__ import annotations

import asyncio
import datetime
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

ICV6_MODE_NAMES: dict[int, str] = {0: "Manual", 1: "Auto Schedule", 2: "Auto Schedule"}


def compute_current_levels(
    schedule: list[dict],
    mode: int,
    manual_channels: list[int],
    now: datetime.datetime | None = None,
) -> list[int]:
    """Return the current output brightness for each LED channel.

    Manual mode (0) → returns the stored manual setpoints.
    Auto Schedule mode (1/2) → linearly interpolates between the two nearest
    schedule points using the current wall-clock time.  Outside the schedule
    window the adjacent endpoint value is returned (the last point typically
    ramps to 0).
    """
    if mode == 0 or not schedule:
        return list(manual_channels)

    if now is None:
        now = datetime.datetime.now()
    now_minutes = now.hour * 60 + now.minute

    points: list[tuple[int, list[int]]] = []
    for pt in schedule:
        h, m = pt["time"].split(":")
        points.append((int(h) * 60 + int(m), pt["channels"]))
    points.sort(key=lambda p: p[0])

    if not points:
        return list(manual_channels)

    # Before first point → return first point's values
    if now_minutes < points[0][0]:
        return list(points[0][1])

    # After last point → return last point's values
    if now_minutes >= points[-1][0]:
        return list(points[-1][1])

    # Interpolate between the two bracketing points
    for i in range(len(points) - 1):
        t0, ch0 = points[i]
        t1, ch1 = points[i + 1]
        if t0 <= now_minutes < t1:
            span = t1 - t0
            if span == 0:
                return list(ch0)
            frac = (now_minutes - t0) / span
            return [round(ch0[c] + (ch1[c] - ch0[c]) * frac) for c in range(len(ch0))]

    return list(points[-1][1])


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
    group_num: int = 0        # group/zone the device belongs to
    # Device metadata (parsed from device_id string)
    serial_number: str = ""   # e.g. "A001602" from "R5S2A001602"
    hw_version: str = ""      # e.g. "S2" (series/revision from device_id)
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


def _find_new_packet(resp: bytes | None, expected_sub: int) -> bytes | None:
    """Return the first new-protocol packet matching *expected_sub*."""
    if not resp:
        return None
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

        # Strip padding (0xFF, 0x00, whitespace) before decoding
        raw_device_id = raw_id[1:].rstrip(b"\x00\xff \t\r\n")
        device_id = raw_device_id.decode("ascii", errors="replace").rstrip()

        attrs: dict = {}
        if offset + 5 <= len(data):
            attrs["status_byte"]  = data[offset]
            attrs["channel_count"] = data[offset + 1]
            attrs["group_num"]    = data[offset + 2]
            attrs["power_state"]  = data[offset + 4]
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
    """Blocking device discovery — may take up to ~40 s on a cold bus.

    The ICV6's internal serial bus to peripheral devices needs several
    TCP connections worth of 'beginToSearch' primes to wake up.  From a
    completely cold start this can require 10+ primes; a warm bus responds
    after 1-2.

    Strategy:
      1. Burst-prime phase — send 4 rapid beginToSearch commands on separate
         TCP connections without waiting for search results.  This warms the
         internal serial bus as quickly as possible.
      2. Query phase — alternate prime + query up to 12 times, checking for
         search results after each query.
    """
    prime_pkt = _build_new(_CTRL_ID, 1, 2, 0x21)

    # ------------------------------------------------------------------
    # Phase 1: burst-prime the serial bus (4 rapid primes)
    # ------------------------------------------------------------------
    _LOGGER.debug("ICV6 burst-priming serial bus for %s …", host)
    for i in range(4):
        try:
            conn = _ICV6Connection(host, port)
            conn.connect()
            conn.send_recv(prime_pkt, wait=0.5)
            conn.close()
        except (OSError, socket.timeout):
            _LOGGER.debug("ICV6 burst-prime %d/4 connection failed", i + 1)
        time.sleep(0.2)

    # ------------------------------------------------------------------
    # Phase 2: prime + query loop (up to 12 attempts)
    # ------------------------------------------------------------------
    all_devices: list[dict] = []

    for attempt in range(12):
        _LOGGER.debug("ICV6 search attempt %d/12 …", attempt + 1)

        # Prime
        try:
            conn = _ICV6Connection(host, port)
            conn.connect()
            conn.send_recv(prime_pkt, wait=0.8)
            conn.close()
        except (OSError, socket.timeout):
            _LOGGER.debug("ICV6 prime connection failed on attempt %d", attempt + 1)
            time.sleep(0.3)
            continue

        time.sleep(0.3)

        # Query area 1
        try:
            conn = _ICV6Connection(host, port)
            conn.connect()
            resp = conn.send_recv(
                _build_new(_CTRL_ID, 1, 2, 0x22, bytes([1])), wait=1.5
            )
            conn.close()
        except (OSError, socket.timeout):
            _LOGGER.debug("ICV6 query connection failed on attempt %d", attempt + 1)
            time.sleep(0.3)
            continue

        if resp:
            pkt = _find_new_packet(resp, 0x22)
            if pkt:
                devs = _parse_search_result(pkt)
                if devs:
                    _LOGGER.debug(
                        "ICV6 found %d device(s) on attempt %d",
                        len(devs), attempt + 1,
                    )
                    all_devices.extend(devs)

        if all_devices:
            # Bus is warm — query areas 2-4
            for area_num in range(2, 5):
                time.sleep(0.3)
                try:
                    conn = _ICV6Connection(host, port)
                    conn.connect()
                    resp = conn.send_recv(
                        _build_new(_CTRL_ID, 1, 2, 0x22, bytes([area_num])),
                        wait=1.0,
                    )
                    conn.close()
                except (OSError, socket.timeout):
                    continue
                if resp:
                    pkt = _find_new_packet(resp, 0x22)
                    if pkt:
                        all_devices.extend(_parse_search_result(pkt))
            return all_devices

        time.sleep(0.3)

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
            pkt = _find_new_packet(resp, 0x14)
            if pkt is None or len(pkt) <= 20:
                continue
            payload = pkt[20:-1]
            if len(payload) < num_channels + 2:
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

        Discovery can take up to ~35 s in the worst case on a cold bus,
        because _sync_discover performs an initial burst/prime phase and
        then retries multiple times with sleeps between attempts.
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
            # Parse serial and hw version from device_id string.
            # Format: "<type><series><serial>", e.g. "R5S2A001602"
            #  type = first 2 chars ("R5"), series = next 2 ("S2"),
            #  serial = remainder ("A001602")
            did = d["device_id"]
            hw_ver = did[2:4] if len(did) > 3 else ""
            serial = did[4:] if len(did) > 4 else did

            devices.append(ICV6ChildDevice(
                device_id=did,
                device_type=dev_type,
                type_name=type_name,
                proto_cmd=proto_cmd,
                num_channels=num_channels,
                area=d.get("area", 0),
                group_num=attrs.get("group_num", 0),
                serial_number=serial,
                hw_version=hw_ver,
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
