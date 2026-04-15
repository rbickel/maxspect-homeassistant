"""Integration tests for the coordinator's write-cooldown through HA.

These tests exercise the race-condition protection end-to-end within the
real HA framework.  They complement the pure-Python tests in
test_coordinator_behaviour.py by going through the actual coordinator
instance created by async_setup_entry.

Tests cover:
  - Write cooldown is activated after async_set_power(False)
  - Stale LAN push during cooldown does not flip entity state
  - LAN push confirming mode lifts cooldown early
  - After cooldown expires, LAN pushes are accepted normally
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.integration

from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.maxspect.api import _parse_compact_telemetry
from custom_components.maxspect.const import MODE_FEED, MODE_OFF, MODE_ON

from .conftest import build_compact_payload, setup_integration

SWITCH_ENTITY = "switch.maxspect_my_gyre_pump_power"


class TestWriteCooldownIntegration:

    async def test_cooldown_activated_after_turn_off(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """Coordinator sets _write_lock_until after a turn_off service call."""
        await setup_integration(hass, gyre_config_entry)

        coordinator = gyre_config_entry.runtime_data
        assert coordinator._write_lock_until == 0.0

        async def _set_mode_side_effect(mode, did=None):
            pass  # Don't mutate state — let coordinator handle it

        mock_gizwits_cloud.async_set_mode.side_effect = _set_mode_side_effect

        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_OFF,
            {ATTR_ENTITY_ID: SWITCH_ENTITY},
            blocking=True,
        )
        await hass.async_block_till_done()

        assert coordinator._write_lock_until > time.monotonic() - 1.0
        assert coordinator._pending_mode == MODE_OFF

    async def test_stale_lan_push_suppressed_during_cooldown(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """During cooldown, stale LAN pushes must not flip entity back to ON."""
        await setup_integration(hass, gyre_config_entry)
        coordinator = gyre_config_entry.runtime_data

        # Turn off via service
        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_OFF,
            {ATTR_ENTITY_ID: SWITCH_ENTITY},
            blocking=True,
        )
        await hass.async_block_till_done()

        assert hass.states.get(SWITCH_ENTITY).state == STATE_OFF

        # Simulate a stale LAN push (device hasn't processed cloud command yet)
        _parse_compact_telemetry(
            build_compact_payload(mode=MODE_ON, ch1_rpm=1500, ch2_rpm=1200),
            coordinator.client.state,
        )
        coordinator._on_device_push()
        await hass.async_block_till_done()

        # Entity MUST still show OFF — cooldown suppressed the stale push
        state = hass.states.get(SWITCH_ENTITY)
        assert state.state == STATE_OFF

    async def test_confirming_push_lifts_cooldown(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """LAN push matching the pending mode lifts cooldown early."""
        await setup_integration(hass, gyre_config_entry)
        coordinator = gyre_config_entry.runtime_data

        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_OFF,
            {ATTR_ENTITY_ID: SWITCH_ENTITY},
            blocking=True,
        )
        await hass.async_block_till_done()

        assert coordinator._write_lock_until > 0.0

        # LAN push confirms MODE_OFF — device processed the command
        _parse_compact_telemetry(
            build_compact_payload(mode=MODE_OFF, ch1_rpm=0, ch2_rpm=0),
            coordinator.client.state,
        )
        coordinator._on_device_push()
        await hass.async_block_till_done()

        # Cooldown lifted early
        assert coordinator._write_lock_until == 0.0

        state = hass.states.get(SWITCH_ENTITY)
        assert state.state == STATE_OFF

    async def test_push_accepted_after_cooldown_expires(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """After cooldown expires, LAN pushes update entity state normally."""
        await setup_integration(hass, gyre_config_entry)
        coordinator = gyre_config_entry.runtime_data

        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_OFF,
            {ATTR_ENTITY_ID: SWITCH_ENTITY},
            blocking=True,
        )
        await hass.async_block_till_done()

        # Force cooldown to expire
        coordinator._write_lock_until = time.monotonic() - 1.0

        # Now a LAN push with MODE_ON should be accepted
        _parse_compact_telemetry(
            build_compact_payload(mode=MODE_ON, ch1_rpm=1500, ch2_rpm=1200),
            coordinator.client.state,
        )
        coordinator._on_device_push()
        await hass.async_block_till_done()

        state = hass.states.get(SWITCH_ENTITY)
        assert state.state == STATE_ON

    async def test_feed_mode_push_suppressed_during_off_cooldown(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """Feed mode (is_on=True) push during OFF cooldown is suppressed."""
        await setup_integration(hass, gyre_config_entry)
        coordinator = gyre_config_entry.runtime_data

        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_OFF,
            {ATTR_ENTITY_ID: SWITCH_ENTITY},
            blocking=True,
        )
        await hass.async_block_till_done()

        # Stale feed-mode push
        _parse_compact_telemetry(
            build_compact_payload(mode=MODE_FEED, ch1_rpm=0, ch2_rpm=0),
            coordinator.client.state,
        )
        coordinator._on_device_push()
        await hass.async_block_till_done()

        # Entity must still show OFF
        state = hass.states.get(SWITCH_ENTITY)
        assert state.state == STATE_OFF

    async def test_multiple_pushes_during_cooldown_all_suppressed(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """Multiple stale pushes during cooldown are all suppressed."""
        await setup_integration(hass, gyre_config_entry)
        coordinator = gyre_config_entry.runtime_data

        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_OFF,
            {ATTR_ENTITY_ID: SWITCH_ENTITY},
            blocking=True,
        )
        await hass.async_block_till_done()

        # Send 5 stale pushes
        for _ in range(5):
            _parse_compact_telemetry(
                build_compact_payload(mode=MODE_ON, ch1_rpm=1500, ch2_rpm=1200),
                coordinator.client.state,
            )
            coordinator._on_device_push()

        await hass.async_block_till_done()

        state = hass.states.get(SWITCH_ENTITY)
        assert state.state == STATE_OFF


class TestCoordinatorCloudSeeding:

    async def test_cloud_seed_populates_state(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """Cloud seeding during setup populates state correctly."""
        await setup_integration(hass, gyre_config_entry)

        coordinator = gyre_config_entry.runtime_data
        assert coordinator.data.is_on is True
        assert coordinator.data.mode == MODE_ON

    async def test_cloud_seed_empty_attrs_non_fatal(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """Cloud returning empty attrs is non-fatal."""
        mock_gizwits_cloud.async_get_device_status.return_value = {"attr": {}}

        await setup_integration(hass, gyre_config_entry)

        # Entry still loaded, state comes from mock_lan_client defaults
        assert gyre_config_entry.runtime_data is not None
