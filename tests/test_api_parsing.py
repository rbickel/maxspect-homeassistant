"""Unit tests for Maxspect LAN protocol parsing functions.

These tests cover the pure parsing functions in api.py that have no
Home Assistant dependency. They are the ground truth for:

  - _parse_compact_telemetry  (periodic sensor push)
  - _parse_state_notify       (DP 34 / power + timestamp)
  - _dp_is_flagged            (attr_flags bit lookup)
  - _dp_data_offset           (byte offset within push payload)
"""

from __future__ import annotations

import struct

import pytest

from custom_components.maxspect.api import (
    MaxspectDeviceState,
    _dp_data_offset,
    _dp_is_flagged,
    _parse_compact_telemetry,
    _parse_state_notify,
)
from custom_components.maxspect.const import (
    ATTR_FLAGS_LEN,
    DP_LENGTHS,
    MODE_EXIT_FEED,
    MODE_FEED,
    MODE_OFF,
    MODE_ON,
    MODE_PROGRAMMING,
    MODE_WATER_FLOW,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compact_payload(
    mode: int,
    ch1_rpm: int = 1000,
    ch1_v_x100: int = 2400,
    ch1_w: int = 50,
    ch2_rpm: int = 800,
    ch2_v_x100: int = 2350,
    ch2_w: int = 45,
) -> bytes:
    """Build a minimal 25-byte compact telemetry payload."""
    payload = bytearray(25)
    payload[0] = mode
    struct.pack_into(">H", payload, 2, ch1_rpm)
    struct.pack_into(">H", payload, 4, ch1_v_x100)
    payload[7] = ch1_w
    struct.pack_into(">H", payload, 11, ch2_rpm)
    struct.pack_into(">H", payload, 13, ch2_v_x100)
    payload[16] = ch2_w
    return bytes(payload)


def _flags_for_dps(*dp_ids: int) -> bytes:
    """Build a 6-byte attr_flags bytestring with the given DPs flagged."""
    flags = bytearray(ATTR_FLAGS_LEN)
    for dp_id in dp_ids:
        byte_idx = ATTR_FLAGS_LEN - 1 - (dp_id // 8)
        bit_idx = dp_id % 8
        if 0 <= byte_idx < ATTR_FLAGS_LEN:
            flags[byte_idx] |= 1 << bit_idx
    return bytes(flags)


def _state_notify_payload(
    power_bit: int = 1,
    year: int = 25,
    month: int = 4,
    day: int = 10,
    hour: int = 14,
    minute: int = 30,
    second: int = 0,
) -> bytes:
    return bytes([power_bit, year, month, day, hour, minute, second])


# ---------------------------------------------------------------------------
# _parse_compact_telemetry
# ---------------------------------------------------------------------------

class TestParseCompactTelemetry:

    def test_mode_off_sets_is_on_false(self) -> None:
        state = MaxspectDeviceState()
        _parse_compact_telemetry(_compact_payload(MODE_OFF), state)
        assert state.is_on is False
        assert state.mode == MODE_OFF

    def test_mode_on_sets_is_on_true(self) -> None:
        state = MaxspectDeviceState()
        _parse_compact_telemetry(_compact_payload(MODE_ON), state)
        assert state.is_on is True
        assert state.mode == MODE_ON

    @pytest.mark.parametrize("mode", [
        MODE_WATER_FLOW, MODE_PROGRAMMING, MODE_FEED, MODE_EXIT_FEED, MODE_ON,
    ])
    def test_non_off_modes_set_is_on_true(self, mode: int) -> None:
        state = MaxspectDeviceState()
        _parse_compact_telemetry(_compact_payload(mode), state)
        assert state.is_on is True

    def test_feed_mode_is_on_true_pumps_paused(self) -> None:
        """FEED mode: device is 'on', but pumps not spinning.

        The current integration represents this as is_on=True, meaning the
        switch shows ON even though gyres are physically stopped.  This is
        a known, documented behaviour — not a bug — but can be confusing.
        The mode sensor ('Feed') disambiguates it.
        """
        state = MaxspectDeviceState()
        _parse_compact_telemetry(_compact_payload(MODE_FEED, ch1_rpm=0, ch2_rpm=0), state)
        assert state.is_on is True  # switch ON
        assert state.mode == MODE_FEED
        assert state.ch1_rpm == 0   # gyres not spinning

    def test_ch1_rpm_extraction(self) -> None:
        state = MaxspectDeviceState()
        _parse_compact_telemetry(_compact_payload(MODE_ON, ch1_rpm=1500), state)
        assert state.ch1_rpm == 1500

    def test_ch2_rpm_extraction(self) -> None:
        state = MaxspectDeviceState()
        _parse_compact_telemetry(_compact_payload(MODE_ON, ch2_rpm=900), state)
        assert state.ch2_rpm == 900

    def test_ch1_voltage_conversion(self) -> None:
        state = MaxspectDeviceState()
        _parse_compact_telemetry(_compact_payload(MODE_ON, ch1_v_x100=2437), state)
        assert abs(state.ch1_voltage - 24.37) < 0.001

    def test_ch2_voltage_conversion(self) -> None:
        state = MaxspectDeviceState()
        _parse_compact_telemetry(_compact_payload(MODE_ON, ch2_v_x100=2360), state)
        assert abs(state.ch2_voltage - 23.60) < 0.001

    def test_ch1_power_extraction(self) -> None:
        state = MaxspectDeviceState()
        _parse_compact_telemetry(_compact_payload(MODE_ON, ch1_w=72), state)
        assert state.ch1_power == 72

    def test_ch2_power_extraction(self) -> None:
        state = MaxspectDeviceState()
        _parse_compact_telemetry(_compact_payload(MODE_ON, ch2_w=65), state)
        assert state.ch2_power == 65

    def test_last_active_mode_updated_when_on(self) -> None:
        state = MaxspectDeviceState()
        _parse_compact_telemetry(_compact_payload(MODE_WATER_FLOW), state)
        assert state.last_active_mode == MODE_WATER_FLOW

    def test_last_active_mode_not_updated_when_off(self) -> None:
        state = MaxspectDeviceState(last_active_mode=MODE_ON)
        _parse_compact_telemetry(_compact_payload(MODE_OFF), state)
        assert state.last_active_mode == MODE_ON  # preserved from before

    def test_short_payload_is_ignored(self) -> None:
        """Payloads shorter than 17 bytes must not mutate state."""
        state = MaxspectDeviceState(mode=MODE_ON, is_on=True)
        _parse_compact_telemetry(b"\x03\x00", state)  # 2 bytes — too short
        assert state.mode == MODE_ON
        assert state.is_on is True

    def test_exact_minimum_length_accepted(self) -> None:
        """17-byte payload is the minimum that triggers parsing."""
        state = MaxspectDeviceState()
        _parse_compact_telemetry(_compact_payload(MODE_OFF)[:17], state)
        assert state.mode == MODE_OFF


# ---------------------------------------------------------------------------
# _parse_state_notify
# ---------------------------------------------------------------------------

class TestParseStateNotify:

    def test_timestamp_parsed_correctly(self) -> None:
        state = MaxspectDeviceState()
        _parse_state_notify(_state_notify_payload(1, 25, 4, 10, 14, 30, 0), state)
        assert state.timestamp == "2025-04-10 14:30:00"

    def test_power_bit_1_does_not_set_is_on(self) -> None:
        """DP 34 must NEVER modify is_on — pump state comes from Mode only."""
        state = MaxspectDeviceState(is_on=False, mode=MODE_OFF)
        _parse_state_notify(_state_notify_payload(power_bit=1), state)
        assert state.is_on is False  # unchanged despite hw_power=1

    def test_power_bit_0_does_not_clear_is_on(self) -> None:
        """DP 34 power bit 0 must not turn off is_on."""
        state = MaxspectDeviceState(is_on=True, mode=MODE_ON)
        _parse_state_notify(_state_notify_payload(power_bit=0), state)
        assert state.is_on is True  # unchanged despite hw_power=0

    def test_single_byte_payload_no_crash(self) -> None:
        """One-byte payload (only power byte, no timestamp) must not crash."""
        state = MaxspectDeviceState()
        _parse_state_notify(b"\x01", state)
        assert state.timestamp == ""

    def test_empty_payload_no_crash(self) -> None:
        state = MaxspectDeviceState()
        _parse_state_notify(b"", state)  # must not raise

    def test_invalid_month_zero_rejected(self) -> None:
        state = MaxspectDeviceState()
        _parse_state_notify(bytes([1, 25, 0, 10, 14, 30, 0]), state)  # month=0
        assert state.timestamp == ""

    def test_invalid_day_zero_rejected(self) -> None:
        state = MaxspectDeviceState()
        _parse_state_notify(bytes([1, 25, 4, 0, 14, 30, 0]), state)  # day=0
        assert state.timestamp == ""

    def test_invalid_month_13_rejected(self) -> None:
        state = MaxspectDeviceState()
        _parse_state_notify(bytes([1, 25, 13, 10, 14, 30, 0]), state)  # month=13
        assert state.timestamp == ""

    def test_invalid_day_32_rejected(self) -> None:
        state = MaxspectDeviceState()
        _parse_state_notify(bytes([1, 25, 4, 32, 14, 30, 0]), state)  # day=32
        assert state.timestamp == ""


# ---------------------------------------------------------------------------
# _dp_is_flagged
# ---------------------------------------------------------------------------

class TestDpIsFlagged:

    @pytest.mark.parametrize("dp", [18, 19, 20, 21, 22, 34, 35, 36])
    def test_single_dp_detected(self, dp: int) -> None:
        flags = _flags_for_dps(dp)
        assert _dp_is_flagged(flags, dp) is True

    @pytest.mark.parametrize("dp", [18, 19, 20, 21, 22, 34, 35, 36])
    def test_other_dps_not_detected(self, dp: int) -> None:
        flags = _flags_for_dps(dp)
        for other in [18, 19, 20, 21, 22, 34, 35, 36]:
            if other != dp:
                assert _dp_is_flagged(flags, other) is False, (
                    f"DP {other} should not be flagged when only DP {dp} is set"
                )

    def test_all_zero_flags(self) -> None:
        flags = b"\x00" * ATTR_FLAGS_LEN
        for dp in [18, 19, 20, 21, 22, 34, 35, 36]:
            assert _dp_is_flagged(flags, dp) is False

    def test_multiple_dps_flagged(self) -> None:
        flags = _flags_for_dps(18, 34)
        assert _dp_is_flagged(flags, 18) is True
        assert _dp_is_flagged(flags, 34) is True
        assert _dp_is_flagged(flags, 19) is False

    def test_compact_telemetry_shortcut_bit(self) -> None:
        """flags[0] & 0x10 is the compact-telemetry shortcut (not a standard DP).

        The code checks this directly as `flags[0] & 0x10` rather than via
        _dp_is_flagged, so verify the raw bit is independent from DP flags.
        """
        flags = bytes([0x10, 0, 0, 0, 0, 0])
        # flags[0] bit 4 set → NOT the same as a well-known DP
        assert flags[0] & 0x10  # raw check passes
        # DPs 18–22 and 34 are all unset
        for dp in [18, 19, 20, 21, 22, 34]:
            assert _dp_is_flagged(flags, dp) is False


# ---------------------------------------------------------------------------
# _dp_data_offset
# ---------------------------------------------------------------------------

class TestDpDataOffset:

    def test_single_dp_offset_is_zero(self) -> None:
        """The first (and only) DP in a payload is always at offset 0."""
        flags = _flags_for_dps(18)
        assert _dp_data_offset(flags, 18) == 0

    def test_second_dp_offset_after_first(self) -> None:
        """DP 19 follows DP 18 (1 byte each → offset 1)."""
        flags = _flags_for_dps(18, 19)
        assert _dp_data_offset(flags, 19) == DP_LENGTHS[18]  # 1

    def test_third_dp_offset_sum_of_preceding(self) -> None:
        """DP 20 follows DP 18 + DP 19 → offset 2."""
        flags = _flags_for_dps(18, 19, 20)
        assert _dp_data_offset(flags, 20) == DP_LENGTHS[18] + DP_LENGTHS[19]  # 2

    def test_dp_17_contributes_to_offset(self) -> None:
        """DP 17 (1 byte) before DP 18 shifts DP 18 to offset 1."""
        flags = _flags_for_dps(17, 18)
        assert _dp_data_offset(flags, 18) == DP_LENGTHS[17]  # 1

    def test_unflagged_dp_does_not_count(self) -> None:
        """A DP not in the flags does not contribute to the offset."""
        # Only DP 20 is flagged; DPs 17-19 absent → offset for 20 is 0
        flags = _flags_for_dps(20)
        assert _dp_data_offset(flags, 20) == 0

    def test_large_dp_offset(self) -> None:
        """DP 34 (7 bytes) before DP 35 shifts DP 35 offset by 7."""
        flags = _flags_for_dps(34, 35)
        # DPs below 34 absent; DP 34 is 7 bytes
        assert _dp_data_offset(flags, 35) == DP_LENGTHS[34]  # 7
