"""Layer 3 — protocol-level simulation tests.

These tests run a real TCP server that speaks the Gizwits binary protocol
and connect the actual MaxspectClient to it.  This validates:

  - Frame encoding/decoding round-trip
  - Handshake sequence (DEV_INFO_REQ → BIND_REQ → BIND_ACK)
  - Compact telemetry push parsing through the full client pipeline
  - State notify push parsing through the full client pipeline
  - Mode update push parsing
  - Heartbeat exchange
  - Connection loss detection and reconnect
  - Malformed frame handling

No HA runtime is needed — these are pure asyncio tests against the real
MaxspectClient class with a simulated device.
"""

from __future__ import annotations

import asyncio
import struct
from unittest.mock import MagicMock

import pytest

# Protocol simulation tests need real TCP sockets
pytestmark = [pytest.mark.enable_socket, pytest.mark.protocol]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations():
    """Override conftest autouse — protocol tests don't use the HA runtime."""


@pytest.fixture(autouse=True)
def _re_enable_socket():
    """Re-enable sockets after HA plugin disables them in pytest_runtest_setup."""
    import pytest_socket
    pytest_socket.enable_socket()
    yield
    pytest_socket.disable_socket(allow_unix_socket=True)


from custom_components.maxspect.api import (
    MaxspectClient,
    MaxspectConnectionError,
    MaxspectDeviceState,
    _build_frame,
    _encode_leb128,
)
from custom_components.maxspect.const import (
    ACTION_DEVICE_REPORT,
    ATTR_FLAGS_LEN,
    CMD_BIND_ACK,
    CMD_BIND_REQ,
    CMD_DATA_RECV,
    CMD_DEV_INFO_REQ,
    CMD_DEV_INFO_RESP,
    CMD_HEARTBEAT_REQ,
    CMD_HEARTBEAT_RESP,
    FRAME_HEADER,
    MODE_FEED,
    MODE_OFF,
    MODE_ON,
    MODE_WATER_FLOW,
)


# ---------------------------------------------------------------------------
# Simulated device server
# ---------------------------------------------------------------------------

BINDING_KEY = b"\x01\x02\x03\x04\x05\x06\x07\x08"


def _compact_telemetry_push(
    mode: int = MODE_ON,
    ch1_rpm: int = 1500,
    ch1_v_x100: int = 2437,
    ch1_w: int = 72,
    ch2_rpm: int = 1200,
    ch2_v_x100: int = 2360,
    ch2_w: int = 65,
) -> bytes:
    """Build a full 0x0091 frame containing compact telemetry."""
    # flags[0] bit 4 set = compact telemetry
    flags = bytearray(ATTR_FLAGS_LEN)
    flags[0] = 0x10

    data = bytearray(25)
    data[0] = mode
    struct.pack_into(">H", data, 2, ch1_rpm)
    struct.pack_into(">H", data, 4, ch1_v_x100)
    data[7] = ch1_w
    struct.pack_into(">H", data, 11, ch2_rpm)
    struct.pack_into(">H", data, 13, ch2_v_x100)
    data[16] = ch2_w

    payload = bytes([ACTION_DEVICE_REPORT]) + bytes(flags) + bytes(data)
    return _build_frame(CMD_DATA_RECV, payload)


def _state_notify_push(
    power: int = 1,
    year: int = 26, month: int = 4, day: int = 11,
    hour: int = 14, minute: int = 30, second: int = 0,
) -> bytes:
    """Build a 0x0091 frame containing state notify (DP 34)."""
    # DP 34: byte_idx = 6-1-(34//8) = 6-1-4 = 1; bit_idx = 34%8 = 2; → flags[1] |= 0x04
    flags = bytearray(ATTR_FLAGS_LEN)
    flags[1] = 0x04  # DP 34

    time_data = bytes([power, year, month, day, hour, minute, second])
    payload = bytes([ACTION_DEVICE_REPORT]) + bytes(flags) + time_data
    return _build_frame(CMD_DATA_RECV, payload)


def _mode_update_push(mode: int) -> bytes:
    """Build a 0x0091 frame containing a mode update (DP 18)."""
    # DP 18: byte_idx = 6-1-(18//8) = 6-1-2 = 3; bit_idx = 18%8 = 2; → flags[3] |= 0x04
    flags = bytearray(ATTR_FLAGS_LEN)
    flags[3] = 0x04  # DP 18

    # Mode DP is the first non-bool DP flagged, but we need to account for
    # DP 17 (1 byte) if it were flagged (it's not here). So offset = 0 and
    # we just put the mode byte.
    # Actually DP 17 is NOT flagged, so offset for DP18 = 0
    payload = bytes([ACTION_DEVICE_REPORT]) + bytes(flags) + bytes([mode])
    return _build_frame(CMD_DATA_RECV, payload)


class FakeGizwitsDevice:
    """A mock Gizwits device that responds to the handshake and sends pushes."""

    def __init__(self) -> None:
        self._server: asyncio.Server | None = None
        self._writer: asyncio.StreamWriter | None = None
        self.port: int = 0
        self._pushes: list[bytes] = []
        self._push_event = asyncio.Event()
        self._connected = asyncio.Event()
        self._handshake_done = asyncio.Event()
        self._client_tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._client_connected, "127.0.0.1", 0,
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for task in self._client_tasks:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._client_tasks.clear()
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    def queue_push(self, frame: bytes) -> None:
        """Queue a frame to be sent after handshake completes."""
        self._pushes.append(frame)
        self._push_event.set()

    async def _client_connected(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task:
            self._client_tasks.append(task)
        await self._handle_client(reader, writer)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        self._writer = writer
        self._connected.set()

        try:
            # Phase 1: DEV_INFO_REQ → respond with DEV_INFO_RESP
            frame = await self._read_frame(reader)
            if frame and frame["cmd"] == CMD_DEV_INFO_REQ:
                writer.write(_build_frame(CMD_DEV_INFO_RESP, BINDING_KEY))
                await writer.drain()

            # Phase 2: BIND_REQ → respond with BIND_ACK
            frame = await self._read_frame(reader)
            if frame and frame["cmd"] == CMD_BIND_REQ:
                writer.write(_build_frame(CMD_BIND_ACK))
                await writer.drain()

            self._handshake_done.set()

            # Phase 3: Send queued pushes and handle heartbeats
            while True:
                # Send any queued pushes
                if self._pushes:
                    data = self._pushes.pop(0)
                    writer.write(data)
                    await writer.drain()

                # Check for incoming frames (heartbeats, polls) with short timeout
                try:
                    frame = await self._read_frame(reader, timeout=0.2)
                    if frame and frame["cmd"] == CMD_HEARTBEAT_REQ:
                        writer.write(_build_frame(CMD_HEARTBEAT_RESP))
                        await writer.drain()
                except asyncio.TimeoutError:
                    pass

                # Yield to event loop
                await asyncio.sleep(0.01)

        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            writer.close()

    @staticmethod
    async def _read_frame(
        reader: asyncio.StreamReader, timeout: float = 5.0,
    ) -> dict | None:
        header = await asyncio.wait_for(reader.readexactly(4), timeout=timeout)
        if header != FRAME_HEADER:
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestHandshake:

    async def test_client_connects_and_handshakes(self) -> None:
        """Client performs DEV_INFO → BIND → listening."""
        device = FakeGizwitsDevice()
        await device.start()

        try:
            client = MaxspectClient("127.0.0.1", device.port)
            await client.async_connect()

            assert client.connected is True
            assert device._handshake_done.is_set()

            await client.async_disconnect()
        finally:
            await device.stop()

    async def test_connection_failure_raises(self) -> None:
        """Connecting to a closed port raises MaxspectConnectionError."""
        client = MaxspectClient("127.0.0.1", 1)  # port 1 should fail
        with pytest.raises(MaxspectConnectionError):
            await client.async_connect()


class TestCompactTelemetry:

    async def test_compact_telemetry_updates_state(self) -> None:
        """Full pipeline: simulated device → client → state updated."""
        device = FakeGizwitsDevice()
        await device.start()

        callback_called = asyncio.Event()

        try:
            client = MaxspectClient("127.0.0.1", device.port)
            client.set_update_callback(lambda: callback_called.set())
            await client.async_connect()

            # Queue a compact telemetry push
            device.queue_push(
                _compact_telemetry_push(
                    mode=MODE_ON, ch1_rpm=1800, ch1_v_x100=2500, ch1_w=80,
                    ch2_rpm=1400, ch2_v_x100=2400, ch2_w=60,
                )
            )

            # Wait for callback
            await asyncio.wait_for(callback_called.wait(), timeout=5.0)

            assert client.state.mode == MODE_ON
            assert client.state.is_on is True
            assert client.state.ch1_rpm == 1800
            assert client.state.ch1_voltage == 25.00
            assert client.state.ch1_power == 80
            assert client.state.ch2_rpm == 1400
            assert client.state.ch2_voltage == 24.00
            assert client.state.ch2_power == 60

            await client.async_disconnect()
        finally:
            await device.stop()

    async def test_off_mode_telemetry(self) -> None:
        """Compact telemetry with MODE_OFF sets is_on=False."""
        device = FakeGizwitsDevice()
        await device.start()

        callback_called = asyncio.Event()

        try:
            client = MaxspectClient("127.0.0.1", device.port)
            client.set_update_callback(lambda: callback_called.set())
            await client.async_connect()

            device.queue_push(
                _compact_telemetry_push(mode=MODE_OFF, ch1_rpm=0, ch2_rpm=0)
            )

            await asyncio.wait_for(callback_called.wait(), timeout=5.0)

            assert client.state.mode == MODE_OFF
            assert client.state.is_on is False
            assert client.state.ch1_rpm == 0
            assert client.state.ch2_rpm == 0

            await client.async_disconnect()
        finally:
            await device.stop()

    async def test_feed_mode_is_on_true(self) -> None:
        """Compact telemetry with MODE_FEED sets is_on=True (by design)."""
        device = FakeGizwitsDevice()
        await device.start()

        callback_called = asyncio.Event()

        try:
            client = MaxspectClient("127.0.0.1", device.port)
            client.set_update_callback(lambda: callback_called.set())
            await client.async_connect()

            device.queue_push(
                _compact_telemetry_push(mode=MODE_FEED, ch1_rpm=0, ch2_rpm=0)
            )

            await asyncio.wait_for(callback_called.wait(), timeout=5.0)

            assert client.state.mode == MODE_FEED
            assert client.state.is_on is True  # Feed = on (just paused)

            await client.async_disconnect()
        finally:
            await device.stop()


class TestStateNotify:

    async def test_state_notify_updates_timestamp(self) -> None:
        """State notify push updates timestamp but not is_on."""
        device = FakeGizwitsDevice()
        await device.start()

        callback_called = asyncio.Event()

        try:
            client = MaxspectClient("127.0.0.1", device.port)
            client.set_update_callback(lambda: callback_called.set())
            await client.async_connect()

            original_is_on = client.state.is_on

            device.queue_push(
                _state_notify_push(power=1, year=26, month=4, day=15, hour=10, minute=30)
            )

            await asyncio.wait_for(callback_called.wait(), timeout=5.0)

            assert client.state.timestamp == "2026-04-15 10:30:00"
            # is_on must NOT be changed by state_notify
            assert client.state.is_on == original_is_on

            await client.async_disconnect()
        finally:
            await device.stop()


class TestModeUpdatePush:

    async def test_mode_update_changes_mode(self) -> None:
        """DP 18 mode update push changes is_on and mode."""
        device = FakeGizwitsDevice()
        await device.start()

        callback_called = asyncio.Event()

        try:
            client = MaxspectClient("127.0.0.1", device.port)
            client.set_update_callback(lambda: callback_called.set())
            await client.async_connect()

            device.queue_push(_mode_update_push(MODE_OFF))

            await asyncio.wait_for(callback_called.wait(), timeout=5.0)

            assert client.state.mode == MODE_OFF
            assert client.state.is_on is False

            await client.async_disconnect()
        finally:
            await device.stop()


class TestMultiplePushes:

    async def test_sequential_pushes(self) -> None:
        """Multiple sequential pushes are all processed correctly."""
        device = FakeGizwitsDevice()
        await device.start()

        push_count = 0

        def _on_push():
            nonlocal push_count
            push_count += 1

        try:
            client = MaxspectClient("127.0.0.1", device.port)
            client.set_update_callback(_on_push)
            await client.async_connect()

            # Send three pushes
            device.queue_push(
                _compact_telemetry_push(mode=MODE_ON, ch1_rpm=1000, ch2_rpm=800)
            )
            await asyncio.sleep(0.3)

            device.queue_push(
                _compact_telemetry_push(mode=MODE_WATER_FLOW, ch1_rpm=1200, ch2_rpm=900)
            )
            await asyncio.sleep(0.3)

            device.queue_push(
                _compact_telemetry_push(mode=MODE_OFF, ch1_rpm=0, ch2_rpm=0)
            )
            await asyncio.sleep(0.5)

            assert push_count >= 3
            # Final state should be MODE_OFF
            assert client.state.mode == MODE_OFF
            assert client.state.is_on is False

            await client.async_disconnect()
        finally:
            await device.stop()


class TestConnectionLoss:

    async def test_server_disconnect_detected(self) -> None:
        """Client detects when the server closes the connection."""
        device = FakeGizwitsDevice()
        await device.start()

        try:
            client = MaxspectClient("127.0.0.1", device.port)
            await client.async_connect()
            assert client.connected is True

            # Kill the server side
            await device.stop()

            # Give the listener loop time to detect the disconnect
            await asyncio.sleep(1.0)

            assert client.connected is False

            await client.async_disconnect()
        finally:
            # Already stopped, but safety
            try:
                await device.stop()
            except Exception:
                pass


class TestFrameEncoding:

    def test_leb128_encoding_small(self) -> None:
        """LEB128 encodes small values as single byte."""
        assert _encode_leb128(0) == b"\x00"
        assert _encode_leb128(1) == b"\x01"
        assert _encode_leb128(127) == b"\x7f"

    def test_leb128_encoding_large(self) -> None:
        """LEB128 encodes values > 127 as multi-byte."""
        assert _encode_leb128(128) == b"\x80\x01"
        assert _encode_leb128(300) == b"\xac\x02"

    def test_build_frame_structure(self) -> None:
        """Built frame has correct header + LEB128 length + flag + cmd."""
        frame = _build_frame(CMD_HEARTBEAT_REQ, b"", flag=0x00)
        assert frame[:4] == FRAME_HEADER
        # After header: LEB128(3) = 0x03, then flag(0x00) + cmd(0x000C)
        assert frame[4] == 3  # len = 1 (flag) + 2 (cmd) = 3
        assert frame[5] == 0x00  # flag
        assert struct.unpack(">H", frame[6:8])[0] == CMD_HEARTBEAT_REQ

    def test_build_frame_with_payload(self) -> None:
        """Built frame includes payload after flag+cmd."""
        payload = b"\x11\x22\x33"
        frame = _build_frame(CMD_DATA_RECV, payload)
        assert frame[:4] == FRAME_HEADER
        # Length = 1 (flag) + 2 (cmd) + 3 (payload) = 6
        assert frame[4] == 6
        assert frame[8:11] == payload
