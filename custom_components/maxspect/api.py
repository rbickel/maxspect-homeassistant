"""Gizwits LAN protocol client for Maxspect devices.

Communicates via TCP 12416 using the binary Gizwits frame format:
  [00 00 00 03] [LEB128 length] [flag(1) + cmd(2 BE) + payload]

Push payloads use Gizwits V4 var_len format:
  [action: 1B] [attr_flags: 6B] [dp_data...]

The device pushes data in several message types:
  - Compact telemetry  (flags[0]=0x10) -- periodic sensor readings
  - State notify        (DP 34 only) -- power state + timestamp
  - Mode updates        (DP 18 flagged) -- mode value changes
  - Config data         (DPs 35/36) -- program blobs (ignored)

See MAXSPECT_PROTOCOL.MD for full protocol documentation.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .const import (
    ACTION_DEVICE_REPORT,
    ACTION_READ,
    ACTION_WRITE,
    ATTR_FLAGS_LEN,
    CMD_BIND_ACK,
    CMD_BIND_REQ,
    CMD_DATA_RECV,
    CMD_DATA_SEND,
    CMD_DEV_INFO_REQ,
    CMD_DEV_INFO_RESP,
    CMD_HEARTBEAT_REQ,
    CMD_HEARTBEAT_RESP,
    DP_LENGTHS,
    FRAME_HEADER,
    HEARTBEAT_INTERVAL,
    MODE_NAMES,
    MODE_OFF,
    MODE_ON,
    POLL_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

# State notify polls DP 34 (Time) -- returns power + timestamp
READ_STATE_NOTIFY = bytes([ACTION_READ]) + b"\x00\x04\x00\x00\x00\x00"


class MaxspectConnectionError(Exception):
    """Error communicating with the Maxspect device."""


@dataclass
class MaxspectDeviceState:
    """Parsed device state from push frames."""

    is_on: bool = False
    mode: int = 0
    last_active_mode: int = MODE_ON
    ch1_rpm: int = 0
    ch1_voltage: float = 0.0
    ch1_power: int = 0
    ch2_rpm: int = 0
    ch2_voltage: float = 0.0
    ch2_power: int = 0
    timestamp: str = ""

    @property
    def mode_name(self) -> str:
        """Return human-readable mode name."""
        return MODE_NAMES.get(self.mode, f"Unknown ({self.mode})")


# -- Frame encoding / decoding ----------------------------------------


def _encode_leb128(value: int) -> bytes:
    result = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            byte |= 0x80
        result.append(byte)
        if not value:
            break
    return bytes(result)


def _build_frame(cmd: int, payload: bytes = b"", flag: int = 0x00) -> bytes:
    """Build a Gizwits LAN frame."""
    data = bytes([flag]) + struct.pack(">H", cmd) + payload
    return FRAME_HEADER + _encode_leb128(len(data)) + data


async def _read_frame(
    reader: asyncio.StreamReader, timeout: float = 5.0,
) -> dict[str, Any] | None:
    """Read and parse one Gizwits LAN frame."""
    header = await asyncio.wait_for(reader.readexactly(4), timeout=timeout)
    if header != FRAME_HEADER:
        _LOGGER.warning("Unexpected header: %s", header.hex())
        return None

    length = 0
    shift = 0
    while True:
        b = (await asyncio.wait_for(reader.readexactly(1), timeout=timeout))[0]
        length |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7

    data = await asyncio.wait_for(reader.readexactly(length), timeout=timeout)
    if len(data) < 3:
        return {"flag": 0, "cmd": 0, "payload": data}

    return {
        "flag": data[0],
        "cmd": struct.unpack(">H", data[1:3])[0],
        "payload": data[3:],
    }


# -- Attr flags helpers ------------------------------------------------


def _dp_is_flagged(flags: bytes, dp_id: int) -> bool:
    """Check if a data point ID has its bit set in attr_flags."""
    byte_idx = ATTR_FLAGS_LEN - 1 - (dp_id // 8)
    bit_idx = dp_id % 8
    return 0 <= byte_idx < len(flags) and bool(flags[byte_idx] & (1 << bit_idx))


def _dp_data_offset(flags: bytes, dp_id: int) -> int:
    """Calculate the byte offset of a non-bool DP in the data payload."""
    offset = 0
    for did in sorted(DP_LENGTHS):
        if did >= dp_id:
            break
        if _dp_is_flagged(flags, did):
            offset += DP_LENGTHS[did]
    return offset


def _dp_attr_flags(dp_id: int) -> bytes:
    """Build 6-byte attr_flags with a single DP bit set."""
    flags = bytearray(ATTR_FLAGS_LEN)
    byte_idx = ATTR_FLAGS_LEN - 1 - (dp_id // 8)
    bit_idx = dp_id % 8
    if 0 <= byte_idx < ATTR_FLAGS_LEN:
        flags[byte_idx] = 1 << bit_idx
    return bytes(flags)


def _build_write_payload(dp_id: int, value: int) -> bytes:
    """Build a write payload for a uint8 DP: [0x12] [flags (6B)] [value (1B)]."""
    return bytes([ACTION_WRITE]) + _dp_attr_flags(dp_id) + bytes([value & 0xFF])


# -- Push payload parsing ----------------------------------------------


def _parse_compact_telemetry(
    data: bytes, state: MaxspectDeviceState,
) -> None:
    """Parse compact telemetry data (after 7-byte header).

    Layout (25 bytes):
      [0]     mode (0-5)
      [2:4]   ch1_rpm (uint16 BE)
      [4:6]   ch1_voltage (uint16 BE, /100 = volts)
      [7]     ch1_power (uint8, watts)
      [11:13] ch2_rpm (uint16 BE)
      [13:15] ch2_voltage (uint16 BE, /100 = volts)
      [16]    ch2_power (uint8, watts)
    """
    if len(data) < 17:
        return

    state.mode = data[0]
    state.is_on = state.mode != MODE_OFF
    if state.is_on:
        state.last_active_mode = state.mode
    state.ch1_rpm = struct.unpack(">H", data[2:4])[0]
    state.ch1_voltage = struct.unpack(">H", data[4:6])[0] / 100.0
    state.ch1_power = data[7]
    state.ch2_rpm = struct.unpack(">H", data[11:13])[0]
    state.ch2_voltage = struct.unpack(">H", data[13:15])[0] / 100.0
    state.ch2_power = data[16]


def _parse_state_notify(data: bytes, state: MaxspectDeviceState) -> None:
    """Parse DP 34 (Time) data -- power flag + timestamp.

    Layout (7 bytes):
      [0]     power (bit 0: 1=on, 0=off)
      [1:7]   timestamp (YY MM DD HH MM SS)
    """
    if len(data) < 1:
        return

    # Note: data[0] bit 0 is the device *power* flag (hardware has power),
    # NOT whether the pumps are running.  Pump on/off is determined solely
    # by Mode (3 = off, anything else = on) which is set by compact
    # telemetry and mode-update pushes.

    if len(data) >= 7:
        ts = data[1:7]
        if ts[0] <= 99 and 1 <= ts[1] <= 12 and 1 <= ts[2] <= 31:
            state.timestamp = (
                f"20{ts[0]:02d}-{ts[1]:02d}-{ts[2]:02d} "
                f"{ts[3]:02d}:{ts[4]:02d}:{ts[5]:02d}"
            )


# -- Client class ------------------------------------------------------


class MaxspectClient:
    """Async TCP client for Maxspect devices using Gizwits LAN protocol."""

    def __init__(self, host: str, port: int = 12416) -> None:
        self._host = host
        self._port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False
        self._state = MaxspectDeviceState()
        self._listener_task: asyncio.Task[None] | None = None
        self._state_event = asyncio.Event()
        self._update_callback: Callable[[], None] | None = None

    @property
    def host(self) -> str:
        return self._host

    @property
    def state(self) -> MaxspectDeviceState:
        return self._state

    @property
    def connected(self) -> bool:
        return self._connected and self._writer is not None

    def set_update_callback(self, callback: Callable[[], None]) -> None:
        self._update_callback = callback

    # -- Connection management -----------------------------------------

    async def async_connect(self) -> None:
        """Connect, handshake, and start the background listener."""
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            self._listener_task = None

        await self._async_close_transport()

        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port), timeout=10,
            )
        except (OSError, asyncio.TimeoutError) as err:
            raise MaxspectConnectionError(
                f"Cannot connect to {self._host}:{self._port}: {err}"
            ) from err

        self._connected = True
        _LOGGER.debug("Connected to %s:%s", self._host, self._port)

        try:
            await self._handshake()
        except Exception as err:
            await self.async_disconnect()
            raise MaxspectConnectionError(
                f"Handshake failed with {self._host}: {err}"
            ) from err

        self._listener_task = asyncio.create_task(self._listen_loop())

    async def _handshake(self) -> None:
        assert self._writer is not None
        assert self._reader is not None

        self._writer.write(_build_frame(CMD_DEV_INFO_REQ))
        await self._writer.drain()

        resp = await _read_frame(self._reader, timeout=5)
        if resp is None or resp["cmd"] != CMD_DEV_INFO_RESP:
            raise MaxspectConnectionError("No device info response")

        binding_key = resp["payload"]
        _LOGGER.debug("Binding key: %s", binding_key.hex())

        self._writer.write(_build_frame(CMD_BIND_REQ, payload=binding_key))
        await self._writer.drain()

        resp = await _read_frame(self._reader, timeout=5)
        if resp is None or resp["cmd"] != CMD_BIND_ACK:
            raise MaxspectConnectionError("Bind not acknowledged")

        _LOGGER.debug("Handshake complete with %s", self._host)

        # Drain delayed duplicate ACK
        try:
            await _read_frame(self._reader, timeout=1)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            pass

    async def async_disconnect(self) -> None:
        self._connected = False
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        self._listener_task = None
        await self._async_close_transport()

    async def _async_close_transport(self) -> None:
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._writer = None
            self._reader = None

    # -- Background listener -------------------------------------------

    async def _listen_loop(self) -> None:
        """Read frames, send heartbeats, poll for status."""
        loop = asyncio.get_running_loop()
        last_heartbeat = loop.time()
        last_poll = 0.0

        while self._connected and self._reader and self._writer:
            now = loop.time()

            # Heartbeat
            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                try:
                    self._writer.write(_build_frame(CMD_HEARTBEAT_REQ))
                    await self._writer.drain()
                    _LOGGER.debug("Heartbeat sent to %s", self._host)
                    last_heartbeat = now
                except OSError:
                    _LOGGER.warning("Heartbeat send failed to %s", self._host)
                    self._connected = False
                    break

            # Poll state notify for power + timestamp
            if now - last_poll >= POLL_INTERVAL:
                try:
                    self._writer.write(
                        _build_frame(CMD_DATA_SEND, payload=READ_STATE_NOTIFY)
                    )
                    await self._writer.drain()
                    last_poll = now
                except OSError:
                    _LOGGER.warning("Poll send failed to %s", self._host)
                    self._connected = False
                    break

            try:
                resp = await _read_frame(self._reader, timeout=2)
            except asyncio.TimeoutError:
                continue
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                _LOGGER.warning("Connection lost to %s", self._host)
                self._connected = False
                break

            if resp is None:
                continue

            if resp["cmd"] == CMD_DATA_RECV:
                self._process_push(resp["payload"])
            elif resp["cmd"] == CMD_HEARTBEAT_RESP:
                _LOGGER.debug("Heartbeat ACK from %s", self._host)
                last_heartbeat = loop.time()

        _LOGGER.debug("Listener stopped for %s", self._host)

    def _process_push(self, payload: bytes) -> None:
        """Parse a 0x0091 push payload and update state."""
        if not payload:
            _LOGGER.debug("Write ACK from %s", self._host)
            return

        if len(payload) < 7:
            return

        action = payload[0]
        flags = payload[1:7]
        data = payload[7:]

        if action != ACTION_DEVICE_REPORT:
            _LOGGER.debug(
                "Non-report action 0x%02x from %s (%dB)",
                action, self._host, len(payload),
            )
            return

        updated = False

        # Compact telemetry: flags[0] bit 4 = firmware telemetry DP
        if flags[0] & 0x10:
            _parse_compact_telemetry(data, self._state)
            _LOGGER.debug(
                "Compact telemetry from %s: mode=%d ch1=%drpm/%dW ch2=%drpm/%dW",
                self._host, self._state.mode,
                self._state.ch1_rpm, self._state.ch1_power,
                self._state.ch2_rpm, self._state.ch2_power,
            )
            updated = True
        elif _dp_is_flagged(flags, 34) and not any(
            _dp_is_flagged(flags, dp) for dp in (35, 36)
        ):
            # DP 34 (Time) only = state notify (power + timestamp)
            _parse_state_notify(data, self._state)
            _LOGGER.debug(
                "State notify from %s: power=%s ts=%s",
                self._host, self._state.is_on, self._state.timestamp,
            )
            updated = True
        elif _dp_is_flagged(flags, 18):
            # Any push with Mode DP -- extract mode
            offset = _dp_data_offset(flags, 18)
            if offset < len(data):
                new_mode = data[offset]
                self._state.mode = new_mode
                self._state.is_on = new_mode != MODE_OFF
                if self._state.is_on:
                    self._state.last_active_mode = new_mode
                _LOGGER.debug(
                    "Mode update from %s: mode=%d (%s)",
                    self._host, new_mode,
                    MODE_NAMES.get(new_mode, "unknown"),
                )
                updated = True

        if not updated:
            _LOGGER.debug(
                "Push from %s: %dB flags=%s (ignored)",
                self._host, len(payload), flags.hex(),
            )

        if updated:
            self._state_event.set()
            if self._update_callback:
                self._update_callback()

    # -- Public API ----------------------------------------------------

    async def async_request_status(self) -> MaxspectDeviceState:
        if not self.connected:
            await self.async_connect()

        if self._state_event.is_set():
            return self._state

        try:
            await asyncio.wait_for(self._state_event.wait(), timeout=10)
        except asyncio.TimeoutError:
            _LOGGER.warning("No status from %s within 10s", self._host)

        return self._state

    async def async_validate_connection(self) -> None:
        """Connect, handshake, disconnect."""
        await self.async_connect()
        await self.async_disconnect()

    async def async_set_mode(self, mode: int) -> None:
        """Write Mode DP (18) to the device."""
        if not self.connected:
            await self.async_connect()
        assert self._writer is not None

        payload = _build_write_payload(dp_id=18, value=mode)
        self._writer.write(_build_frame(CMD_DATA_SEND, payload=payload))
        await self._writer.drain()
        _LOGGER.debug("Sent Mode=%d to %s", mode, self._host)

    async def async_turn_on(self) -> None:
        """Turn the pump on by restoring the last active mode."""
        await self.async_set_mode(self._state.last_active_mode)

    async def async_turn_off(self) -> None:
        """Turn the pump off (Mode=3)."""
        if self._state.is_on:
            self._state.last_active_mode = self._state.mode
        await self.async_set_mode(MODE_OFF)
