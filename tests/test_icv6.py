"""Tests for the ICV6 integration path.

Covers:
  1. Protocol helpers      — packet building, parsing, search-result decoding
  2. Data model            — ICV6ChildDevice defaults, device-type table
  3. Coordinator logic     — discovery, state polling, power/brightness control
                             (via a MockICV6Coordinator that mirrors the real one)
  4. Sensor entity values  — ICV6ModeSensor, ICV6ChannelSensor native_value
  5. Switch entity values  — ICV6PowerSwitch is_on, translation_key, async_turn_*

All tests work without a running Home Assistant instance.
"""

from __future__ import annotations

import struct
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.maxspect.icv6_api import (
    ICV6ChildDevice,
    ICV6ConnectionError,
    ICV6_DEVICE_TYPES,
    ICV6_MODE_NAMES,
    _build_new,
    _find_new_packet,
    _parse_search_result,
    _sync_read_device_all,
    _sync_set_brightness,
    _sync_set_power,
)
from custom_components.maxspect.sensor import (
    ICV6ChannelSensor,
    ICV6DeviceIdSensor,
    ICV6GroupSensor,
    ICV6ManualBrightnessSensor,
    ICV6ModeSensor,
    ICV6ScheduleSensor,
)
from custom_components.maxspect.switch import ICV6PowerSwitch


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

_HOST = "192.168.1.100"


def _led_device(
    device_id: str = "R5S2A001602",
    device_type: str = "R5",
    num_channels: int = 4,
    mode: int = 0,
    is_on: bool = True,
    channels: list[int] | None = None,
) -> ICV6ChildDevice:
    return ICV6ChildDevice(
        device_id=device_id,
        device_type=device_type,
        type_name=ICV6_DEVICE_TYPES[device_type][0],
        proto_cmd=ICV6_DEVICE_TYPES[device_type][1],
        num_channels=num_channels,
        is_on=is_on,
        mode=mode,
        manual_channels=channels if channels is not None else [50, 60, 70, 80],
    )


def _pump_device(device_id: str = "G2X0B001234", device_type: str = "G2") -> ICV6ChildDevice:
    return ICV6ChildDevice(
        device_id=device_id,
        device_type=device_type,
        type_name=ICV6_DEVICE_TYPES[device_type][0],
        proto_cmd=ICV6_DEVICE_TYPES[device_type][1],
        num_channels=0,
        is_on=True,
    )


def _coordinator(
    devices: dict[str, ICV6ChildDevice] | None = None,
    host: str = _HOST,
) -> MagicMock:
    """Return a mock ICV6Coordinator suitable for entity property tests."""
    coord = MagicMock()
    coord.data = devices or {}
    coord.host = host
    coord.last_update_success = True
    return coord


# ---------------------------------------------------------------------------
# Section 1 — Protocol helpers
# ---------------------------------------------------------------------------

class TestICV6PacketBuilding:
    """_build_new produces correctly framed packets."""

    def test_header_prefix(self) -> None:
        pkt = _build_new(b"R5S2A001602", 1, 0x0F, 0x14)
        assert pkt[:3] == b"\xdd\xee\xff", "new-protocol header must start DD EE FF"

    def test_length_field_matches_body(self) -> None:
        pkt = _build_new(b"R5S2A001602", 1, 0x0F, 0x14)
        declared_len = struct.unpack(">H", pkt[3:5])[0]
        # body runs from pkt[5] to end (checksum is the last byte, included in length)
        actual_body_len = len(pkt) - 5
        assert declared_len == actual_body_len

    def test_sub_command_byte_in_packet(self) -> None:
        """Sub-command 0x14 should appear at offset 19."""
        pkt = _build_new(b"R5S2A001602", 1, 0x0F, 0x14)
        assert pkt[19] == 0x14

    def test_checksum_is_valid(self) -> None:
        """Checksum = sum of bytes[3:] & 0xFF, appended as last byte."""
        pkt = _build_new(b"R5S2A001602", 1, 0x0F, 0x14)
        expected_cs = sum(pkt[3:-1]) & 0xFF
        assert pkt[-1] == expected_cs

    def test_payload_appended_after_sub(self) -> None:
        payload = bytes([10, 20, 30])
        pkt = _build_new(b"R5S2A001602", 1, 0x0F, 0x0C, payload)
        # payload starts at offset 20 (header 5 + body prefix 15 + sub 1 = 21 → idx 20)
        assert pkt[20:23] == payload

    def test_short_device_id_is_padded(self) -> None:
        """A device ID shorter than 11 bytes must be padded with 0xFF.

        Packet layout: [DD EE FF][len 2B][0xFF][dev_id 11B][mod][cmd][sub]...
        Header = 5 bytes, then 0xFF at [5], dev_id starts at [6].
        b"SHORT" is 5 bytes → padding fills bytes [11]..[16].
        """
        pkt = _build_new(b"SHORT", 1, 0x0F, 0x14)
        # First byte past the 5-char id is at offset 6+5 = 11 — must be 0xFF padding
        assert pkt[11] == 0xFF

    def test_long_device_id_is_truncated(self) -> None:
        """A device ID longer than 11 bytes must be silently truncated."""
        long_id = b"R5S2A001602EXTRA"
        normal = _build_new(b"R5S2A001602", 1, 0x0F, 0x14)
        truncated = _build_new(long_id, 1, 0x0F, 0x14)
        assert len(normal) == len(truncated), "truncated packet must be same length"


class TestICV6PacketParsing:
    """_find_new_packet decodes packets correctly."""

    def _make_response(self, sub: int, payload: bytes = b"") -> bytes:
        """Build a minimal valid new-protocol response packet."""
        # packet = DD EE FF [len 2B] [0xFF] [11-byte dev_id] [module cmd sub] [payload] [cs]
        body_before_cs = (
            bytes([0xFF])
            + b"I6A1A\xff\xff\xff\xff\xff\xff"  # 11-byte ctrl_id
            + bytes([0x01, 0x50 | 0x02, sub])    # module, cmd (response), sub
            + payload
        )
        length = len(body_before_cs) + 1  # +1 for checksum
        header = bytes([0xDD, 0xEE, 0xFF]) + struct.pack(">H", length)
        raw = header + body_before_cs
        return raw + bytes([sum(raw[3:]) & 0xFF])

    def test_find_packet_matches_sub(self) -> None:
        resp = self._make_response(sub=0x22, payload=bytes([1, 0]))
        pkt = _find_new_packet(resp, 0x22)
        assert pkt is not None

    def test_find_packet_returns_none_for_wrong_sub(self) -> None:
        resp = self._make_response(sub=0x22)
        assert _find_new_packet(resp, 0x14) is None

    def test_find_packet_in_concatenated_response(self) -> None:
        """_find_new_packet must scan past garbage / other packets."""
        junk = b"heartbeat" + bytes(10)
        resp = junk + self._make_response(sub=0x14, payload=bytes([0, 50, 60]))
        pkt = _find_new_packet(resp, 0x14)
        assert pkt is not None

    def test_find_packet_returns_data_bytes(self) -> None:
        data = bytes([0, 50, 60, 70, 80])  # mode + 4 channels
        resp = self._make_response(sub=0x14, payload=data)
        pkt = _find_new_packet(resp, 0x14)
        assert pkt is not None
        assert pkt[20:-1] == data

    def test_find_packet_none_for_empty_response(self) -> None:
        assert _find_new_packet(b"", 0x14) is None

    def test_find_packet_none_for_garbage(self) -> None:
        assert _find_new_packet(b"heartbeathearbeat" + bytes(20), 0x14) is None


class TestParseSearchResult:
    """_parse_search_result decodes the device discovery response."""

    def _make_search_packet(self, area: int, devices: list[dict]) -> bytes:
        """Build a synthetic search-result packet body (after the 20-byte header)."""
        body = bytes([area, len(devices)])
        for dev in devices:
            device_id_bytes = dev["device_id"].encode("ascii")
            # type_code byte + 'B' + device_id (11 bytes total)
            raw_id = b"B" + device_id_bytes.ljust(11)[:11]
            body += bytes([0x00])  # type_code
            body += raw_id
            # 6 attribute bytes: status, ch_count, group, attr3, attr4, power_state
            power = dev.get("power_state", 1)
            status = dev.get("status_byte", 0x01)
            body += bytes([status, dev.get("num_channels", 4), 0x01, 0x00, 0x00, power])
        # Pad to simulate the full packet header
        header = bytes(20) + body + bytes([sum(body) & 0xFF])
        return header

    def test_returns_empty_for_short_data(self) -> None:
        result = _parse_search_result(bytes(25))
        assert result == []

    def test_parses_single_led_device(self) -> None:
        pkt = self._make_search_packet(
            area=1, devices=[{"device_id": "R5S2A001602", "num_channels": 4}]
        )
        devices = _parse_search_result(pkt)
        assert len(devices) == 1
        assert devices[0]["device_id"] == "R5S2A001602"

    def test_device_type_detected_from_prefix(self) -> None:
        pkt = self._make_search_packet(
            area=1, devices=[{"device_id": "R5S2A001602", "num_channels": 4}]
        )
        devices = _parse_search_result(pkt)
        assert devices[0]["device_type"] == "R5"

    def test_area_stored_in_result(self) -> None:
        pkt = self._make_search_packet(
            area=2, devices=[{"device_id": "R6X0B001234", "num_channels": 6}]
        )
        devices = _parse_search_result(pkt)
        assert devices[0]["area"] == 2

    def test_power_state_in_attrs(self) -> None:
        pkt = self._make_search_packet(
            area=1, devices=[{"device_id": "R5S2A001602", "power_state": 0}]
        )
        devices = _parse_search_result(pkt)
        assert devices[0]["attrs"]["power_state"] == 0

    def test_power_state_on_in_attrs(self) -> None:
        pkt = self._make_search_packet(
            area=1, devices=[{"device_id": "R5S2A001602", "power_state": 1}]
        )
        devices = _parse_search_result(pkt)
        assert devices[0]["attrs"]["power_state"] == 1

    def test_status_byte_preserved_in_attrs(self) -> None:
        """status_byte (device mode) is extracted from the first attr byte."""
        pkt = self._make_search_packet(
            area=1, devices=[{"device_id": "R5S2A001602", "status_byte": 0x02}]
        )
        devices = _parse_search_result(pkt)
        assert devices[0]["attrs"]["status_byte"] == 0x02

    def test_unknown_prefix_yields_none_device_type(self) -> None:
        pkt = self._make_search_packet(
            area=1, devices=[{"device_id": "ZZUnknown001", "num_channels": 0}]
        )
        devices = _parse_search_result(pkt)
        # Unknown prefix → device_type is None
        assert devices[0]["device_type"] is None


# ---------------------------------------------------------------------------
# Section 2 — Data model
# ---------------------------------------------------------------------------

class TestICV6DeviceTypes:
    """ICV6_DEVICE_TYPES table sanity checks."""

    def test_all_led_types_have_positive_channel_count(self) -> None:
        led_prefixes = {"R5", "R6", "E5", "F2"}
        for prefix in led_prefixes:
            _, _, channels = ICV6_DEVICE_TYPES[prefix]
            assert channels > 0, f"{prefix} should have > 0 channels"

    def test_all_pump_types_have_zero_channels(self) -> None:
        pump_prefixes = {"T1", "G2", "G3", "A1"}
        for prefix in pump_prefixes:
            _, _, channels = ICV6_DEVICE_TYPES[prefix]
            assert channels == 0, f"{prefix} should have 0 channels"

    def test_mode_names_cover_expected_modes(self) -> None:
        assert ICV6_MODE_NAMES[0] == "Manual"
        assert ICV6_MODE_NAMES[1] == "Auto Schedule"

    def test_r5_has_four_channels(self) -> None:
        _, _, ch = ICV6_DEVICE_TYPES["R5"]
        assert ch == 4

    def test_r6_has_six_channels(self) -> None:
        _, _, ch = ICV6_DEVICE_TYPES["R6"]
        assert ch == 6

    def test_e5_has_five_channels(self) -> None:
        _, _, ch = ICV6_DEVICE_TYPES["E5"]
        assert ch == 5


class TestICV6ChildDeviceDefaults:
    """ICV6ChildDevice dataclass default values."""

    def test_default_is_on(self) -> None:
        dev = ICV6ChildDevice(
            device_id="R5X", device_type="R5", type_name="RSX R5 LED",
            proto_cmd=0x0F, num_channels=4,
        )
        assert dev.is_on is True

    def test_default_mode_zero(self) -> None:
        dev = ICV6ChildDevice(
            device_id="R5X", device_type="R5", type_name="RSX R5 LED",
            proto_cmd=0x0F, num_channels=4,
        )
        assert dev.mode == 0

    def test_default_channels_empty(self) -> None:
        dev = ICV6ChildDevice(
            device_id="R5X", device_type="R5", type_name="RSX R5 LED",
            proto_cmd=0x0F, num_channels=4,
        )
        assert dev.manual_channels == []

    def test_default_schedule_empty(self) -> None:
        dev = ICV6ChildDevice(
            device_id="R5X", device_type="R5", type_name="RSX R5 LED",
            proto_cmd=0x0F, num_channels=4,
        )
        assert dev.schedule == []


# ---------------------------------------------------------------------------
# Section 3 — Coordinator logic (via MockICV6Coordinator)
# ---------------------------------------------------------------------------

_REDISCOVER_INTERVAL = 300.0  # must match icv6_coordinator.py


class MockICV6Coordinator:
    """Mirrors ICV6Coordinator's _async_update_data, async_set_power,
    and async_set_brightness without requiring a real HA context."""

    def __init__(self, host: str = _HOST) -> None:
        self.host = host
        self.client = AsyncMock()
        self.data: dict[str, ICV6ChildDevice] = {}
        self._last_discovery: float = 0.0
        self._notifications: list[dict] = []

    def async_set_updated_data(self, data: dict) -> None:
        self.data = data
        self._notifications.append(dict(data))

    async def _async_update_data(self) -> dict[str, ICV6ChildDevice]:
        """Mirrors ICV6Coordinator._async_update_data.

        Full device reads only happen during discovery cycles (not every poll).
        """
        from homeassistant.helpers.update_coordinator import UpdateFailed

        now = time.monotonic()
        needs_discovery = (
            not self.data
            or (now - self._last_discovery) >= _REDISCOVER_INTERVAL
        )

        if not needs_discovery:
            return dict(self.data)

        discovered = await self.client.async_discover_devices()

        if not discovered and not self.data:
            raise UpdateFailed("No ICV6 devices found")

        current = dict(self.data)
        for dev in discovered:
            if dev.device_id not in current:
                current[dev.device_id] = dev
            else:
                existing = current[dev.device_id]
                existing.area = dev.area
                existing.is_on = dev.is_on
                existing.mode = dev.mode
                existing.group_num = dev.group_num
        self._last_discovery = now
        devices = current

        # Full device read — only during discovery cycles
        for device_id, dev in devices.items():
            if dev.num_channels == 0:
                continue
            state = await self.client.async_read_device(
                device_id, dev.proto_cmd, dev.num_channels
            )
            if state:
                dev.mode = state.get("mode", dev.mode)
                dev.manual_channels = state.get("manual_channels", dev.manual_channels)
                dev.schedule = state.get("schedule", dev.schedule)

        return devices

    async def async_set_power(self, device_id: str, on: bool) -> None:
        """Mirrors ICV6Coordinator.async_set_power."""
        dev = self.data.get(device_id)
        if dev is None:
            return
        ok = await self.client.async_set_power(device_id, dev.proto_cmd, on)
        if not ok:
            return
        dev.is_on = on
        self.async_set_updated_data(dict(self.data))

    async def async_set_brightness(self, device_id: str, channels: list[int]) -> None:
        """Mirrors ICV6Coordinator.async_set_brightness."""
        dev = self.data.get(device_id)
        if dev is None:
            return
        ok = await self.client.async_set_brightness(device_id, dev.proto_cmd, channels)
        if not ok:
            return
        dev.manual_channels = channels
        self.async_set_updated_data(dict(self.data))


def _mock_coordinator(host: str = _HOST) -> MockICV6Coordinator:
    return MockICV6Coordinator(host=host)


# ── Discovery ────────────────────────────────────────────────────────────────

class TestICV6CoordinatorDiscovery:

    async def test_first_update_calls_discover(self) -> None:
        c = _mock_coordinator()
        c.client.async_discover_devices.return_value = [_led_device()]
        c.client.async_read_device.return_value = None
        await c._async_update_data()
        c.client.async_discover_devices.assert_awaited_once()

    async def test_discovered_device_added_to_data(self) -> None:
        c = _mock_coordinator()
        dev = _led_device("R5S2A001602")
        c.client.async_discover_devices.return_value = [dev]
        c.client.async_read_device.return_value = None
        result = await c._async_update_data()
        assert "R5S2A001602" in result

    async def test_multiple_devices_all_added(self) -> None:
        c = _mock_coordinator()
        c.client.async_discover_devices.return_value = [
            _led_device("R5S2A001602"),
            _pump_device("G2X0B001234"),
        ]
        c.client.async_read_device.return_value = None
        result = await c._async_update_data()
        assert "R5S2A001602" in result
        assert "G2X0B001234" in result

    async def test_raises_update_failed_with_no_devices_cold_start(self) -> None:
        from homeassistant.helpers.update_coordinator import UpdateFailed
        c = _mock_coordinator()
        c.client.async_discover_devices.return_value = []
        with pytest.raises(UpdateFailed):
            await c._async_update_data()

    async def test_empty_rediscovery_keeps_existing_devices(self) -> None:
        c = _mock_coordinator()
        dev = _led_device("R5S2A001602")
        c.data = {"R5S2A001602": dev}
        c._last_discovery = time.monotonic()  # recent — no rediscovery
        c.client.async_read_device.return_value = None
        result = await c._async_update_data()
        assert "R5S2A001602" in result
        c.client.async_discover_devices.assert_not_awaited()

    async def test_rediscovery_triggered_after_interval(self) -> None:
        c = _mock_coordinator()
        dev = _led_device("R5S2A001602")
        c.data = {"R5S2A001602": dev}
        # Simulate interval elapsed
        c._last_discovery = time.monotonic() - _REDISCOVER_INTERVAL - 1
        c.client.async_discover_devices.return_value = [dev]
        c.client.async_read_device.return_value = None
        await c._async_update_data()
        c.client.async_discover_devices.assert_awaited_once()

    async def test_new_device_added_on_rediscovery(self) -> None:
        c = _mock_coordinator()
        existing = _led_device("R5S2A001602")
        new_dev = _pump_device("G2X0B001234")
        c.data = {"R5S2A001602": existing}
        c._last_discovery = time.monotonic() - _REDISCOVER_INTERVAL - 1
        c.client.async_discover_devices.return_value = [existing, new_dev]
        c.client.async_read_device.return_value = None
        result = await c._async_update_data()
        assert "G2X0B001234" in result

    async def test_existing_device_not_replaced_on_rediscovery(self) -> None:
        """Runtime channels must not be wiped; mode/is_on update from discovery."""
        c = _mock_coordinator()
        dev = _led_device("R5S2A001602", mode=1, channels=[10, 20, 30, 40])
        c.data = {"R5S2A001602": dev}
        c._last_discovery = time.monotonic() - _REDISCOVER_INTERVAL - 1
        freshly_discovered = _led_device("R5S2A001602")  # default mode=0
        c.client.async_discover_devices.return_value = [freshly_discovered]
        c.client.async_read_device.return_value = None
        result = await c._async_update_data()
        # mode now comes from discovery (freshly_discovered has mode=0)
        assert result["R5S2A001602"].mode == 0
        # manual_channels preserved (read_device returned None)
        assert result["R5S2A001602"].manual_channels == [10, 20, 30, 40]


# ── Polling ──────────────────────────────────────────────────────────────────

class TestICV6CoordinatorPolling:

    async def test_led_device_mode_updated_on_discovery(self) -> None:
        """Device reads only happen during discovery cycles."""
        c = _mock_coordinator()
        dev = _led_device("R5S2A001602", mode=0)
        c.data = {"R5S2A001602": dev}
        # Force a discovery cycle
        c._last_discovery = time.monotonic() - _REDISCOVER_INTERVAL - 1
        c.client.async_discover_devices.return_value = []
        c.client.async_read_device.return_value = {
            "mode": 1,
            "manual_channels": [10, 20, 30, 40],
            "schedule": [],
        }
        result = await c._async_update_data()
        assert result["R5S2A001602"].mode == 1

    async def test_led_device_channels_updated_on_discovery(self) -> None:
        c = _mock_coordinator()
        dev = _led_device("R5S2A001602", channels=[0, 0, 0, 0])
        c.data = {"R5S2A001602": dev}
        c._last_discovery = time.monotonic() - _REDISCOVER_INTERVAL - 1
        c.client.async_discover_devices.return_value = []
        c.client.async_read_device.return_value = {
            "mode": 0,
            "manual_channels": [25, 50, 75, 100],
            "schedule": [],
        }
        result = await c._async_update_data()
        assert result["R5S2A001602"].manual_channels == [25, 50, 75, 100]

    async def test_between_discoveries_returns_cached_state(self) -> None:
        """Between discovery cycles, no bus traffic — cached state returned."""
        c = _mock_coordinator()
        dev = _led_device("R5S2A001602", mode=0, channels=[10, 20, 30, 40])
        c.data = {"R5S2A001602": dev}
        c._last_discovery = time.monotonic()
        c.client.async_read_device.return_value = {
            "mode": 1,
            "manual_channels": [99, 99, 99, 99],
        }
        result = await c._async_update_data()
        # Should NOT have called read — no bus traffic
        c.client.async_read_device.assert_not_awaited()
        c.client.async_discover_devices.assert_not_awaited()
        # State unchanged
        assert result["R5S2A001602"].mode == 0
        assert result["R5S2A001602"].manual_channels == [10, 20, 30, 40]

    async def test_pump_device_not_polled(self) -> None:
        """Pumps have 0 channels — async_read_device must not be called for them."""
        c = _mock_coordinator()
        pump = _pump_device("G2X0B001234")
        c.data = {"G2X0B001234": pump}
        c._last_discovery = time.monotonic() - _REDISCOVER_INTERVAL - 1
        c.client.async_discover_devices.return_value = []
        await c._async_update_data()
        c.client.async_read_device.assert_not_awaited()

    async def test_none_poll_response_keeps_previous_channels(self) -> None:
        c = _mock_coordinator()
        dev = _led_device("R5S2A001602", channels=[40, 50, 60, 70])
        c.data = {"R5S2A001602": dev}
        c._last_discovery = time.monotonic() - _REDISCOVER_INTERVAL - 1
        c.client.async_discover_devices.return_value = []
        c.client.async_read_device.return_value = None  # device not responding
        result = await c._async_update_data()
        assert result["R5S2A001602"].manual_channels == [40, 50, 60, 70]

    async def test_schedule_updated_on_discovery(self) -> None:
        c = _mock_coordinator()
        dev = _led_device("R5S2A001602")
        c.data = {"R5S2A001602": dev}
        c._last_discovery = time.monotonic() - _REDISCOVER_INTERVAL - 1
        c.client.async_discover_devices.return_value = []
        schedule = [{"point": 1, "time": "08:00", "channels": [10, 20, 30, 40]}]
        c.client.async_read_device.return_value = {
            "mode": 1,
            "manual_channels": [50, 50, 50, 50],
            "schedule": schedule,
        }
        result = await c._async_update_data()
        assert result["R5S2A001602"].schedule == schedule

    async def test_read_called_with_correct_proto_cmd_on_discovery(self) -> None:
        c = _mock_coordinator()
        dev = _led_device("R5S2A001602", device_type="R5")
        expected_cmd = ICV6_DEVICE_TYPES["R5"][1]  # 0x0F
        c.data = {"R5S2A001602": dev}
        c._last_discovery = time.monotonic() - _REDISCOVER_INTERVAL - 1
        c.client.async_discover_devices.return_value = []
        c.client.async_read_device.return_value = None
        await c._async_update_data()
        c.client.async_read_device.assert_awaited_once_with(
            "R5S2A001602", expected_cmd, 4
        )


# ── Power control ─────────────────────────────────────────────────────────────

class TestICV6CoordinatorPowerControl:

    async def test_turn_on_sets_is_on_true(self) -> None:
        c = _mock_coordinator()
        dev = _led_device("R5S2A001602", is_on=False)
        c.data = {"R5S2A001602": dev}
        c.client.async_set_power.return_value = True
        await c.async_set_power("R5S2A001602", True)
        assert c.data["R5S2A001602"].is_on is True

    async def test_turn_off_sets_is_on_false(self) -> None:
        c = _mock_coordinator()
        dev = _led_device("R5S2A001602", is_on=True)
        c.data = {"R5S2A001602": dev}
        c.client.async_set_power.return_value = True
        await c.async_set_power("R5S2A001602", False)
        assert c.data["R5S2A001602"].is_on is False

    async def test_turn_on_notifies_subscribers(self) -> None:
        c = _mock_coordinator()
        dev = _led_device("R5S2A001602", is_on=False)
        c.data = {"R5S2A001602": dev}
        c.client.async_set_power.return_value = True
        n_before = len(c._notifications)
        await c.async_set_power("R5S2A001602", True)
        assert len(c._notifications) > n_before

    async def test_client_called_with_correct_args(self) -> None:
        c = _mock_coordinator()
        dev = _led_device("R5S2A001602", device_type="R5")
        expected_cmd = ICV6_DEVICE_TYPES["R5"][1]
        c.data = {"R5S2A001602": dev}
        c.client.async_set_power.return_value = True
        await c.async_set_power("R5S2A001602", True)
        c.client.async_set_power.assert_awaited_once_with("R5S2A001602", expected_cmd, True)

    async def test_failed_client_does_not_update_state(self) -> None:
        c = _mock_coordinator()
        dev = _led_device("R5S2A001602", is_on=True)
        c.data = {"R5S2A001602": dev}
        c.client.async_set_power.return_value = False
        n_before = len(c._notifications)
        await c.async_set_power("R5S2A001602", False)
        assert c.data["R5S2A001602"].is_on is True  # unchanged
        assert len(c._notifications) == n_before    # no notification

    async def test_unknown_device_id_is_silently_ignored(self) -> None:
        c = _mock_coordinator()
        c.data = {}
        # Must not raise
        await c.async_set_power("nonexistent", True)
        c.client.async_set_power.assert_not_awaited()

    async def test_pump_power_control_uses_correct_proto_cmd(self) -> None:
        c = _mock_coordinator()
        pump = _pump_device("G2X0B001234", device_type="G2")
        expected_cmd = ICV6_DEVICE_TYPES["G2"][1]  # 0x10
        c.data = {"G2X0B001234": pump}
        c.client.async_set_power.return_value = True
        await c.async_set_power("G2X0B001234", False)
        c.client.async_set_power.assert_awaited_once_with("G2X0B001234", expected_cmd, False)


# ── Brightness control ────────────────────────────────────────────────────────

class TestICV6CoordinatorBrightnessControl:

    async def test_brightness_updates_manual_channels(self) -> None:
        c = _mock_coordinator()
        dev = _led_device("R5S2A001602", channels=[0, 0, 0, 0])
        c.data = {"R5S2A001602": dev}
        c.client.async_set_brightness.return_value = True
        await c.async_set_brightness("R5S2A001602", [25, 50, 75, 100])
        assert c.data["R5S2A001602"].manual_channels == [25, 50, 75, 100]

    async def test_brightness_notifies_subscribers(self) -> None:
        c = _mock_coordinator()
        dev = _led_device("R5S2A001602")
        c.data = {"R5S2A001602": dev}
        c.client.async_set_brightness.return_value = True
        n_before = len(c._notifications)
        await c.async_set_brightness("R5S2A001602", [10, 20, 30, 40])
        assert len(c._notifications) > n_before

    async def test_brightness_client_called_with_correct_args(self) -> None:
        c = _mock_coordinator()
        dev = _led_device("R5S2A001602", device_type="R5")
        expected_cmd = ICV6_DEVICE_TYPES["R5"][1]
        c.data = {"R5S2A001602": dev}
        c.client.async_set_brightness.return_value = True
        await c.async_set_brightness("R5S2A001602", [10, 20, 30, 40])
        c.client.async_set_brightness.assert_awaited_once_with(
            "R5S2A001602", expected_cmd, [10, 20, 30, 40]
        )

    async def test_failed_brightness_does_not_update_channels(self) -> None:
        c = _mock_coordinator()
        dev = _led_device("R5S2A001602", channels=[50, 50, 50, 50])
        c.data = {"R5S2A001602": dev}
        c.client.async_set_brightness.return_value = False
        await c.async_set_brightness("R5S2A001602", [0, 0, 0, 0])
        assert c.data["R5S2A001602"].manual_channels == [50, 50, 50, 50]

    async def test_brightness_unknown_device_is_ignored(self) -> None:
        c = _mock_coordinator()
        c.data = {}
        await c.async_set_brightness("nonexistent", [10, 20])
        c.client.async_set_brightness.assert_not_awaited()


# ---------------------------------------------------------------------------
# Section 4 — Sensor entity properties
# ---------------------------------------------------------------------------
#
# We create real sensor entity instances backed by a MagicMock coordinator
# that supplies the data dict.  No HA hass/config_entry context is needed
# because _attr_device_info and native_value are pure property reads.
# ---------------------------------------------------------------------------

class TestICV6ModeSensor:

    def _sensor(self, mode: int, host: str = _HOST) -> ICV6ModeSensor:
        dev = _led_device("R5S2A001602", mode=mode)
        coord = _coordinator({"R5S2A001602": dev}, host)
        return ICV6ModeSensor(coord, "R5S2A001602")

    def test_manual_mode(self) -> None:
        assert self._sensor(0).native_value == "Manual"

    def test_auto_schedule_mode(self) -> None:
        assert self._sensor(1).native_value == "Auto Schedule"

    def test_unknown_mode_shows_numeric(self) -> None:
        assert self._sensor(99).native_value == "Unknown (99)"

    def test_translation_key(self) -> None:
        assert self._sensor(0)._attr_translation_key == "icv6_mode"

    def test_unique_id_format(self) -> None:
        uid = self._sensor(0)._attr_unique_id
        assert uid == f"icv6_{_HOST}_R5S2A001602_mode"

    def test_available_when_device_present(self) -> None:
        sensor = self._sensor(0)
        # coordinator.last_update_success is True and device_id is in data
        assert sensor.available is True

    def test_unavailable_when_device_missing(self) -> None:
        dev = _led_device("R5S2A001602")
        coord = _coordinator({"R5S2A001602": dev}, _HOST)
        sensor = ICV6ModeSensor(coord, "R5S2A001602")
        # Remove device from coordinator data
        coord.data = {}
        assert sensor.available is False


class TestICV6ChannelSensor:

    def _sensor(
        self,
        channel: int,
        channels: list[int] | None = None,
        host: str = _HOST,
    ) -> ICV6ChannelSensor:
        ch = channels if channels is not None else [10, 20, 30, 40]
        dev = _led_device("R5S2A001602", channels=ch)
        coord = _coordinator({"R5S2A001602": dev}, host)
        return ICV6ChannelSensor(coord, "R5S2A001602", channel)

    @pytest.mark.parametrize("ch,expected", [(1, 10), (2, 20), (3, 30), (4, 40)])
    def test_channel_value(self, ch: int, expected: int) -> None:
        assert self._sensor(ch, [10, 20, 30, 40]).native_value == expected

    def test_returns_none_when_no_channel_data(self) -> None:
        assert self._sensor(1, []).native_value is None

    def test_returns_none_when_channel_index_out_of_range(self) -> None:
        # 2 channels but asking for channel 4
        assert self._sensor(4, [50, 60]).native_value is None

    @pytest.mark.parametrize("ch", [1, 2, 3, 4, 5, 6, 7, 8])
    def test_translation_key_matches_channel_number(self, ch: int) -> None:
        sensor = self._sensor(ch, list(range(ch)))
        assert sensor._attr_translation_key == f"channel_{ch}"

    def test_unique_id_format(self) -> None:
        uid = self._sensor(2)._attr_unique_id
        assert uid == f"icv6_{_HOST}_R5S2A001602_ch2"

    def test_unit_is_percent(self) -> None:
        assert self._sensor(1)._attr_native_unit_of_measurement == "%"

    def test_full_brightness(self) -> None:
        assert self._sensor(1, [100]).native_value == 100

    def test_zero_brightness(self) -> None:
        assert self._sensor(1, [0]).native_value == 0

    def test_six_channel_device_last_channel(self) -> None:
        dev = _led_device("R6X0B001234", device_type="R6", num_channels=6,
                          channels=[10, 20, 30, 40, 50, 60])
        coord = _coordinator({"R6X0B001234": dev})
        sensor = ICV6ChannelSensor(coord, "R6X0B001234", 6)
        assert sensor.native_value == 60

    def test_extra_attrs_no_schedule_has_manual_key(self) -> None:
        dev = _led_device("R5S2A001602", channels=[50, 50, 50, 50])
        dev.schedule = []
        coord = _coordinator({"R5S2A001602": dev})
        sensor = ICV6ChannelSensor(coord, "R5S2A001602", 1)
        assert sensor.extra_state_attributes == {"manual": 50}

    def test_extra_attrs_contain_per_channel_schedule(self) -> None:
        dev = _led_device("R5S2A001602", channels=[40, 60, 60, 60])
        dev.schedule = [
            {"point": 1, "time": "10:00", "channels": [0, 0, 0, 0]},
            {"point": 2, "time": "12:00", "channels": [40, 60, 60, 60]},
        ]
        coord = _coordinator({"R5S2A001602": dev})
        sensor = ICV6ChannelSensor(coord, "R5S2A001602", 1)
        attrs = sensor.extra_state_attributes
        assert attrs["manual"] == 40
        assert attrs["10:00"] == 0
        assert attrs["12:00"] == 40

    def test_extra_attrs_for_ch2_picks_correct_column(self) -> None:
        dev = _led_device("R5S2A001602", channels=[40, 60, 60, 60])
        dev.schedule = [{"point": 1, "time": "12:00", "channels": [40, 60, 60, 60]}]
        coord = _coordinator({"R5S2A001602": dev})
        sensor = ICV6ChannelSensor(coord, "R5S2A001602", 2)
        attrs = sensor.extra_state_attributes
        assert attrs["12:00"] == 60
        assert attrs["manual"] == 60

    def test_auto_schedule_mode_returns_interpolated_value(self) -> None:
        """In Auto Schedule mode native_value is interpolated, not the manual setpoint."""
        dev = _led_device("R5S2A001602", mode=1, channels=[99, 99, 99, 99])
        dev.schedule = [
            {"point": 1, "time": "10:00", "channels": [0, 0, 0, 0]},
            {"point": 2, "time": "12:00", "channels": [40, 60, 60, 60]},
        ]
        coord = _coordinator({"R5S2A001602": dev})
        sensor = ICV6ChannelSensor(coord, "R5S2A001602", 1)
        # At exactly 11:00 (halfway between 10:00 and 12:00), ch1 = 0 + 0.5*40 = 20
        import datetime
        fixed_time = datetime.datetime(2024, 1, 1, 11, 0)
        from custom_components.maxspect.icv6_api import compute_current_levels
        result = compute_current_levels(dev.schedule, dev.mode, dev.manual_channels, fixed_time)
        assert result[0] == 20

    def test_manual_mode_returns_stored_setpoint(self) -> None:
        """In Manual mode native_value equals the stored channel setpoint."""
        dev = _led_device("R5S2A001602", mode=0, channels=[40, 60, 60, 60])
        dev.schedule = [
            {"point": 1, "time": "10:00", "channels": [0, 0, 0, 0]},
            {"point": 2, "time": "12:00", "channels": [100, 100, 100, 100]},
        ]
        coord = _coordinator({"R5S2A001602": dev})
        sensor = ICV6ChannelSensor(coord, "R5S2A001602", 1)
        # Manual mode ignores schedule — always returns stored manual value
        assert sensor.native_value == 40


class TestComputeCurrentLevels:
    """Unit tests for the schedule-interpolation helper."""

    from custom_components.maxspect.icv6_api import compute_current_levels as _fn

    def _compute(self, schedule, mode, manual, h, m=0):
        import datetime
        from custom_components.maxspect.icv6_api import compute_current_levels
        return compute_current_levels(schedule, mode, manual, datetime.datetime(2024, 1, 1, h, m))

    def test_manual_mode_returns_manual_channels(self) -> None:
        assert self._compute([], mode=0, manual=[50, 60], h=12) == [50, 60]

    def test_empty_schedule_returns_manual(self) -> None:
        assert self._compute([], mode=1, manual=[30, 40], h=12) == [30, 40]

    def test_before_first_point_returns_first(self) -> None:
        sched = [
            {"point": 1, "time": "10:00", "channels": [10, 20]},
            {"point": 2, "time": "12:00", "channels": [40, 60]},
        ]
        assert self._compute(sched, mode=1, manual=[0, 0], h=8) == [10, 20]

    def test_after_last_point_returns_last(self) -> None:
        sched = [
            {"point": 1, "time": "10:00", "channels": [10, 20]},
            {"point": 2, "time": "12:00", "channels": [40, 60]},
        ]
        assert self._compute(sched, mode=1, manual=[0, 0], h=22) == [40, 60]

    def test_exactly_at_first_point(self) -> None:
        sched = [
            {"point": 1, "time": "10:00", "channels": [10, 20]},
            {"point": 2, "time": "12:00", "channels": [40, 60]},
        ]
        assert self._compute(sched, mode=1, manual=[0, 0], h=10) == [10, 20]

    def test_midpoint_interpolation(self) -> None:
        sched = [
            {"point": 1, "time": "10:00", "channels": [0, 0, 0, 0]},
            {"point": 2, "time": "12:00", "channels": [40, 60, 60, 60]},
        ]
        result = self._compute(sched, mode=1, manual=[99, 99, 99, 99], h=11)
        assert result == [20, 30, 30, 30]

    def test_three_quarters_interpolation(self) -> None:
        sched = [
            {"point": 1, "time": "08:00", "channels": [0, 0]},
            {"point": 2, "time": "12:00", "channels": [100, 80]},
        ]
        # 75% of the way from 8:00 to 12:00 = 11:00
        result = self._compute(sched, mode=1, manual=[0, 0], h=11)
        assert result == [75, 60]

    def test_real_device_schedule_at_noon(self) -> None:
        """Matches the user's actual R5 device data at 12:00."""
        sched = [
            {"point": 1, "time": "10:00", "channels": [0, 0, 0, 0]},
            {"point": 2, "time": "11:00", "channels": [16, 32, 32, 16]},
            {"point": 3, "time": "12:00", "channels": [40, 60, 60, 60]},
            {"point": 4, "time": "20:00", "channels": [40, 60, 60, 60]},
            {"point": 5, "time": "21:05", "channels": [0, 0, 0, 0]},
        ]
        result = self._compute(sched, mode=1, manual=[74, 69, 61, 64], h=12)
        assert result == [40, 60, 60, 60]


class TestICV6ScheduleSensor:
    """ICV6ScheduleSensor formats the entire schedule as one string."""

    def _sensor(self, schedule: list[dict] | None = None) -> ICV6ScheduleSensor:
        dev = _led_device("R5S2A001602")
        dev.schedule = schedule if schedule is not None else []
        coord = _coordinator({"R5S2A001602": dev})
        return ICV6ScheduleSensor(coord, "R5S2A001602")

    def test_empty_schedule(self) -> None:
        assert self._sensor([]).native_value == ""

    def test_single_point(self) -> None:
        sched = [{"point": 1, "time": "12:00", "channels": [40, 60, 60, 60]}]
        assert self._sensor(sched).native_value == "12:00 [40/60/60/60]%"

    def test_multiple_points(self) -> None:
        sched = [
            {"point": 1, "time": "10:00", "channels": [0, 0, 0, 0]},
            {"point": 2, "time": "12:00", "channels": [40, 60, 60, 60]},
            {"point": 3, "time": "21:05", "channels": [0, 0, 0, 0]},
        ]
        expected = "10:00 [0/0/0/0]%, 12:00 [40/60/60/60]%, 21:05 [0/0/0/0]%"
        assert self._sensor(sched).native_value == expected

    def test_six_channel_device(self) -> None:
        sched = [{"point": 1, "time": "11:00", "channels": [0, 0, 0, 0, 0, 0]}]
        assert self._sensor(sched).native_value == "11:00 [0/0/0/0/0/0]%"

    def test_real_device_schedule(self) -> None:
        sched = [
            {"point": 1, "time": "10:00", "channels": [0, 0, 0, 0]},
            {"point": 2, "time": "11:00", "channels": [16, 32, 32, 16]},
            {"point": 3, "time": "12:00", "channels": [40, 60, 60, 60]},
            {"point": 4, "time": "20:00", "channels": [40, 60, 60, 60]},
            {"point": 5, "time": "21:05", "channels": [0, 0, 0, 0]},
        ]
        val = self._sensor(sched).native_value
        assert "10:00 [0/0/0/0]%" in val
        assert "12:00 [40/60/60/60]%" in val
        assert "21:05 [0/0/0/0]%" in val

    def test_unique_id_format(self) -> None:
        assert self._sensor()._attr_unique_id == f"icv6_{_HOST}_R5S2A001602_schedule"

    def test_translation_key(self) -> None:
        assert self._sensor()._attr_translation_key == "icv6_schedule"

    def test_entity_category_is_diagnostic(self) -> None:
        from homeassistant.const import EntityCategory
        assert self._sensor()._attr_entity_category == EntityCategory.DIAGNOSTIC

    def test_extra_attrs_contain_point_details(self) -> None:
        sched = [
            {"point": 1, "time": "12:00", "channels": [40, 60, 60, 60]},
            {"point": 2, "time": "21:05", "channels": [0, 0, 0, 0]},
        ]
        attrs = self._sensor(sched).extra_state_attributes
        assert attrs["points"] == 2
        assert attrs["point_1_time"] == "12:00"
        assert attrs["point_1_channels"] == [40, 60, 60, 60]
        assert attrs["point_2_time"] == "21:05"
        assert attrs["point_2_channels"] == [0, 0, 0, 0]

    def test_extra_attrs_empty_schedule(self) -> None:
        attrs = self._sensor([]).extra_state_attributes
        assert attrs["points"] == 0

    def test_format_schedule_static_method(self) -> None:
        sched = [
            {"point": 1, "time": "08:00", "channels": [10, 20]},
            {"point": 2, "time": "20:00", "channels": [50, 60]},
        ]
        result = ICV6ScheduleSensor.format_schedule(sched)
        assert result == "08:00 [10/20]%, 20:00 [50/60]%"


class TestICV6GroupSensor:

    def _sensor(self, group_num: int = 1) -> ICV6GroupSensor:
        dev = _led_device("R5S2A001602")
        dev.group_num = group_num
        coord = _coordinator({"R5S2A001602": dev})
        return ICV6GroupSensor(coord, "R5S2A001602")

    def test_group_num_reflected(self) -> None:
        assert self._sensor(group_num=1).native_value == 1

    def test_group_zero(self) -> None:
        assert self._sensor(group_num=0).native_value == 0

    def test_translation_key(self) -> None:
        assert self._sensor()._attr_translation_key == "icv6_group"

    def test_unique_id_format(self) -> None:
        assert self._sensor()._attr_unique_id == f"icv6_{_HOST}_R5S2A001602_group"

    def test_entity_category_is_diagnostic(self) -> None:
        from homeassistant.const import EntityCategory
        assert self._sensor()._attr_entity_category == EntityCategory.DIAGNOSTIC


class TestICV6ModeSensorAttributes:

    def test_schedule_in_extra_attrs(self) -> None:
        dev = _led_device("R5S2A001602", mode=1)
        dev.schedule = [{"point": 1, "time": "12:00", "channels": [40, 60, 60, 60]}]
        coord = _coordinator({"R5S2A001602": dev})
        sensor = ICV6ModeSensor(coord, "R5S2A001602")
        attrs = sensor.extra_state_attributes
        assert attrs["schedule"] == dev.schedule
        assert attrs["schedule_points"] == 1

    def test_empty_schedule_in_extra_attrs(self) -> None:
        dev = _led_device("R5S2A001602", mode=0)
        dev.schedule = []
        coord = _coordinator({"R5S2A001602": dev})
        sensor = ICV6ModeSensor(coord, "R5S2A001602")
        attrs = sensor.extra_state_attributes
        assert attrs["schedule"] == []
        assert attrs["schedule_points"] == 0


# ---------------------------------------------------------------------------
# Section 5 — Switch entity properties
# ---------------------------------------------------------------------------

class TestICV6PowerSwitch:

    def _switch(
        self,
        is_on: bool = True,
        num_channels: int = 4,
        device_id: str = "R5S2A001602",
        host: str = _HOST,
    ) -> ICV6PowerSwitch:
        if num_channels > 0:
            dev = _led_device(device_id, num_channels=num_channels, is_on=is_on)
        else:
            dev = _pump_device(device_id)
            dev.is_on = is_on
        coord = _coordinator({device_id: dev}, host)
        return ICV6PowerSwitch(coord, device_id)

    def test_is_on_true(self) -> None:
        assert self._switch(is_on=True).is_on is True

    def test_is_on_false(self) -> None:
        assert self._switch(is_on=False).is_on is False

    def test_translation_key_led_is_light_power(self) -> None:
        switch = self._switch(num_channels=4)
        assert switch._attr_translation_key == "light_power"

    def test_translation_key_pump_is_pump_power(self) -> None:
        switch = self._switch(num_channels=0, device_id="G2X0B001234")
        assert switch._attr_translation_key == "pump_power"

    def test_unique_id_format(self) -> None:
        uid = self._switch(device_id="R5S2A001602")._attr_unique_id
        assert uid == f"icv6_{_HOST}_R5S2A001602_power"

    async def test_turn_on_calls_set_power_true(self) -> None:
        dev = _led_device("R5S2A001602", is_on=False)
        coord = _coordinator({"R5S2A001602": dev})
        coord.async_set_power = AsyncMock()
        switch = ICV6PowerSwitch(coord, "R5S2A001602")
        await switch.async_turn_on()
        coord.async_set_power.assert_awaited_once_with("R5S2A001602", True)

    async def test_turn_off_calls_set_power_false(self) -> None:
        dev = _led_device("R5S2A001602", is_on=True)
        coord = _coordinator({"R5S2A001602": dev})
        coord.async_set_power = AsyncMock()
        switch = ICV6PowerSwitch(coord, "R5S2A001602")
        await switch.async_turn_off()
        coord.async_set_power.assert_awaited_once_with("R5S2A001602", False)

    async def test_turn_on_pump_uses_correct_device_id(self) -> None:
        pump = _pump_device("G2X0B001234")
        coord = _coordinator({"G2X0B001234": pump})
        coord.async_set_power = AsyncMock()
        switch = ICV6PowerSwitch(coord, "G2X0B001234")
        await switch.async_turn_on()
        coord.async_set_power.assert_awaited_once_with("G2X0B001234", True)

    def test_reflects_updated_state_after_set(self) -> None:
        """Switch reads from coordinator.data live — reflects optimistic update."""
        dev = _led_device("R5S2A001602", is_on=True)
        coord = _coordinator({"R5S2A001602": dev})
        switch = ICV6PowerSwitch(coord, "R5S2A001602")
        assert switch.is_on is True
        # Simulate coordinator optimistic update
        coord.data["R5S2A001602"].is_on = False
        assert switch.is_on is False


# ---------------------------------------------------------------------------
# Section 6 — ICV6Client connection validation (mocked socket)
# ---------------------------------------------------------------------------

class TestICV6ClientValidation:
    """ICV6Client.async_validate_connection raises ICV6ConnectionError on failure."""

    async def test_raises_on_connection_refused(self) -> None:
        from custom_components.maxspect.icv6_api import ICV6Client

        client = ICV6Client(host="192.0.2.1", port=80)  # TEST-NET — never routable
        with patch(
            "custom_components.maxspect.icv6_api._sync_validate",
            side_effect=OSError("refused"),
        ):
            with pytest.raises(ICV6ConnectionError):
                await client.async_validate_connection()

    async def test_raises_on_timeout(self) -> None:
        import socket
        from custom_components.maxspect.icv6_api import ICV6Client

        client = ICV6Client(host="192.0.2.1", port=80)
        with patch(
            "custom_components.maxspect.icv6_api._sync_validate",
            side_effect=socket.timeout("timed out"),
        ):
            with pytest.raises(ICV6ConnectionError):
                await client.async_validate_connection()

    async def test_succeeds_when_sync_validate_passes(self) -> None:
        from custom_components.maxspect.icv6_api import ICV6Client

        client = ICV6Client(host=_HOST, port=80)
        with patch("custom_components.maxspect.icv6_api._sync_validate", return_value=None):
            # Must not raise
            await client.async_validate_connection()


# ---------------------------------------------------------------------------
# Section 7 — New ICV6 sensor entity tests
# ---------------------------------------------------------------------------

class TestICV6ManualBrightnessSensor:
    """ICV6ManualBrightnessSensor shows the raw manual setpoint, ignoring mode."""

    def _sensor(
        self, channel: int, channels: list[int] | None = None,
        mode: int = 0,
    ) -> ICV6ManualBrightnessSensor:
        ch = channels if channels is not None else [10, 20, 30, 40]
        dev = _led_device("R5S2A001602", mode=mode, channels=ch)
        dev.schedule = [
            {"point": 1, "time": "10:00", "channels": [0, 0, 0, 0]},
            {"point": 2, "time": "12:00", "channels": [100, 100, 100, 100]},
        ]
        coord = _coordinator({"R5S2A001602": dev})
        return ICV6ManualBrightnessSensor(coord, "R5S2A001602", channel)

    def test_returns_manual_value_in_manual_mode(self) -> None:
        assert self._sensor(1, [50, 60, 70, 80], mode=0).native_value == 50

    def test_returns_manual_value_in_auto_mode(self) -> None:
        """Unlike ICV6ChannelSensor, always returns the stored manual setpoint."""
        assert self._sensor(1, [50, 60, 70, 80], mode=1).native_value == 50

    def test_each_channel(self) -> None:
        for ch, expected in [(1, 10), (2, 20), (3, 30), (4, 40)]:
            assert self._sensor(ch).native_value == expected

    def test_returns_none_when_empty_channels(self) -> None:
        assert self._sensor(1, []).native_value is None

    def test_returns_none_when_out_of_range(self) -> None:
        assert self._sensor(4, [50, 60]).native_value is None

    def test_translation_key(self) -> None:
        assert self._sensor(2)._attr_translation_key == "icv6_manual_ch2"

    def test_unique_id_format(self) -> None:
        uid = self._sensor(3)._attr_unique_id
        assert uid == f"icv6_{_HOST}_R5S2A001602_manual_ch3"

    def test_entity_category_is_diagnostic(self) -> None:
        from homeassistant.const import EntityCategory
        assert self._sensor(1)._attr_entity_category == EntityCategory.DIAGNOSTIC

    def test_unit_is_percent(self) -> None:
        assert self._sensor(1)._attr_native_unit_of_measurement == "%"


class TestICV6DeviceIdSensor:
    """ICV6DeviceIdSensor shows the full device ID string."""

    def _sensor(self, device_id: str = "R5S2A001602") -> ICV6DeviceIdSensor:
        dev = _led_device(device_id)
        coord = _coordinator({device_id: dev})
        return ICV6DeviceIdSensor(coord, device_id)

    def test_returns_device_id(self) -> None:
        assert self._sensor("R5S2A001602").native_value == "R5S2A001602"

    def test_translation_key(self) -> None:
        assert self._sensor()._attr_translation_key == "icv6_device_id"

    def test_unique_id_format(self) -> None:
        uid = self._sensor()._attr_unique_id
        assert uid == f"icv6_{_HOST}_R5S2A001602_device_id"

    def test_entity_category_is_diagnostic(self) -> None:
        from homeassistant.const import EntityCategory
        assert self._sensor()._attr_entity_category == EntityCategory.DIAGNOSTIC

    def test_returns_none_when_device_missing(self) -> None:
        dev = _led_device("R5S2A001602")
        coord = _coordinator({"R5S2A001602": dev})
        sensor = ICV6DeviceIdSensor(coord, "R5S2A001602")
        coord.data = {}
        assert sensor.native_value is None


class TestICV6DeviceInfoEnrichment:
    """ICV6Entity.device_info includes serial_number and hw_version from discovery."""

    def test_serial_number_in_device_info(self) -> None:
        dev = _led_device("R5S2A001602")
        dev.serial_number = "A001602"
        dev.hw_version = "S2"
        coord = _coordinator({"R5S2A001602": dev})
        sensor = ICV6ModeSensor(coord, "R5S2A001602")
        info = sensor._attr_device_info
        assert info.get("serial_number") == "A001602"

    def test_hw_version_in_device_info(self) -> None:
        dev = _led_device("R5S2A001602")
        dev.serial_number = "A001602"
        dev.hw_version = "S2"
        coord = _coordinator({"R5S2A001602": dev})
        sensor = ICV6ModeSensor(coord, "R5S2A001602")
        info = sensor._attr_device_info
        assert info.get("hw_version") == "S2"

    def test_missing_serial_not_in_device_info(self) -> None:
        dev = _led_device("R5S2A001602")
        dev.serial_number = ""
        dev.hw_version = ""
        coord = _coordinator({"R5S2A001602": dev})
        sensor = ICV6ModeSensor(coord, "R5S2A001602")
        info = sensor._attr_device_info
        assert "serial_number" not in info
        assert "hw_version" not in info


# ---------------------------------------------------------------------------
# Section 8 — ICV6 bus handshake sequence
# ---------------------------------------------------------------------------

def _make_mock_connection(third_response: bytes | None = None):
    """Return (ctx_mock, conn_mock) where ctx_mock is a context manager mock
    wrapping conn_mock (used to replace _ICV6Connection in tests).

    send_recv side-effects: None (prime), None (search), third_response (device cmd).
    """
    conn = MagicMock()
    conn.send_recv.side_effect = [None, None, third_response]
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=conn)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx, conn


def _minimal_read_response(num_channels: int = 4, proto_cmd: int = 0x0F) -> bytes:
    """Build a minimal valid 0x14 response packet for _sync_read_device_all.

    Args:
        num_channels: Number of LED channels the device reports (drives payload size).
        proto_cmd: Protocol command byte used for this device type (e.g. 0x0F for R5).
    """
    # payload: mode(1) + channels(n) + num_points(1) = n+2 bytes minimum
    payload = bytes([0]) + bytes(num_channels) + bytes([0])
    return _build_new(b"R5S2A001602", 1, proto_cmd, 0x14, payload)


class TestICV6ConnectionHandshake:
    """Prime (0x21) + search (0x22) must be sent on the same connection
    before any device-level command (0x14 / 0x02 / 0x0C)."""

    # ── _sync_read_device_all ────────────────────────────────────────────────

    def test_read_device_all_sends_prime_first(self) -> None:
        ctx, conn = _make_mock_connection(_minimal_read_response())
        with patch("custom_components.maxspect.icv6_api._ICV6Connection", return_value=ctx):
            _sync_read_device_all(_HOST, 12416, "R5S2A001602", 0x0F, 4)
        first_pkt = conn.send_recv.call_args_list[0][0][0]
        assert first_pkt[19] == 0x21, "first send_recv must carry prime (0x21)"

    def test_read_device_all_sends_search_second(self) -> None:
        ctx, conn = _make_mock_connection(_minimal_read_response())
        with patch("custom_components.maxspect.icv6_api._ICV6Connection", return_value=ctx):
            _sync_read_device_all(_HOST, 12416, "R5S2A001602", 0x0F, 4)
        second_pkt = conn.send_recv.call_args_list[1][0][0]
        assert second_pkt[19] == 0x22, "second send_recv must carry search (0x22)"

    def test_read_device_all_sends_0x14_third(self) -> None:
        ctx, conn = _make_mock_connection(_minimal_read_response())
        with patch("custom_components.maxspect.icv6_api._ICV6Connection", return_value=ctx):
            _sync_read_device_all(_HOST, 12416, "R5S2A001602", 0x0F, 4)
        third_pkt = conn.send_recv.call_args_list[2][0][0]
        assert third_pkt[19] == 0x14, "third send_recv must carry device read (0x14)"

    def test_read_device_all_three_calls_on_one_connection(self) -> None:
        ctx, conn = _make_mock_connection(_minimal_read_response())
        with patch("custom_components.maxspect.icv6_api._ICV6Connection", return_value=ctx):
            _sync_read_device_all(_HOST, 12416, "R5S2A001602", 0x0F, 4)
        assert conn.send_recv.call_count == 3, "exactly 3 send_recv calls on one connection"

    # ── _sync_set_power ──────────────────────────────────────────────────────

    def test_set_power_sends_prime_first(self) -> None:
        ctx, conn = _make_mock_connection()
        with patch("custom_components.maxspect.icv6_api._ICV6Connection", return_value=ctx):
            _sync_set_power(_HOST, 12416, "R5S2A001602", 0x0F, True)
        first_pkt = conn.send_recv.call_args_list[0][0][0]
        assert first_pkt[19] == 0x21, "first send_recv must carry prime (0x21)"

    def test_set_power_sends_search_second(self) -> None:
        ctx, conn = _make_mock_connection()
        with patch("custom_components.maxspect.icv6_api._ICV6Connection", return_value=ctx):
            _sync_set_power(_HOST, 12416, "R5S2A001602", 0x0F, True)
        second_pkt = conn.send_recv.call_args_list[1][0][0]
        assert second_pkt[19] == 0x22, "second send_recv must carry search (0x22)"

    def test_set_power_sends_0x02_third(self) -> None:
        ctx, conn = _make_mock_connection()
        with patch("custom_components.maxspect.icv6_api._ICV6Connection", return_value=ctx):
            _sync_set_power(_HOST, 12416, "R5S2A001602", 0x0F, True)
        third_pkt = conn.send_recv.call_args_list[2][0][0]
        assert third_pkt[19] == 0x02, "third send_recv must carry power command (0x02)"

    def test_set_power_three_calls_on_one_connection(self) -> None:
        ctx, conn = _make_mock_connection()
        with patch("custom_components.maxspect.icv6_api._ICV6Connection", return_value=ctx):
            _sync_set_power(_HOST, 12416, "R5S2A001602", 0x0F, True)
        assert conn.send_recv.call_count == 3

    def test_set_power_off_payload_is_zero(self) -> None:
        ctx, conn = _make_mock_connection()
        with patch("custom_components.maxspect.icv6_api._ICV6Connection", return_value=ctx):
            _sync_set_power(_HOST, 12416, "R5S2A001602", 0x0F, False)
        third_pkt = conn.send_recv.call_args_list[2][0][0]
        # payload byte immediately follows sub-command at offset 19; it's at offset 20
        assert third_pkt[20] == 0x00, "power-off payload must be 0x00"

    def test_set_power_on_payload_is_one(self) -> None:
        ctx, conn = _make_mock_connection()
        with patch("custom_components.maxspect.icv6_api._ICV6Connection", return_value=ctx):
            _sync_set_power(_HOST, 12416, "R5S2A001602", 0x0F, True)
        third_pkt = conn.send_recv.call_args_list[2][0][0]
        assert third_pkt[20] == 0x01, "power-on payload must be 0x01"

    # ── _sync_set_brightness ─────────────────────────────────────────────────

    def test_set_brightness_sends_prime_first(self) -> None:
        ctx, conn = _make_mock_connection()
        with patch("custom_components.maxspect.icv6_api._ICV6Connection", return_value=ctx):
            _sync_set_brightness(_HOST, 12416, "R5S2A001602", 0x0F, [50, 60, 70, 80])
        first_pkt = conn.send_recv.call_args_list[0][0][0]
        assert first_pkt[19] == 0x21

    def test_set_brightness_sends_search_second(self) -> None:
        ctx, conn = _make_mock_connection()
        with patch("custom_components.maxspect.icv6_api._ICV6Connection", return_value=ctx):
            _sync_set_brightness(_HOST, 12416, "R5S2A001602", 0x0F, [50, 60, 70, 80])
        second_pkt = conn.send_recv.call_args_list[1][0][0]
        assert second_pkt[19] == 0x22

    def test_set_brightness_sends_0x0c_third(self) -> None:
        ctx, conn = _make_mock_connection()
        with patch("custom_components.maxspect.icv6_api._ICV6Connection", return_value=ctx):
            _sync_set_brightness(_HOST, 12416, "R5S2A001602", 0x0F, [50, 60, 70, 80])
        third_pkt = conn.send_recv.call_args_list[2][0][0]
        assert third_pkt[19] == 0x0C, "third send_recv must carry brightness command (0x0C)"

    def test_set_brightness_three_calls_on_one_connection(self) -> None:
        ctx, conn = _make_mock_connection()
        with patch("custom_components.maxspect.icv6_api._ICV6Connection", return_value=ctx):
            _sync_set_brightness(_HOST, 12416, "R5S2A001602", 0x0F, [50, 60, 70, 80])
        assert conn.send_recv.call_count == 3

    def test_set_brightness_payload_clamped_to_100(self) -> None:
        ctx, conn = _make_mock_connection()
        with patch("custom_components.maxspect.icv6_api._ICV6Connection", return_value=ctx):
            _sync_set_brightness(_HOST, 12416, "R5S2A001602", 0x0F, [0, 50, 100, 150])
        third_pkt = conn.send_recv.call_args_list[2][0][0]
        # channel values start at offset 20
        assert list(third_pkt[20:24]) == [0, 50, 100, 100], "values above 100 must clamp to 100"

    def test_set_brightness_returns_false_on_oserror(self) -> None:
        with patch(
            "custom_components.maxspect.icv6_api._ICV6Connection",
            side_effect=OSError("refused"),
        ):
            result = _sync_set_brightness(_HOST, 12416, "R5S2A001602", 0x0F, [50, 50, 50, 50])
        assert result is False

    def test_set_power_returns_false_on_oserror(self) -> None:
        with patch(
            "custom_components.maxspect.icv6_api._ICV6Connection",
            side_effect=OSError("refused"),
        ):
            result = _sync_set_power(_HOST, 12416, "R5S2A001602", 0x0F, True)
        assert result is False


# ---------------------------------------------------------------------------
# Section 9 — Schedule empty-slot filtering
# ---------------------------------------------------------------------------

def _build_0x14_response(
    num_channels: int,
    proto_cmd: int,
    mode: int,
    manual_channels: list[int],
    points: list[dict],
) -> bytes:
    """Build a synthetic 0x14 TCP response that _sync_read_device_all can parse.

    Args:
        num_channels: Number of LED channels for this device type.
        proto_cmd: Protocol command byte for this device type (e.g. 0x0F for R5).
        mode: Device operating mode byte (0 = manual, 1 = auto schedule, …).
        manual_channels: List of per-channel brightness setpoints (0–100).
        points: Schedule points. Each dict must have keys:
            "point" (int), "hour" (int), "minute" (int), "channels" (list[int]).
    """
    pt_size = 3 + num_channels
    payload = bytes([mode]) + bytes(manual_channels[:num_channels]) + bytes([len(points)])
    for pt in points:
        payload += bytes(
            [pt["point"], pt["hour"], pt["minute"]] + pt["channels"][:num_channels]
        )
    return _build_new(b"R5S2A001602", 1, proto_cmd, 0x14, payload)


class TestICV6ScheduleEmptySlotFiltering:
    """_sync_read_device_all must skip schedule points where time is 00:00
    and all channel values are 0 (empty firmware slots)."""

    def _call_read(
        self,
        response: bytes,
        num_channels: int = 4,
        proto_cmd: int = 0x0F,
    ) -> dict | None:
        ctx, conn = _make_mock_connection(response)
        with patch("custom_components.maxspect.icv6_api._ICV6Connection", return_value=ctx):
            return _sync_read_device_all(_HOST, 12416, "R5S2A001602", proto_cmd, num_channels)

    def test_empty_slot_is_filtered_out(self) -> None:
        """A 00:00 / all-zero slot must not appear in the returned schedule."""
        response = _build_0x14_response(
            num_channels=4, proto_cmd=0x0F, mode=0,
            manual_channels=[50, 60, 70, 80],
            points=[
                {"point": 0, "hour": 0, "minute": 0, "channels": [0, 0, 0, 0]},  # empty
                {"point": 1, "hour": 8, "minute": 30, "channels": [10, 20, 30, 40]},
            ],
        )
        result = self._call_read(response)
        assert result is not None
        assert len(result["schedule"]) == 1
        assert result["schedule"][0]["time"] == "08:30"

    def test_real_point_is_kept(self) -> None:
        """Real (non-empty) points must always be included."""
        response = _build_0x14_response(
            num_channels=4, proto_cmd=0x0F, mode=0,
            manual_channels=[50, 60, 70, 80],
            points=[
                {"point": 1, "hour": 8, "minute": 0, "channels": [10, 20, 30, 40]},
                {"point": 2, "hour": 20, "minute": 0, "channels": [50, 60, 70, 80]},
            ],
        )
        result = self._call_read(response)
        assert result is not None
        assert len(result["schedule"]) == 2

    def test_multiple_empty_slots_all_filtered(self) -> None:
        """Multiple empty slots interspersed with real ones — only real ones survive."""
        response = _build_0x14_response(
            num_channels=4, proto_cmd=0x0F, mode=0,
            manual_channels=[50, 60, 70, 80],
            points=[
                {"point": 0, "hour": 0, "minute": 0, "channels": [0, 0, 0, 0]},  # empty
                {"point": 1, "hour": 8, "minute": 0, "channels": [10, 20, 30, 40]},
                {"point": 2, "hour": 0, "minute": 0, "channels": [0, 0, 0, 0]},  # empty
                {"point": 3, "hour": 20, "minute": 0, "channels": [50, 60, 70, 80]},
            ],
        )
        result = self._call_read(response)
        assert result is not None
        assert len(result["schedule"]) == 2
        assert result["schedule"][0]["time"] == "08:00"
        assert result["schedule"][1]["time"] == "20:00"

    def test_all_empty_slots_returns_empty_schedule(self) -> None:
        """When every slot is empty the schedule list must be empty."""
        response = _build_0x14_response(
            num_channels=4, proto_cmd=0x0F, mode=0,
            manual_channels=[50, 60, 70, 80],
            points=[
                {"point": 0, "hour": 0, "minute": 0, "channels": [0, 0, 0, 0]},
                {"point": 0, "hour": 0, "minute": 0, "channels": [0, 0, 0, 0]},
            ],
        )
        result = self._call_read(response)
        assert result is not None
        assert result["schedule"] == []

    def test_midnight_with_nonzero_channels_is_kept(self) -> None:
        """A point at 00:00 with at least one non-zero channel is a real point."""
        response = _build_0x14_response(
            num_channels=4, proto_cmd=0x0F, mode=0,
            manual_channels=[50, 60, 70, 80],
            points=[
                {"point": 1, "hour": 0, "minute": 0, "channels": [0, 0, 0, 1]},
            ],
        )
        result = self._call_read(response)
        assert result is not None
        assert len(result["schedule"]) == 1
        assert result["schedule"][0]["time"] == "00:00"

    def test_nonzero_time_with_zero_channels_is_kept(self) -> None:
        """A point with a non-midnight time but all-zero channels is a real blackout."""
        response = _build_0x14_response(
            num_channels=4, proto_cmd=0x0F, mode=0,
            manual_channels=[50, 60, 70, 80],
            points=[
                {"point": 1, "hour": 21, "minute": 5, "channels": [0, 0, 0, 0]},
            ],
        )
        result = self._call_read(response)
        assert result is not None
        assert len(result["schedule"]) == 1
        assert result["schedule"][0]["time"] == "21:05"

    def test_schedule_point_fields_are_correct(self) -> None:
        """Each returned point must have 'point', 'time', and 'channels' keys."""
        response = _build_0x14_response(
            num_channels=4, proto_cmd=0x0F, mode=0,
            manual_channels=[50, 60, 70, 80],
            points=[
                {"point": 2, "hour": 12, "minute": 30, "channels": [40, 60, 60, 60]},
            ],
        )
        result = self._call_read(response)
        assert result is not None
        pt = result["schedule"][0]
        assert pt["point"] == 2
        assert pt["time"] == "12:30"
        assert pt["channels"] == [40, 60, 60, 60]

    def test_manual_channels_and_mode_parsed_correctly(self) -> None:
        """Mode and manual channel values must survive parsing unchanged."""
        response = _build_0x14_response(
            num_channels=4, proto_cmd=0x0F, mode=1,
            manual_channels=[25, 50, 75, 100],
            points=[],
        )
        result = self._call_read(response)
        assert result is not None
        assert result["mode"] == 1
        assert result["manual_channels"] == [25, 50, 75, 100]
