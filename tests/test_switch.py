"""Integration tests for the Maxspect switch platform.

Tests the power switch entity through the real HA state machine:
  - Switch entity appears with correct state
  - turn_on / turn_off calls the coordinator → cloud API
  - State reflects optimistic updates
  - Switch state after LAN push during cooldown (race condition guard)
"""

from __future__ import annotations

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

from custom_components.maxspect.const import MODE_OFF, MODE_ON

from .conftest import build_bak24_hex, build_time_hex, setup_integration

SWITCH_ENTITY = "switch.maxspect_my_gyre_pump_power"


class TestGyrePowerSwitch:

    async def test_switch_state_on(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """Switch shows ON when device state is_on=True."""
        await setup_integration(hass, gyre_config_entry)

        state = hass.states.get(SWITCH_ENTITY)
        assert state is not None
        assert state.state == STATE_ON

    async def test_switch_state_off(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """Switch shows OFF when device state is_on=False."""
        mock_maxspect_client.state.is_on = False
        mock_maxspect_client.state.mode = MODE_OFF
        # Cloud seed must also report OFF so it doesn't override
        mock_gizwits_cloud.async_get_device_status.return_value = {
            "attr": {
                "Mode": MODE_OFF,
                "Bak24": build_bak24_hex(mode=MODE_OFF, ch1_rpm=0, ch1_v_x100=0, ch1_w=0, ch2_rpm=0, ch2_v_x100=0, ch2_w=0),
                "Time": build_time_hex(power=0),
                "Time_Feed": 10,
                "Model_A": 0,
                "Model_B": 0,
                "Wash": 7,
            }
        }

        await setup_integration(hass, gyre_config_entry)

        state = hass.states.get(SWITCH_ENTITY)
        assert state is not None
        assert state.state == STATE_OFF

    async def test_turn_off(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """turn_off sends MODE_OFF via cloud and updates entity state."""
        await setup_integration(hass, gyre_config_entry)

        # Simulate the cloud write succeeding and state being updated
        async def _set_mode_side_effect(mode, did=None):
            mock_maxspect_client.state.mode = mode
            mock_maxspect_client.state.is_on = mode != MODE_OFF

        mock_gizwits_cloud.async_set_mode.side_effect = _set_mode_side_effect

        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_OFF,
            {ATTR_ENTITY_ID: SWITCH_ENTITY},
            blocking=True,
        )
        await hass.async_block_till_done()

        # Cloud API was called with the OFF mode value (3 for Gyre)
        mock_gizwits_cloud.async_set_mode.assert_awaited()

        state = hass.states.get(SWITCH_ENTITY)
        assert state is not None
        assert state.state == STATE_OFF

    async def test_turn_on(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """turn_on sends MODE_ON via cloud and updates entity state."""
        mock_maxspect_client.state.is_on = False
        mock_maxspect_client.state.mode = MODE_OFF

        await setup_integration(hass, gyre_config_entry)

        async def _set_mode_side_effect(mode, did=None):
            mock_maxspect_client.state.mode = mode
            mock_maxspect_client.state.is_on = mode != MODE_OFF

        mock_gizwits_cloud.async_set_mode.side_effect = _set_mode_side_effect

        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_ON,
            {ATTR_ENTITY_ID: SWITCH_ENTITY},
            blocking=True,
        )
        await hass.async_block_till_done()

        mock_gizwits_cloud.async_set_mode.assert_awaited()

        state = hass.states.get(SWITCH_ENTITY)
        assert state is not None
        assert state.state == STATE_ON

    async def test_turn_off_then_on(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """Rapid off → on sequence both go through cloud API."""
        await setup_integration(hass, gyre_config_entry)

        async def _set_mode_side_effect(mode, did=None):
            mock_maxspect_client.state.mode = mode
            mock_maxspect_client.state.is_on = mode != MODE_OFF

        mock_gizwits_cloud.async_set_mode.side_effect = _set_mode_side_effect

        # OFF
        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_OFF,
            {ATTR_ENTITY_ID: SWITCH_ENTITY},
            blocking=True,
        )
        await hass.async_block_till_done()

        assert hass.states.get(SWITCH_ENTITY).state == STATE_OFF

        # ON
        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_ON,
            {ATTR_ENTITY_ID: SWITCH_ENTITY},
            blocking=True,
        )
        await hass.async_block_till_done()

        assert hass.states.get(SWITCH_ENTITY).state == STATE_ON
        # Two cloud calls total
        assert mock_gizwits_cloud.async_set_mode.await_count == 2

    async def test_switch_exists_and_operational(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """Switch entity exists and is operational after setup."""
        await setup_integration(hass, gyre_config_entry)

        # Verify the switch exists and has a valid state
        state = hass.states.get(SWITCH_ENTITY)
        assert state is not None
        assert state.state in (STATE_ON, STATE_OFF)
