"""Gizwits LAN protocol client for Maxspect devices.

Communicates via TCP 12416 using the binary Gizwits frame format:
  [00 00 00 03] [LEB128 length] [flag(1) + cmd(2 BE) + payload]

Push payloads use Gizwits V4 var_len format:
  [action: 1B] [attr_flags: 6B] [dp_data...]

The device pushes data in several message types:
  - Full status        (1049 bytes) -- fixed-layout state and sensor readings
  - Compact telemetry  (flags[0]=0x10) -- periodic sensor readings
  - State notify        (DP 34 only) -- power state + timestamp
  - Mode updates        (DP 18 flagged) -- mode value changes
  - Config data         (DPs 35/36) -- program blobs (raw diagnostics)

See MAXSPECT_PROTOCOL.MD for full protocol documentation.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from datetime import datetime, timedelta
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .const import (
    ACTION_DEVICE_REPORT,
    ACTION_READ,
    ACTION_READ_ACK,
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
    GYRE_DP_NAMES,
    MODE_NAMES,
    MODE_OFF,
    MODE_ON,
    MODE_EXIT_FEED,
)

_LOGGER = logging.getLogger(__name__)

# Payloads for explicit reads only; the listener never polls attributes.
READ_STATE_NOTIFY = bytes([ACTION_READ]) + b"\x00\x04\x00\x00\x00\x00"

# Request all 47 schema data points, including settings and diagnostics.
READ_CONFIG_DPS = bytes([ACTION_READ]) + b"\x7f\xff\xff\xff\xff\xff"


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
    feed_duration: int = 0    # DP 19 Time_Feed (minutes, 5-120)
    model_a: int = 0          # DP 20 Model_A (pump A model code)
    model_b: int = 0          # DP 21 Model_B (pump B model code)
    wash_reminder: int = 0    # DP 22 Wash (wash reminder days)
    # Cloud-seeded attrs for non-Gyre device types
    generic_attrs: dict = field(default_factory=dict)
    # Track if immutable attributes have been initialized
    _model_initialized: bool = field(default=False, init=False, repr=False)
    _initialized_models: set[int] = field(default_factory=set, init=False, repr=False)

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
    offset = (sum(_dp_is_flagged(flags, dp) for dp in range(17)) + 7) // 8
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
    """Build a write payload for a uint8 DP: [0x11] [flags (6B)] [value (1B)]."""
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
      [7]     ch1_power (uint8, unscaled electrical value)
      [11:13] ch2_rpm (uint16 BE)
      [13:15] ch2_voltage (uint16 BE, /100 = volts)
      [16]    ch2_power (uint8, unscaled electrical value)
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
        if (ts[0] <= 99 and 1 <= ts[1] <= 12 and 1 <= ts[2] <= 31
                and ts[3] <= 23 and ts[4] <= 59 and ts[5] <= 59):
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
        self.last_report_attrs: dict[str, Any] = {}
        self._clock_reference: tuple[datetime, float] | None = None
        self._clock_payload: str | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False
        self._state = MaxspectDeviceState()
        self._listener_task: asyncio.Task[None] | None = None
        self._state_event = asyncio.Event()
        self._mode_event = asyncio.Event()
        self._mode_lock = asyncio.Lock()
        self._reported_mode: int | None = None
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

        # A report can arrive before the delayed duplicate ACK.
        try:
            delayed = await _read_frame(self._reader, timeout=1)
            if delayed is not None and delayed["cmd"] == CMD_DATA_RECV:
                self._process_push(delayed["payload"])
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
        """Receive device pushes and send heartbeats, never attribute queries."""
        loop = asyncio.get_running_loop()
        last_heartbeat = loop.time()

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
            elif resp["cmd"] == 0x0094 and len(resp["payload"]) > 4:
                self._process_push(resp["payload"][4:])
            elif resp["cmd"] == CMD_HEARTBEAT_RESP:
                _LOGGER.debug("Heartbeat ACK from %s", self._host)
                last_heartbeat = loop.time()

        _LOGGER.debug("Listener stopped for %s", self._host)

    def _process_push(self, payload: bytes) -> None:
        """Merge a variable-length report or read response, including diagnostics."""
        if len(payload) < 7:
            _LOGGER.debug("Ignoring empty or short report from %s", self._host)
            return
        if payload[0] not in (ACTION_DEVICE_REPORT, ACTION_READ_ACK):
            _LOGGER.debug("Ignoring non-report action 0x%02x from %s", payload[0], self._host)
            return
        flags, data = payload[1:7], payload[7:]
        selected = [dp for dp in range(47) if _dp_is_flagged(flags, dp)]
        bool_dps = [dp for dp in selected if dp < 17]
        bool_len = (len(bool_dps) + 7) // 8
        expected = bool_len + sum(DP_LENGTHS[dp] for dp in selected if dp >= 17)
        if len(data) != expected:
            _LOGGER.warning(
                "Ignoring malformed report from %s: %d bytes, expected %d",
                self._host, len(data), expected,
            )
            return
        attrs: dict[str, Any] = {}
        bool_values = int.from_bytes(data[:bool_len], "big")
        for bit, dp in enumerate(bool_dps):
            attrs[GYRE_DP_NAMES[dp]] = bool(bool_values & (1 << bit))
        offset = bool_len
        for dp in selected:
            if dp < 17:
                continue
            length = DP_LENGTHS[dp]
            value = data[offset:offset + length]
            attrs[GYRE_DP_NAMES[dp]] = value[0] if dp < 33 else value.hex()
            offset += length
        if not attrs:
            return
        self.last_report_attrs = attrs
        self.apply_attributes(attrs)
        if "Mode" in attrs or "Bak24" in attrs:
            self._reported_mode = self._state.mode
            self._mode_event.set()
        self._state_event.set()
        if self._update_callback:
            self._update_callback()

    def apply_attributes(self, attrs: dict[str, Any]) -> None:
        """Merge named device attributes without discarding previous reports."""
        self._state.generic_attrs.update(attrs)
        state = self._state
        for name, parser in (("Bak24", _parse_compact_telemetry), ("Time", _parse_state_notify)):
            value = attrs.get(name)
            if isinstance(value, str):
                try:
                    parser(bytes.fromhex(value), state)
                    if name == "Time" and value != self._clock_payload:
                        raw = bytes.fromhex(value)
                        if len(raw) == 7:
                            clock = datetime(2000 + raw[1], *raw[2:])
                            self._clock_reference = (clock, time.monotonic())
                            self._clock_payload = value
                except ValueError:
                    _LOGGER.debug("Invalid hex in %s", name)
        mode = attrs.get("Mode")
        if isinstance(mode, int) and mode in MODE_NAMES:
            state.mode = mode
            state.is_on = mode != MODE_OFF
            if state.is_on:
                state.last_active_mode = mode
        duration = attrs.get("Time_Feed")
        if isinstance(duration, int) and 5 <= duration <= 120:
            state.feed_duration = duration
        wash = attrs.get("Wash")
        if isinstance(wash, int) and 0 <= wash <= 255:
            state.wash_reminder = wash
        for dp, name, field_name in ((20, "Model_A", "model_a"), (21, "Model_B", "model_b")):
            value = attrs.get(name)
            if value in (0, 1) and not state._model_initialized and dp not in state._initialized_models:
                setattr(state, field_name, value)
                state._initialized_models.add(dp)
        if state._initialized_models == {20, 21}:
            state._model_initialized = True

    # -- Public API ----------------------------------------------------

    def controller_time_now(self) -> datetime | None:
        if self._clock_reference is None:
            return None
        clock, received = self._clock_reference
        return clock + timedelta(seconds=time.monotonic() - received)

    async def async_request_full_status(self) -> None:
        """Send the SDK's sequenced read request and the legacy read request."""
        if not self.connected:
            await self.async_connect()
        assert self._writer is not None
        self._writer.write(_build_frame(0x0093, payload=b"\x00\x00\x00\x03" + bytes([ACTION_READ]) + b"\xff" * 6))
        self._writer.write(_build_frame(CMD_DATA_SEND, payload=READ_CONFIG_DPS))
        await self._writer.drain()

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
        """Write Mode DP (18), then wait for an actual device mode report."""
        async with self._mode_lock:
            await self._async_set_mode(mode)

    async def _async_set_mode(self, mode: int) -> None:
        """Serialize writes so another command cannot consume confirmation."""
        if mode not in MODE_NAMES:
            raise ValueError("Unsupported Gyre mode")
        if not self.connected:
            await self.async_connect()
        assert self._writer is not None

        self._mode_event.clear()
        payload = _build_write_payload(dp_id=18, value=mode)
        self._writer.write(_build_frame(CMD_DATA_SEND, payload=payload))
        await self._writer.drain()
        _LOGGER.debug("Sent Mode=%d to %s", mode, self._host)
        expected = {0, 1, MODE_ON} if mode in (MODE_ON, MODE_EXIT_FEED) else {mode}
        try:
            async with asyncio.timeout(8):
                while True:
                    await self._mode_event.wait()
                    self._mode_event.clear()
                    if self._reported_mode in expected:
                        return
        except TimeoutError as err:
            raise MaxspectConnectionError("Device did not confirm the requested mode") from err

    async def async_turn_on(self) -> None:
        """Turn the pump on by restoring the last active mode."""
        await self.async_set_mode(self._state.last_active_mode)

    async def async_turn_off(self) -> None:
        """Turn the pump off (Mode=3)."""
        if self._state.is_on:
            self._state.last_active_mode = self._state.mode
        await self.async_set_mode(MODE_OFF)
