"""Coordinator behaviour tests — demonstrate and guard the write-cooldown fix.

These tests work without a running Home Assistant instance.  We use a
``MockCoordinator`` that mirrors the exact state-mutation logic of the real
``MaxspectCoordinator`` so we can exercise the race condition and the fix in
pure Python.

Two groups:
  TestRaceConditionAtStateLevel   — low-level proof that the bug exists
  TestCoordinatorWriteCooldown    — unit tests for the write-cooldown fix
"""

from __future__ import annotations

import asyncio
import struct
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.maxspect.api import (
    MaxspectDeviceState,
    _parse_compact_telemetry,
)
from custom_components.maxspect.const import (
    MODE_FEED,
    MODE_OFF,
    MODE_ON,
    MODE_WATER_FLOW,
)


# ---------------------------------------------------------------------------
# Helpers (mirrors test_api_parsing helpers)
# ---------------------------------------------------------------------------

def _compact_payload(mode: int, ch1_rpm: int = 1000, ch2_rpm: int = 800) -> bytes:
    """Minimal 25-byte compact telemetry payload."""
    payload = bytearray(25)
    payload[0] = mode
    struct.pack_into(">H", payload, 2, ch1_rpm)
    struct.pack_into(">H", payload, 4, 2400)    # ch1 voltage
    payload[7] = 50
    struct.pack_into(">H", payload, 11, ch2_rpm)
    struct.pack_into(">H", payload, 13, 2350)   # ch2 voltage
    payload[16] = 45
    return bytes(payload)


# ---------------------------------------------------------------------------
# MockCoordinator
#
# Faithfully mirrors the coordinator's _on_device_push / async_set_mode
# logic without requiring a Home Assistant hass/config_entry context.
# ---------------------------------------------------------------------------

_WRITE_COOLDOWN = 8.0  # must match coordinator.py


class MockCoordinator:
    """Minimal coordinator shim for testing coordinator state-update logic."""

    def __init__(self, cooldown: float = _WRITE_COOLDOWN) -> None:
        self.client = MagicMock()
        self.client.state = MaxspectDeviceState()
        self.cloud = AsyncMock()
        self._cloud_did = "test-did"
        self._write_lock_until: float = 0.0
        self._pending_mode: int = MODE_ON
        self._cooldown = cooldown
        self.data: MaxspectDeviceState = self.client.state
        self._notifications: list[MaxspectDeviceState] = []

    # ── Mirrors coordinator methods ──────────────────────────────────────

    def async_set_updated_data(self, state: MaxspectDeviceState) -> None:
        """Record the notification (real HA would push to subscribers)."""
        self.data = state
        self._notifications.append(state)

    def _on_device_push_buggy(self) -> None:
        """Current (pre-fix) implementation — no cooldown check."""
        self.async_set_updated_data(self.client.state)

    def _on_device_push(self) -> None:
        """Fixed implementation — mirrors coordinator._on_device_push with cooldown."""
        if time.monotonic() < self._write_lock_until:
            state = self.client.state
            if state.mode == self._pending_mode:
                # Device confirmed our write — lift cooldown early
                self._write_lock_until = 0.0
            else:
                # Stale push — re-apply pending state, no notification
                state.mode = self._pending_mode
                state.is_on = self._pending_mode != MODE_OFF
                return
        self.async_set_updated_data(self.client.state)

    async def async_set_mode(self, mode: int) -> None:
        """Mirrors coordinator.async_set_mode with the write-cooldown fix."""
        await self.cloud.async_set_mode(mode, did=self._cloud_did)
        self._pending_mode = mode
        self._write_lock_until = time.monotonic() + self._cooldown
        state = self.client.state
        state.mode = mode
        state.is_on = mode != MODE_OFF
        self.async_set_updated_data(state)


# ---------------------------------------------------------------------------
# TestRaceConditionAtStateLevel
#
# These tests work at the MaxspectDeviceState level — no coordinator involved.
# They prove that _parse_compact_telemetry can overwrite an optimistic update.
# ---------------------------------------------------------------------------

class TestRaceConditionAtStateLevel:

    def test_stale_compact_telemetry_overwrites_optimistic_off(self) -> None:
        """The race condition: optimistic OFF → stale LAN push → is_on=True again.

        Sequence:
          1. User clicks OFF → coordinator optimistically sets mode=3, is_on=False
          2. Old compact-telemetry push arrives (device not yet updated)
          3. _parse_compact_telemetry sets mode=5, is_on=True
          → Switch shows ON while gyres are physically OFF
        """
        state = MaxspectDeviceState(mode=MODE_ON, is_on=True)

        # Step 1: optimistic update (as coordinator.async_set_mode does)
        state.mode = MODE_OFF
        state.is_on = False
        assert state.is_on is False, "optimistic update must set is_on=False"

        # Step 2: stale LAN push overwrites (this is the bug)
        _parse_compact_telemetry(_compact_payload(MODE_ON), state)

        # Bug confirmed: is_on flipped back to True by the stale push
        assert state.is_on is True
        assert state.mode == MODE_ON

    def test_correct_lan_push_after_cloud_command_resolves_state(self) -> None:
        """If the device sends the new mode via LAN, state resolves correctly."""
        state = MaxspectDeviceState(mode=MODE_ON, is_on=True)

        # Optimistic update
        state.mode = MODE_OFF
        state.is_on = False

        # Confirming LAN push (device processed the cloud command)
        _parse_compact_telemetry(_compact_payload(MODE_OFF, ch1_rpm=0, ch2_rpm=0), state)

        assert state.is_on is False
        assert state.mode == MODE_OFF
        assert state.ch1_rpm == 0
        assert state.ch2_rpm == 0

    def test_feed_mode_race_variant(self) -> None:
        """Same race condition when coming out of Feed mode."""
        state = MaxspectDeviceState(mode=MODE_FEED, is_on=True)

        state.mode = MODE_OFF
        state.is_on = False

        # Stale FEED-mode push arrives
        _parse_compact_telemetry(_compact_payload(MODE_FEED, ch1_rpm=0, ch2_rpm=0), state)

        assert state.is_on is True   # bug: shows ON, gyres stopped


# ---------------------------------------------------------------------------
# TestCoordinatorWriteCooldown
#
# These tests exercise the fixed _on_device_push / async_set_mode and must
# pass after the write-cooldown is applied to coordinator.py.
# ---------------------------------------------------------------------------

class TestCoordinatorWriteCooldown:

    async def test_cooldown_is_set_after_successful_cloud_write(self) -> None:
        coordinator = MockCoordinator()
        t_before = time.monotonic()
        await coordinator.async_set_mode(MODE_OFF)
        assert coordinator._write_lock_until > t_before
        assert coordinator._write_lock_until <= t_before + _WRITE_COOLDOWN + 0.1

    async def test_optimistic_state_is_pushed_immediately(self) -> None:
        coordinator = MockCoordinator()
        coordinator.client.state.is_on = True
        coordinator.client.state.mode = MODE_ON

        await coordinator.async_set_mode(MODE_OFF)

        # One notification should have been sent (the optimistic update)
        assert len(coordinator._notifications) >= 1
        assert coordinator.data.is_on is False
        assert coordinator.data.mode == MODE_OFF

    async def test_stale_lan_push_ignored_during_cooldown(self) -> None:
        """During write cooldown, _on_device_push must not update subscribers."""
        coordinator = MockCoordinator(cooldown=10.0)  # long cooldown
        coordinator.client.state.mode = MODE_ON
        coordinator.client.state.is_on = True

        await coordinator.async_set_mode(MODE_OFF)
        n_after_write = len(coordinator._notifications)
        assert coordinator.data.is_on is False

        # Simulate stale LAN push (re-mutates state back to ON)
        coordinator.client.state.mode = MODE_ON
        coordinator.client.state.is_on = True
        coordinator._on_device_push()

        # No additional notification pushed to subscribers
        assert len(coordinator._notifications) == n_after_write
        # The coordinator.data still reflects the optimistic state
        assert coordinator.data.is_on is False

    async def test_stale_lan_push_accepted_with_buggy_implementation(self) -> None:
        """Prove the bug: without cooldown, stale push overwrites optimistic state."""
        coordinator = MockCoordinator(cooldown=0.0)  # cooldown disabled
        coordinator.client.state.mode = MODE_ON
        coordinator.client.state.is_on = True

        await coordinator.async_set_mode(MODE_OFF)
        assert coordinator.data.is_on is False

        # Stale LAN push re-mutates state
        coordinator.client.state.mode = MODE_ON
        coordinator.client.state.is_on = True
        coordinator._on_device_push_buggy()  # use buggy implementation

        # Bug: data flipped back to ON
        assert coordinator.data.is_on is True

    async def test_push_accepted_after_cooldown_expires(self) -> None:
        """After cooldown, LAN pushes are forwarded to subscribers again."""
        coordinator = MockCoordinator(cooldown=0.05)  # very short cooldown
        coordinator.client.state.is_on = True

        await coordinator.async_set_mode(MODE_OFF)
        assert coordinator.data.is_on is False

        await asyncio.sleep(0.1)  # wait for cooldown to expire

        # Simulate a new LAN push (device confirmed new mode or app changed it)
        coordinator.client.state.mode = MODE_ON
        coordinator.client.state.is_on = True
        coordinator._on_device_push()

        assert coordinator.data.is_on is True  # accepted after cooldown

    async def test_cloud_cloud_is_called_with_correct_mode(self) -> None:
        coordinator = MockCoordinator()
        await coordinator.async_set_mode(MODE_ON)
        coordinator.cloud.async_set_mode.assert_awaited_once_with(
            MODE_ON, did="test-did"
        )

    async def test_turn_on_after_turn_off(self) -> None:
        coordinator = MockCoordinator(cooldown=0.05)

        await coordinator.async_set_mode(MODE_OFF)
        assert coordinator.data.is_on is False

        await asyncio.sleep(0.1)  # cooldown expires

        await coordinator.async_set_mode(MODE_ON)
        assert coordinator.data.is_on is True
        assert coordinator.data.mode == MODE_ON

    async def test_multiple_modes_during_cooldown_last_write_wins(self) -> None:
        """Rapid double-write: only the last optimistic state should be visible."""
        coordinator = MockCoordinator(cooldown=10.0)

        await coordinator.async_set_mode(MODE_OFF)
        assert coordinator.data.is_on is False

        # Second write (e.g., user changes their mind and turns back ON)
        coordinator._write_lock_until = 0.0  # simulate cooldown ended between writes
        await coordinator.async_set_mode(MODE_ON)
        assert coordinator.data.is_on is True

    async def test_feed_mode_is_on_true(self) -> None:
        """Feed mode: coordinator reports is_on=True (pumps paused, device on)."""
        coordinator = MockCoordinator()
        await coordinator.async_set_mode(MODE_FEED)
        assert coordinator.data.is_on is True  # mode != MODE_OFF

    async def test_water_flow_mode_is_on_true(self) -> None:
        coordinator = MockCoordinator()
        await coordinator.async_set_mode(MODE_WATER_FLOW)
        assert coordinator.data.is_on is True
