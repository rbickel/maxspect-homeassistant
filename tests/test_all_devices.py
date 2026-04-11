"""Comprehensive tests for all 6 Maxspect device families.

Each device type is exercised through a MockCoordinator that faithfully
mirrors the real MaxspectCoordinator's core methods (async_seed_from_cloud,
async_set_power, _on_device_push, _async_update_data) without requiring a
running Home Assistant instance.

Test groups per device:
  1. Cloud seeding    — async_seed_from_cloud processes the cloud payload
  2. Power commands   — async_set_power sends the right attr/value and does
                        the optimistic state update (including state.mode)
  3. Sensor values    — mode names and channel readings derived from state
  4. LAN push gating  — non-Gyre pushes are silently dropped; Gyre accepted
  5. Periodic refresh — _async_update_data calls cloud seed for non-Gyre only

Payloads are synthetic but follow the actual Gizwits attr names from the
model JSON files.  Update individual payload constants with real captured
data once a physical device is available.
"""

from __future__ import annotations

import struct
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.maxspect.api import (
    MaxspectDeviceState,
    _parse_compact_telemetry,
    _parse_state_notify,
)
from custom_components.maxspect.const import (
    DEVICE_CONTROL,
    DEVICE_TYPE_AQUARIUM_20,
    DEVICE_TYPE_AQUARIUM_SYS,
    DEVICE_TYPE_GYRE,
    DEVICE_TYPE_LED_6CH,
    DEVICE_TYPE_LED_8CH,
    DEVICE_TYPE_LED_E8,
    LED_6CH_MODE_NAMES,
    LED_8CH_MODE_NAMES,
    AQUARIUM_20_MODE_NAMES,
    MODE_OFF,
    MODE_ON,
    MODE_WATER_FLOW,
)

# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------

_WRITE_COOLDOWN = 8.0  # must match coordinator.py


def _make_bak24_hex(
    mode: int = MODE_ON,
    ch1_rpm: int = 1500,
    ch1_v_x100: int = 2437,
    ch1_w: int = 72,
    ch2_rpm: int = 1200,
    ch2_v_x100: int = 2360,
    ch2_w: int = 65,
) -> str:
    """Build a compact-telemetry hex string (Gyre Bak24 cloud attribute).

    This is the hex-encoded form of the 25-byte payload that
    _parse_compact_telemetry expects.
    """
    payload = bytearray(25)
    payload[0] = mode
    struct.pack_into(">H", payload, 2, ch1_rpm)
    struct.pack_into(">H", payload, 4, ch1_v_x100)
    payload[7] = ch1_w
    struct.pack_into(">H", payload, 11, ch2_rpm)
    struct.pack_into(">H", payload, 13, ch2_v_x100)
    payload[16] = ch2_w
    return bytes(payload).hex()


def _make_time_hex(
    power: int = 1,
    year: int = 26,
    month: int = 4,
    day: int = 11,
    hour: int = 14,
    minute: int = 30,
    second: int = 0,
) -> str:
    """Build a state-notify hex string (Gyre Time cloud attribute)."""
    return bytes([power, year, month, day, hour, minute, second]).hex()


# ---------------------------------------------------------------------------
# Sample cloud payloads — one per device type
#
# These are synthetic payloads built from the Gizwits model JSON attr names.
# Replace with real captured payloads once physical devices are available.
# Capture command (from coordinator.py debug logs):
#   "Cloud seed for <type> (did=…): received N attrs: {…}"
# ---------------------------------------------------------------------------

#: Gyre XF330CE (cd01d1f3…) — pump running in MODE_ON (5)
GYRE_CLOUD_PAYLOAD_ON: dict = {
    "attr": {
        "Mode": MODE_ON,                        # 5 = on
        "Bak24": _make_bak24_hex(
            mode=MODE_ON, ch1_rpm=1500, ch1_v_x100=2437, ch1_w=72,
            ch2_rpm=1200, ch2_v_x100=2360, ch2_w=65,
        ),
        "Time": _make_time_hex(power=1, year=26, month=4, day=11, hour=14, minute=30),
        "Time_Feed": 10,
        "Model_A": 0,   # XF 330CE
        "Model_B": 0,   # XF 330CE
        "Wash": 7,
    }
}

#: Gyre XF330CE — pump off (MODE_OFF = 3)
GYRE_CLOUD_PAYLOAD_OFF: dict = {
    "attr": {
        "Mode": MODE_OFF,                       # 3 = off
        "Bak24": _make_bak24_hex(
            mode=MODE_OFF, ch1_rpm=0, ch1_v_x100=0, ch1_w=0,
            ch2_rpm=0, ch2_v_x100=0, ch2_w=0,
        ),
        "Time": _make_time_hex(power=1, year=26, month=4, day=11, hour=14, minute=45),
        "Time_Feed": 10,
        "Model_A": 0,
        "Model_B": 0,
        "Wash": 7,
    }
}

#: LED L165 / wifi灯 (401dff81…) — 6 channels, manual mode (mode=0)
L165_CLOUD_PAYLOAD_ON: dict = {
    "attr": {
        "mode": 0,          # 0 = Manual
        "channel_1": 75,
        "channel_2": 60,
        "channel_3": 45,
        "channel_4": 30,
        "channel_5": 20,
        "channel_6": 10,
        "special_mode": 0,
    }
}

#: LED L165 — off (mode=3)
L165_CLOUD_PAYLOAD_OFF: dict = {
    "attr": {
        "mode": 3,          # 3 = Off
        "channel_1": 0,
        "channel_2": 0,
        "channel_3": 0,
        "channel_4": 0,
        "channel_5": 0,
        "channel_6": 0,
        "special_mode": 0,
    }
}

#: LED L165 — auto mode (mode=1)
L165_CLOUD_PAYLOAD_AUTO: dict = {
    "attr": {
        "mode": 1,          # 1 = Auto
        "channel_1": 0,
        "channel_2": 0,
        "channel_3": 0,
        "channel_4": 0,
        "channel_5": 0,
        "channel_6": 0,
        "special_mode": 0,
    }
}

#: LED MJ-L265/L290 (5dc78a56…) — 8 channels, manual mode (MODE=0)
L265_CLOUD_PAYLOAD_ON: dict = {
    "attr": {
        "MODE": 0,          # 0 = Manual
        "channel_1": 80,
        "channel_2": 70,
        "channel_3": 60,
        "channel_4": 50,
        "channel_5": 40,
        "channel_6": 30,
        "channel_7": 20,
        "channel_8": 10,
    }
}

#: LED MJ-L265/L290 — off (MODE=2)
L265_CLOUD_PAYLOAD_OFF: dict = {
    "attr": {
        "MODE": 2,          # 2 = Off
        "channel_1": 0,
        "channel_2": 0,
        "channel_3": 0,
        "channel_4": 0,
        "channel_5": 0,
        "channel_6": 0,
        "channel_7": 0,
        "channel_8": 0,
    }
}

#: LED E8 (53a6a71b…) — 8 channels, manual mode (MODE=0)
E8_CLOUD_PAYLOAD_ON: dict = {
    "attr": {
        "MODE": 0,          # 0 = Manual
        "channel_1": 90,
        "channel_2": 80,
        "channel_3": 70,
        "channel_4": 60,
        "channel_5": 50,
        "channel_6": 40,
        "channel_7": 30,
        "channel_8": 20,
        "special_mode": 0,
    }
}

#: LED E8 — off (MODE=2)
E8_CLOUD_PAYLOAD_OFF: dict = {
    "attr": {
        "MODE": 2,          # 2 = Off
        "channel_1": 0,
        "channel_2": 0,
        "channel_3": 0,
        "channel_4": 0,
        "channel_5": 0,
        "channel_6": 0,
        "channel_7": 0,
        "channel_8": 0,
        "special_mode": 0,
    }
}

#: Aquarium 20缸 (254085a8…) — running (Mode=0)
#
# Temperature is stored as a uint16 raw value; the sensor divides by 2.0.
#   raw 50  → 25.0 °C
#   raw 498 → 249.0 °C  (NOT a realistic encoding — update once confirmed)
AQUARIUM_20_CLOUD_PAYLOAD_RUNNING: dict = {
    "attr": {
        "Mode": 0,          # 0 = Running
        "Temperature1": 50, # raw /2.0 → 25.0 °C
        "Temperature2": 49, # raw /2.0 → 24.5 °C
        "Level_Pump": 3,
        "Level_Skimmer": 2,
    }
}

#: Aquarium 20缸 — standby (Mode=1)
AQUARIUM_20_CLOUD_PAYLOAD_STANDBY: dict = {
    "attr": {
        "Mode": 1,          # 1 = Standby
        "Temperature1": 48, # raw /2.0 → 24.0 °C
        "Temperature2": 47, # raw /2.0 → 23.5 °C
        "Level_Pump": 0,
        "Level_Skimmer": 0,
    }
}

#: Aquarium 套缸 (11c81d63…) — on (Switch_All=1)
AQUARIUM_SYS_CLOUD_PAYLOAD_ON: dict = {
    "attr": {
        "Switch_All": 1,    # 1 = on
        "Temp_051": 52,     # uint8, /2.0 → 26.0 °C
        "Temp_052": 51,     # /2.0 → 25.5 °C
    }
}

#: Aquarium 套缸 — off (Switch_All=0)
AQUARIUM_SYS_CLOUD_PAYLOAD_OFF: dict = {
    "attr": {
        "Switch_All": 0,    # 0 = off
        "Temp_051": 50,     # 25.0 °C
        "Temp_052": 49,     # 24.5 °C
    }
}


# ---------------------------------------------------------------------------
# MockCoordinator
#
# Faithfully mirrors MaxspectCoordinator's core methods so tests run without
# a Home Assistant instance.  Update if coordinator.py logic changes.
# ---------------------------------------------------------------------------


class MockCoordinator:
    """Minimal coordinator shim for all device types."""

    def __init__(self, device_type: str, cloud_did: str = "test-did") -> None:
        self.device_type = device_type
        self._cloud_did = cloud_did
        self.client = MagicMock()
        self.client.state = MaxspectDeviceState()
        self.client.connected = True
        self.cloud = AsyncMock()
        self.data: MaxspectDeviceState = self.client.state
        self._notifications: list[MaxspectDeviceState] = []
        self._write_lock_until: float = 0.0
        self._pending_mode: int = MODE_ON

    # ── Infrastructure ──────────────────────────────────────────────────

    def async_set_updated_data(self, state: MaxspectDeviceState) -> None:
        self.data = state
        self._notifications.append(state)

    # ── Mirrors coordinator.async_seed_from_cloud ────────────────────────

    async def async_seed_from_cloud(self) -> None:
        data = await self.cloud.async_get_device_status(did=self._cloud_did)
        attrs = data.get("attr", {})
        if not attrs:
            return

        state = self.client.state

        if self.device_type == DEVICE_TYPE_GYRE:
            bak24 = attrs.get("Bak24")
            if bak24:
                _parse_compact_telemetry(bytes.fromhex(bak24), state)

            time_hex = attrs.get("Time")
            if time_hex:
                _parse_state_notify(bytes.fromhex(time_hex), state)

            for attr_name, field_name in (
                ("Mode", "mode"),
                ("Time_Feed", "feed_duration"),
                ("Model_A", "model_a"),
                ("Model_B", "model_b"),
                ("Wash", "wash_reminder"),
            ):
                val = attrs.get(attr_name)
                if val is not None:
                    setattr(state, field_name, int(val))

            state.is_on = state.mode != MODE_OFF

        else:
            ctrl = DEVICE_CONTROL.get(self.device_type, {})
            mode_attr = ctrl.get("attr", "Mode")
            off_val = ctrl.get("off", 1)
            state.generic_attrs = dict(attrs)
            val = attrs.get(mode_attr)
            if val is not None:
                state.is_on = int(val) != off_val
                state.mode = int(val)

        self.async_set_updated_data(state)

    # ── Mirrors coordinator.async_set_mode (Gyre) ───────────────────────

    async def async_set_mode(self, mode: int) -> None:
        await self.cloud.async_set_mode(mode, did=self._cloud_did)
        self._pending_mode = mode
        self._write_lock_until = time.monotonic() + _WRITE_COOLDOWN
        state = self.client.state
        state.mode = mode
        state.is_on = mode != MODE_OFF
        self.async_set_updated_data(state)

    # ── Mirrors coordinator.async_set_power ─────────────────────────────

    async def async_set_power(self, on: bool) -> None:
        ctrl = DEVICE_CONTROL[self.device_type]
        val = ctrl["on"] if on else ctrl["off"]

        if self.device_type == DEVICE_TYPE_GYRE:
            await self.async_set_mode(val)
            return

        await self.cloud.async_set_attr(ctrl["attr"], val, did=self._cloud_did)
        state = self.client.state
        state.is_on = on
        state.mode = val
        state.generic_attrs[ctrl["attr"]] = val
        self.async_set_updated_data(state)

    # ── Mirrors coordinator._on_device_push ─────────────────────────────

    def _on_device_push(self) -> None:
        if self.device_type != DEVICE_TYPE_GYRE:
            return
        if time.monotonic() < self._write_lock_until:
            state = self.client.state
            if state.mode == self._pending_mode:
                self._write_lock_until = 0.0
            else:
                state.mode = self._pending_mode
                state.is_on = self._pending_mode != MODE_OFF
                return
        self.async_set_updated_data(self.client.state)

    # ── Mirrors coordinator._async_update_data ───────────────────────────

    async def _async_update_data(self) -> MaxspectDeviceState:
        if not self.client.connected:
            await self.client.async_connect()
        if self.device_type != DEVICE_TYPE_GYRE:
            await self.async_seed_from_cloud()
        return self.client.state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _coordinator(device_type: str) -> MockCoordinator:
    return MockCoordinator(device_type)


# ---------------------------------------------------------------------------
# Gyre XF330CE
# ---------------------------------------------------------------------------


class TestGyreXF330CE:

    async def test_seed_on_sets_is_on_true(self) -> None:
        c = _coordinator(DEVICE_TYPE_GYRE)
        c.cloud.async_get_device_status.return_value = GYRE_CLOUD_PAYLOAD_ON
        await c.async_seed_from_cloud()
        assert c.data.is_on is True

    async def test_seed_off_sets_is_on_false(self) -> None:
        c = _coordinator(DEVICE_TYPE_GYRE)
        c.cloud.async_get_device_status.return_value = GYRE_CLOUD_PAYLOAD_OFF
        await c.async_seed_from_cloud()
        assert c.data.is_on is False

    async def test_seed_parses_mode_from_scalar_attr(self) -> None:
        c = _coordinator(DEVICE_TYPE_GYRE)
        c.cloud.async_get_device_status.return_value = GYRE_CLOUD_PAYLOAD_ON
        await c.async_seed_from_cloud()
        assert c.data.mode == MODE_ON

    async def test_seed_parses_compact_telemetry_ch1_rpm(self) -> None:
        c = _coordinator(DEVICE_TYPE_GYRE)
        c.cloud.async_get_device_status.return_value = GYRE_CLOUD_PAYLOAD_ON
        await c.async_seed_from_cloud()
        assert c.data.ch1_rpm == 1500

    async def test_seed_parses_compact_telemetry_ch2_rpm(self) -> None:
        c = _coordinator(DEVICE_TYPE_GYRE)
        c.cloud.async_get_device_status.return_value = GYRE_CLOUD_PAYLOAD_ON
        await c.async_seed_from_cloud()
        assert c.data.ch2_rpm == 1200

    async def test_seed_parses_compact_telemetry_ch1_voltage(self) -> None:
        c = _coordinator(DEVICE_TYPE_GYRE)
        c.cloud.async_get_device_status.return_value = GYRE_CLOUD_PAYLOAD_ON
        await c.async_seed_from_cloud()
        assert abs(c.data.ch1_voltage - 24.37) < 0.001

    async def test_seed_parses_compact_telemetry_ch2_voltage(self) -> None:
        c = _coordinator(DEVICE_TYPE_GYRE)
        c.cloud.async_get_device_status.return_value = GYRE_CLOUD_PAYLOAD_ON
        await c.async_seed_from_cloud()
        assert abs(c.data.ch2_voltage - 23.60) < 0.001

    async def test_seed_parses_compact_telemetry_ch1_power(self) -> None:
        c = _coordinator(DEVICE_TYPE_GYRE)
        c.cloud.async_get_device_status.return_value = GYRE_CLOUD_PAYLOAD_ON
        await c.async_seed_from_cloud()
        assert c.data.ch1_power == 72

    async def test_seed_parses_compact_telemetry_ch2_power(self) -> None:
        c = _coordinator(DEVICE_TYPE_GYRE)
        c.cloud.async_get_device_status.return_value = GYRE_CLOUD_PAYLOAD_ON
        await c.async_seed_from_cloud()
        assert c.data.ch2_power == 65

    async def test_seed_parses_timestamp(self) -> None:
        c = _coordinator(DEVICE_TYPE_GYRE)
        c.cloud.async_get_device_status.return_value = GYRE_CLOUD_PAYLOAD_ON
        await c.async_seed_from_cloud()
        assert c.data.timestamp == "2026-04-11 14:30:00"

    async def test_seed_parses_config_feed_duration(self) -> None:
        c = _coordinator(DEVICE_TYPE_GYRE)
        c.cloud.async_get_device_status.return_value = GYRE_CLOUD_PAYLOAD_ON
        await c.async_seed_from_cloud()
        assert c.data.feed_duration == 10

    async def test_seed_parses_config_model_a(self) -> None:
        c = _coordinator(DEVICE_TYPE_GYRE)
        c.cloud.async_get_device_status.return_value = GYRE_CLOUD_PAYLOAD_ON
        await c.async_seed_from_cloud()
        assert c.data.model_a == 0  # XF 330CE

    async def test_seed_parses_config_wash_reminder(self) -> None:
        c = _coordinator(DEVICE_TYPE_GYRE)
        c.cloud.async_get_device_status.return_value = GYRE_CLOUD_PAYLOAD_ON
        await c.async_seed_from_cloud()
        assert c.data.wash_reminder == 7

    async def test_power_on_sends_mode_on(self) -> None:
        c = _coordinator(DEVICE_TYPE_GYRE)
        await c.async_set_power(True)
        c.cloud.async_set_mode.assert_awaited_once_with(MODE_ON, did="test-did")

    async def test_power_off_sends_mode_off(self) -> None:
        c = _coordinator(DEVICE_TYPE_GYRE)
        await c.async_set_power(False)
        c.cloud.async_set_mode.assert_awaited_once_with(MODE_OFF, did="test-did")

    async def test_power_on_sets_is_on_optimistically(self) -> None:
        c = _coordinator(DEVICE_TYPE_GYRE)
        c.client.state.is_on = False
        await c.async_set_power(True)
        assert c.data.is_on is True

    async def test_power_off_sets_is_on_false_optimistically(self) -> None:
        c = _coordinator(DEVICE_TYPE_GYRE)
        c.client.state.is_on = True
        await c.async_set_power(False)
        assert c.data.is_on is False

    async def test_lan_push_accepted_for_gyre(self) -> None:
        c = _coordinator(DEVICE_TYPE_GYRE)
        n_before = len(c._notifications)
        c._on_device_push()
        assert len(c._notifications) == n_before + 1

    async def test_update_data_does_not_call_cloud_for_gyre(self) -> None:
        c = _coordinator(DEVICE_TYPE_GYRE)
        await c._async_update_data()
        c.cloud.async_get_device_status.assert_not_awaited()


# ---------------------------------------------------------------------------
# LED L165 (6-channel)
# ---------------------------------------------------------------------------


class TestLedL165:

    async def test_seed_on_sets_is_on_true(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_6CH)
        c.cloud.async_get_device_status.return_value = L165_CLOUD_PAYLOAD_ON
        await c.async_seed_from_cloud()
        assert c.data.is_on is True

    async def test_seed_off_sets_is_on_false(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_6CH)
        c.cloud.async_get_device_status.return_value = L165_CLOUD_PAYLOAD_OFF
        await c.async_seed_from_cloud()
        assert c.data.is_on is False

    async def test_seed_populates_generic_attrs(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_6CH)
        c.cloud.async_get_device_status.return_value = L165_CLOUD_PAYLOAD_ON
        await c.async_seed_from_cloud()
        assert c.data.generic_attrs["channel_1"] == 75
        assert c.data.generic_attrs["channel_6"] == 10

    async def test_seed_sets_mode_from_mode_attr(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_6CH)
        c.cloud.async_get_device_status.return_value = L165_CLOUD_PAYLOAD_ON
        await c.async_seed_from_cloud()
        assert c.data.mode == 0  # Manual

    async def test_seed_mode_off_value_is_three(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_6CH)
        c.cloud.async_get_device_status.return_value = L165_CLOUD_PAYLOAD_OFF
        await c.async_seed_from_cloud()
        assert c.data.mode == 3

    async def test_mode_sensor_value_manual(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_6CH)
        c.cloud.async_get_device_status.return_value = L165_CLOUD_PAYLOAD_ON
        await c.async_seed_from_cloud()
        assert LED_6CH_MODE_NAMES.get(c.data.mode) == "Manual"

    async def test_mode_sensor_value_auto(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_6CH)
        c.cloud.async_get_device_status.return_value = L165_CLOUD_PAYLOAD_AUTO
        await c.async_seed_from_cloud()
        assert LED_6CH_MODE_NAMES.get(c.data.mode) == "Auto"

    async def test_mode_sensor_value_off(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_6CH)
        c.cloud.async_get_device_status.return_value = L165_CLOUD_PAYLOAD_OFF
        await c.async_seed_from_cloud()
        assert LED_6CH_MODE_NAMES.get(c.data.mode) == "Off"

    @pytest.mark.parametrize("ch,expected", [
        (1, 75), (2, 60), (3, 45), (4, 30), (5, 20), (6, 10),
    ])
    async def test_channel_value_after_seed(self, ch: int, expected: int) -> None:
        c = _coordinator(DEVICE_TYPE_LED_6CH)
        c.cloud.async_get_device_status.return_value = L165_CLOUD_PAYLOAD_ON
        await c.async_seed_from_cloud()
        assert c.data.generic_attrs.get(f"channel_{ch}") == expected

    async def test_power_on_sends_mode_attr(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_6CH)
        await c.async_set_power(True)
        c.cloud.async_set_attr.assert_awaited_once_with("mode", 0, did="test-did")

    async def test_power_off_sends_mode_off_value(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_6CH)
        await c.async_set_power(False)
        c.cloud.async_set_attr.assert_awaited_once_with("mode", 3, did="test-did")

    async def test_power_on_sets_is_on_true(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_6CH)
        await c.async_set_power(True)
        assert c.data.is_on is True

    async def test_power_off_sets_is_on_false(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_6CH)
        await c.async_set_power(False)
        assert c.data.is_on is False

    async def test_power_on_sets_state_mode(self) -> None:
        """Regression: async_set_power must update state.mode (not only generic_attrs)."""
        c = _coordinator(DEVICE_TYPE_LED_6CH)
        await c.async_set_power(True)
        assert c.data.mode == 0  # on value

    async def test_power_off_sets_state_mode(self) -> None:
        """Regression: mode sensor must read 'Off' after power toggle."""
        c = _coordinator(DEVICE_TYPE_LED_6CH)
        await c.async_set_power(False)
        assert c.data.mode == 3  # off value
        assert LED_6CH_MODE_NAMES.get(c.data.mode) == "Off"

    async def test_power_off_updates_generic_attrs(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_6CH)
        await c.async_set_power(False)
        assert c.data.generic_attrs["mode"] == 3

    async def test_lan_push_ignored(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_6CH)
        n_before = len(c._notifications)
        c._on_device_push()
        assert len(c._notifications) == n_before  # no notification sent

    async def test_update_data_triggers_cloud_seed(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_6CH)
        c.cloud.async_get_device_status.return_value = L165_CLOUD_PAYLOAD_ON
        await c._async_update_data()
        c.cloud.async_get_device_status.assert_awaited_once()

    async def test_update_data_refreshes_channel_values(self) -> None:
        """State must reflect fresh cloud data after each _async_update_data call."""
        c = _coordinator(DEVICE_TYPE_LED_6CH)
        c.cloud.async_get_device_status.return_value = L165_CLOUD_PAYLOAD_ON
        await c._async_update_data()
        assert c.data.generic_attrs.get("channel_1") == 75


# ---------------------------------------------------------------------------
# LED MJ-L265/L290 (8-channel)
# ---------------------------------------------------------------------------


class TestLedL265L290:

    async def test_seed_on_sets_is_on_true(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_8CH)
        c.cloud.async_get_device_status.return_value = L265_CLOUD_PAYLOAD_ON
        await c.async_seed_from_cloud()
        assert c.data.is_on is True

    async def test_seed_off_sets_is_on_false(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_8CH)
        c.cloud.async_get_device_status.return_value = L265_CLOUD_PAYLOAD_OFF
        await c.async_seed_from_cloud()
        assert c.data.is_on is False

    async def test_off_value_is_two(self) -> None:
        """L265/L290 uses MODE=2 for off, not 3."""
        c = _coordinator(DEVICE_TYPE_LED_8CH)
        c.cloud.async_get_device_status.return_value = L265_CLOUD_PAYLOAD_OFF
        await c.async_seed_from_cloud()
        assert c.data.mode == 2

    async def test_mode_attr_is_uppercase_MODE(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_8CH)
        await c.async_set_power(False)
        attr_used = c.cloud.async_set_attr.call_args[0][0]
        assert attr_used == "MODE"

    async def test_mode_sensor_manual_after_on_seed(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_8CH)
        c.cloud.async_get_device_status.return_value = L265_CLOUD_PAYLOAD_ON
        await c.async_seed_from_cloud()
        assert LED_8CH_MODE_NAMES.get(c.data.mode) == "Manual"

    @pytest.mark.parametrize("ch,expected", [
        (1, 80), (2, 70), (3, 60), (4, 50), (5, 40), (6, 30), (7, 20), (8, 10),
    ])
    async def test_channel_value_after_seed(self, ch: int, expected: int) -> None:
        c = _coordinator(DEVICE_TYPE_LED_8CH)
        c.cloud.async_get_device_status.return_value = L265_CLOUD_PAYLOAD_ON
        await c.async_seed_from_cloud()
        assert c.data.generic_attrs.get(f"channel_{ch}") == expected

    async def test_power_on_sends_mode_zero(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_8CH)
        await c.async_set_power(True)
        c.cloud.async_set_attr.assert_awaited_once_with("MODE", 0, did="test-did")

    async def test_power_off_sends_mode_two(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_8CH)
        await c.async_set_power(False)
        c.cloud.async_set_attr.assert_awaited_once_with("MODE", 2, did="test-did")

    async def test_power_off_updates_state_mode(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_8CH)
        await c.async_set_power(False)
        assert c.data.mode == 2
        assert c.data.is_on is False

    async def test_lan_push_ignored(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_8CH)
        n_before = len(c._notifications)
        c._on_device_push()
        assert len(c._notifications) == n_before

    async def test_update_data_calls_cloud_seed(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_8CH)
        c.cloud.async_get_device_status.return_value = L265_CLOUD_PAYLOAD_ON
        await c._async_update_data()
        c.cloud.async_get_device_status.assert_awaited_once()


# ---------------------------------------------------------------------------
# LED E8 (8-channel)
# ---------------------------------------------------------------------------


class TestLedE8:

    async def test_seed_on_sets_is_on_true(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_E8)
        c.cloud.async_get_device_status.return_value = E8_CLOUD_PAYLOAD_ON
        await c.async_seed_from_cloud()
        assert c.data.is_on is True

    async def test_seed_off_sets_is_on_false(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_E8)
        c.cloud.async_get_device_status.return_value = E8_CLOUD_PAYLOAD_OFF
        await c.async_seed_from_cloud()
        assert c.data.is_on is False

    async def test_off_value_is_two(self) -> None:
        """E8 shares the same off value (MODE=2) as L265/L290."""
        c = _coordinator(DEVICE_TYPE_LED_E8)
        c.cloud.async_get_device_status.return_value = E8_CLOUD_PAYLOAD_OFF
        await c.async_seed_from_cloud()
        assert c.data.mode == 2

    async def test_mode_attr_is_uppercase_MODE(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_E8)
        await c.async_set_power(False)
        attr_used = c.cloud.async_set_attr.call_args[0][0]
        assert attr_used == "MODE"

    @pytest.mark.parametrize("ch,expected", [
        (1, 90), (2, 80), (3, 70), (4, 60), (5, 50), (6, 40), (7, 30), (8, 20),
    ])
    async def test_channel_value_after_seed(self, ch: int, expected: int) -> None:
        c = _coordinator(DEVICE_TYPE_LED_E8)
        c.cloud.async_get_device_status.return_value = E8_CLOUD_PAYLOAD_ON
        await c.async_seed_from_cloud()
        assert c.data.generic_attrs.get(f"channel_{ch}") == expected

    async def test_power_on_sends_mode_zero(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_E8)
        await c.async_set_power(True)
        c.cloud.async_set_attr.assert_awaited_once_with("MODE", 0, did="test-did")

    async def test_power_off_sends_mode_two(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_E8)
        await c.async_set_power(False)
        c.cloud.async_set_attr.assert_awaited_once_with("MODE", 2, did="test-did")

    async def test_power_off_updates_state_mode(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_E8)
        await c.async_set_power(False)
        assert c.data.mode == 2
        assert c.data.is_on is False

    async def test_lan_push_ignored(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_E8)
        n_before = len(c._notifications)
        c._on_device_push()
        assert len(c._notifications) == n_before

    async def test_update_data_calls_cloud_seed(self) -> None:
        c = _coordinator(DEVICE_TYPE_LED_E8)
        c.cloud.async_get_device_status.return_value = E8_CLOUD_PAYLOAD_ON
        await c._async_update_data()
        c.cloud.async_get_device_status.assert_awaited_once()


# ---------------------------------------------------------------------------
# Aquarium 20缸
# ---------------------------------------------------------------------------


class TestAquarium20:

    async def test_seed_running_sets_is_on_true(self) -> None:
        c = _coordinator(DEVICE_TYPE_AQUARIUM_20)
        c.cloud.async_get_device_status.return_value = AQUARIUM_20_CLOUD_PAYLOAD_RUNNING
        await c.async_seed_from_cloud()
        assert c.data.is_on is True

    async def test_seed_standby_sets_is_on_false(self) -> None:
        c = _coordinator(DEVICE_TYPE_AQUARIUM_20)
        c.cloud.async_get_device_status.return_value = AQUARIUM_20_CLOUD_PAYLOAD_STANDBY
        await c.async_seed_from_cloud()
        assert c.data.is_on is False

    async def test_seed_mode_running_is_zero(self) -> None:
        c = _coordinator(DEVICE_TYPE_AQUARIUM_20)
        c.cloud.async_get_device_status.return_value = AQUARIUM_20_CLOUD_PAYLOAD_RUNNING
        await c.async_seed_from_cloud()
        assert c.data.mode == 0

    async def test_mode_sensor_value_running(self) -> None:
        c = _coordinator(DEVICE_TYPE_AQUARIUM_20)
        c.cloud.async_get_device_status.return_value = AQUARIUM_20_CLOUD_PAYLOAD_RUNNING
        await c.async_seed_from_cloud()
        assert AQUARIUM_20_MODE_NAMES.get(c.data.mode) == "Running"

    async def test_mode_sensor_value_standby(self) -> None:
        c = _coordinator(DEVICE_TYPE_AQUARIUM_20)
        c.cloud.async_get_device_status.return_value = AQUARIUM_20_CLOUD_PAYLOAD_STANDBY
        await c.async_seed_from_cloud()
        assert AQUARIUM_20_MODE_NAMES.get(c.data.mode) == "Standby"

    async def test_seed_populates_temperature_attrs(self) -> None:
        c = _coordinator(DEVICE_TYPE_AQUARIUM_20)
        c.cloud.async_get_device_status.return_value = AQUARIUM_20_CLOUD_PAYLOAD_RUNNING
        await c.async_seed_from_cloud()
        assert c.data.generic_attrs["Temperature1"] == 50
        assert c.data.generic_attrs["Temperature2"] == 49

    async def test_temperature_sensor_value_celsius(self) -> None:
        """Temperature1=50 raw → 25.0 °C (sensor logic: value / 2.0)."""
        c = _coordinator(DEVICE_TYPE_AQUARIUM_20)
        c.cloud.async_get_device_status.return_value = AQUARIUM_20_CLOUD_PAYLOAD_RUNNING
        await c.async_seed_from_cloud()
        assert round(int(c.data.generic_attrs["Temperature1"]) / 2.0, 1) == 25.0
        assert round(int(c.data.generic_attrs["Temperature2"]) / 2.0, 1) == 24.5

    async def test_seed_populates_pump_level(self) -> None:
        c = _coordinator(DEVICE_TYPE_AQUARIUM_20)
        c.cloud.async_get_device_status.return_value = AQUARIUM_20_CLOUD_PAYLOAD_RUNNING
        await c.async_seed_from_cloud()
        assert c.data.generic_attrs["Level_Pump"] == 3
        assert c.data.generic_attrs["Level_Skimmer"] == 2

    async def test_power_on_sends_mode_zero(self) -> None:
        c = _coordinator(DEVICE_TYPE_AQUARIUM_20)
        await c.async_set_power(True)
        c.cloud.async_set_attr.assert_awaited_once_with("Mode", 0, did="test-did")

    async def test_power_off_sends_mode_one(self) -> None:
        """Aquarium 20缸 uses Mode=1 for standby/off."""
        c = _coordinator(DEVICE_TYPE_AQUARIUM_20)
        await c.async_set_power(False)
        c.cloud.async_set_attr.assert_awaited_once_with("Mode", 1, did="test-did")

    async def test_power_off_updates_state_mode(self) -> None:
        c = _coordinator(DEVICE_TYPE_AQUARIUM_20)
        await c.async_set_power(False)
        assert c.data.mode == 1
        assert c.data.is_on is False

    async def test_lan_push_ignored(self) -> None:
        c = _coordinator(DEVICE_TYPE_AQUARIUM_20)
        n_before = len(c._notifications)
        c._on_device_push()
        assert len(c._notifications) == n_before

    async def test_update_data_calls_cloud_seed(self) -> None:
        c = _coordinator(DEVICE_TYPE_AQUARIUM_20)
        c.cloud.async_get_device_status.return_value = AQUARIUM_20_CLOUD_PAYLOAD_RUNNING
        await c._async_update_data()
        c.cloud.async_get_device_status.assert_awaited_once()


# ---------------------------------------------------------------------------
# Aquarium 套缸 (integrated system)
# ---------------------------------------------------------------------------


class TestAquariumSys:

    async def test_seed_on_sets_is_on_true(self) -> None:
        c = _coordinator(DEVICE_TYPE_AQUARIUM_SYS)
        c.cloud.async_get_device_status.return_value = AQUARIUM_SYS_CLOUD_PAYLOAD_ON
        await c.async_seed_from_cloud()
        assert c.data.is_on is True

    async def test_seed_off_sets_is_on_false(self) -> None:
        c = _coordinator(DEVICE_TYPE_AQUARIUM_SYS)
        c.cloud.async_get_device_status.return_value = AQUARIUM_SYS_CLOUD_PAYLOAD_OFF
        await c.async_seed_from_cloud()
        assert c.data.is_on is False

    async def test_control_attr_is_switch_all(self) -> None:
        c = _coordinator(DEVICE_TYPE_AQUARIUM_SYS)
        await c.async_set_power(True)
        attr_used = c.cloud.async_set_attr.call_args[0][0]
        assert attr_used == "Switch_All"

    async def test_power_on_sends_switch_all_one(self) -> None:
        c = _coordinator(DEVICE_TYPE_AQUARIUM_SYS)
        await c.async_set_power(True)
        c.cloud.async_set_attr.assert_awaited_once_with("Switch_All", 1, did="test-did")

    async def test_power_off_sends_switch_all_zero(self) -> None:
        c = _coordinator(DEVICE_TYPE_AQUARIUM_SYS)
        await c.async_set_power(False)
        c.cloud.async_set_attr.assert_awaited_once_with("Switch_All", 0, did="test-did")

    async def test_power_off_updates_state_mode(self) -> None:
        c = _coordinator(DEVICE_TYPE_AQUARIUM_SYS)
        await c.async_set_power(False)
        assert c.data.mode == 0   # off value for Switch_All
        assert c.data.is_on is False

    async def test_seed_populates_temp_051(self) -> None:
        c = _coordinator(DEVICE_TYPE_AQUARIUM_SYS)
        c.cloud.async_get_device_status.return_value = AQUARIUM_SYS_CLOUD_PAYLOAD_ON
        await c.async_seed_from_cloud()
        assert c.data.generic_attrs["Temp_051"] == 52

    async def test_seed_populates_temp_052(self) -> None:
        c = _coordinator(DEVICE_TYPE_AQUARIUM_SYS)
        c.cloud.async_get_device_status.return_value = AQUARIUM_SYS_CLOUD_PAYLOAD_ON
        await c.async_seed_from_cloud()
        assert c.data.generic_attrs["Temp_052"] == 51

    async def test_temperature_sensor_value_celsius(self) -> None:
        """Temp_051=52 raw → 26.0 °C (sensor divides by 2.0)."""
        c = _coordinator(DEVICE_TYPE_AQUARIUM_SYS)
        c.cloud.async_get_device_status.return_value = AQUARIUM_SYS_CLOUD_PAYLOAD_ON
        await c.async_seed_from_cloud()
        assert round(int(c.data.generic_attrs["Temp_051"]) / 2.0, 1) == 26.0
        assert round(int(c.data.generic_attrs["Temp_052"]) / 2.0, 1) == 25.5

    async def test_lan_push_ignored(self) -> None:
        c = _coordinator(DEVICE_TYPE_AQUARIUM_SYS)
        n_before = len(c._notifications)
        c._on_device_push()
        assert len(c._notifications) == n_before

    async def test_update_data_calls_cloud_seed(self) -> None:
        c = _coordinator(DEVICE_TYPE_AQUARIUM_SYS)
        c.cloud.async_get_device_status.return_value = AQUARIUM_SYS_CLOUD_PAYLOAD_ON
        await c._async_update_data()
        c.cloud.async_get_device_status.assert_awaited_once()


# ---------------------------------------------------------------------------
# Cross-cutting: LAN push gating
# ---------------------------------------------------------------------------


class TestLanPushGating:
    """All non-Gyre devices must silently discard LAN push callbacks."""

    @pytest.mark.parametrize("device_type", [
        DEVICE_TYPE_LED_6CH,
        DEVICE_TYPE_LED_8CH,
        DEVICE_TYPE_LED_E8,
        DEVICE_TYPE_AQUARIUM_20,
        DEVICE_TYPE_AQUARIUM_SYS,
    ])
    def test_lan_push_never_notifies_for_non_gyre(self, device_type: str) -> None:
        c = _coordinator(device_type)
        n_before = len(c._notifications)
        c._on_device_push()
        assert len(c._notifications) == n_before, (
            f"{device_type}: expected no notification on LAN push"
        )

    def test_lan_push_notifies_for_gyre(self) -> None:
        c = _coordinator(DEVICE_TYPE_GYRE)
        n_before = len(c._notifications)
        c._on_device_push()
        assert len(c._notifications) == n_before + 1


# ---------------------------------------------------------------------------
# Cross-cutting: periodic cloud refresh
# ---------------------------------------------------------------------------


class TestPeriodicCloudRefresh:
    """_async_update_data must trigger cloud seed for non-Gyre, skip for Gyre."""

    @pytest.mark.parametrize("device_type,payload", [
        (DEVICE_TYPE_LED_6CH,      L165_CLOUD_PAYLOAD_ON),
        (DEVICE_TYPE_LED_8CH,      L265_CLOUD_PAYLOAD_ON),
        (DEVICE_TYPE_LED_E8,       E8_CLOUD_PAYLOAD_ON),
        (DEVICE_TYPE_AQUARIUM_20,  AQUARIUM_20_CLOUD_PAYLOAD_RUNNING),
        (DEVICE_TYPE_AQUARIUM_SYS, AQUARIUM_SYS_CLOUD_PAYLOAD_ON),
    ])
    async def test_non_gyre_calls_cloud_on_update(
        self, device_type: str, payload: dict
    ) -> None:
        c = _coordinator(device_type)
        c.cloud.async_get_device_status.return_value = payload
        await c._async_update_data()
        c.cloud.async_get_device_status.assert_awaited_once()

    async def test_gyre_does_not_call_cloud_on_update(self) -> None:
        c = _coordinator(DEVICE_TYPE_GYRE)
        await c._async_update_data()
        c.cloud.async_get_device_status.assert_not_awaited()


# ---------------------------------------------------------------------------
# Cross-cutting: DEVICE_CONTROL mapping correctness
# ---------------------------------------------------------------------------


class TestDeviceControlMapping:
    """Sanity-check the DEVICE_CONTROL constant so typos are caught immediately."""

    @pytest.mark.parametrize("device_type,attr,on_val,off_val", [
        (DEVICE_TYPE_GYRE,         "Mode",       5,  3),
        (DEVICE_TYPE_LED_6CH,      "mode",       0,  3),
        (DEVICE_TYPE_LED_8CH,      "MODE",       0,  2),
        (DEVICE_TYPE_LED_E8,       "MODE",       0,  2),
        (DEVICE_TYPE_AQUARIUM_20,  "Mode",       0,  1),
        (DEVICE_TYPE_AQUARIUM_SYS, "Switch_All", 1,  0),
    ])
    def test_control_mapping(
        self, device_type: str, attr: str, on_val: int, off_val: int
    ) -> None:
        ctrl = DEVICE_CONTROL[device_type]
        assert ctrl["attr"]  == attr,    f"{device_type}: wrong attr"
        assert ctrl["on"]    == on_val,  f"{device_type}: wrong on value"
        assert ctrl["off"]   == off_val, f"{device_type}: wrong off value"

    def test_all_known_device_types_have_control_entry(self) -> None:
        for dt in (
            DEVICE_TYPE_GYRE,
            DEVICE_TYPE_LED_6CH, DEVICE_TYPE_LED_8CH, DEVICE_TYPE_LED_E8,
            DEVICE_TYPE_AQUARIUM_20, DEVICE_TYPE_AQUARIUM_SYS,
        ):
            assert dt in DEVICE_CONTROL, f"{dt} missing from DEVICE_CONTROL"
