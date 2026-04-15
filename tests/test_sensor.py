"""Integration tests for the Maxspect sensor platform.

Tests that sensor entities appear with correct values from the HA state machine:
  - Gyre sensors: mode, RPM, voltage, power, timestamp, feed_duration, model, wash
  - Sensor values update when coordinator data changes
  - Zero/None handling for optional sensors
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.integration

from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.maxspect.const import (
    MODE_FEED,
    MODE_OFF,
    MODE_ON,
    MODE_PROGRAMMING,
    MODE_WATER_FLOW,
)

from .conftest import setup_integration


# ---------------------------------------------------------------------------
# Helper: request no_cloud_seed fixture for tests that set non-default state
# ---------------------------------------------------------------------------
@pytest.fixture
def _skip_cloud_seed(no_cloud_seed):
    """Activate no_cloud_seed to prevent cloud overriding mock state."""


class TestGyreModeSensor:

    async def test_mode_on(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """Mode sensor shows 'On' when device mode is MODE_ON."""
        await setup_integration(hass, gyre_config_entry)

        state = hass.states.get("sensor.maxspect_my_gyre_mode")
        assert state is not None
        assert state.state == "On"

    @pytest.mark.usefixtures("_skip_cloud_seed")
    async def test_mode_off(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """Mode sensor shows 'Off' when device mode is MODE_OFF."""
        mock_maxspect_client.state.mode = MODE_OFF
        mock_maxspect_client.state.is_on = False

        await setup_integration(hass, gyre_config_entry)

        state = hass.states.get("sensor.maxspect_my_gyre_mode")
        assert state is not None
        assert state.state == "Off"

    @pytest.mark.usefixtures("_skip_cloud_seed")
    async def test_mode_water_flow(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """Mode sensor shows correct name for each mode value."""
        mock_maxspect_client.state.mode = MODE_WATER_FLOW

        await setup_integration(hass, gyre_config_entry)

        state = hass.states.get("sensor.maxspect_my_gyre_mode")
        assert state.state == "Water Flow"

    @pytest.mark.usefixtures("_skip_cloud_seed")
    async def test_mode_feed(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """Feed mode appears as 'Feed' in sensor."""
        mock_maxspect_client.state.mode = MODE_FEED

        await setup_integration(hass, gyre_config_entry)

        state = hass.states.get("sensor.maxspect_my_gyre_mode")
        assert state.state == "Feed"

    @pytest.mark.usefixtures("_skip_cloud_seed")
    async def test_mode_programming(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        mock_maxspect_client.state.mode = MODE_PROGRAMMING

        await setup_integration(hass, gyre_config_entry)

        state = hass.states.get("sensor.maxspect_my_gyre_mode")
        assert state.state == "Programming"


class TestGyreRPMSensors:

    async def test_ch1_rpm(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """Channel 1 RPM sensor reports correct value."""
        await setup_integration(hass, gyre_config_entry)

        state = hass.states.get("sensor.maxspect_my_gyre_channel_1_rpm")
        assert state is not None
        assert state.state == "1500"

    async def test_ch2_rpm(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """Channel 2 RPM sensor reports correct value."""
        await setup_integration(hass, gyre_config_entry)

        state = hass.states.get("sensor.maxspect_my_gyre_channel_2_rpm")
        assert state is not None
        assert state.state == "1200"

    @pytest.mark.usefixtures("_skip_cloud_seed")
    async def test_rpm_zero_is_none(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """RPM of 0 reports as unknown/None (pump stopped)."""
        mock_maxspect_client.state.ch1_rpm = 0

        await setup_integration(hass, gyre_config_entry)

        state = hass.states.get("sensor.maxspect_my_gyre_channel_1_rpm")
        assert state is not None
        # Zero RPM → native_value returns None → unknown state
        assert state.state == STATE_UNKNOWN


class TestGyreVoltageSensors:

    async def test_ch1_voltage(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        await setup_integration(hass, gyre_config_entry)

        state = hass.states.get("sensor.maxspect_my_gyre_channel_1_voltage")
        assert state is not None
        assert float(state.state) == pytest.approx(24.37)

    async def test_ch2_voltage(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        await setup_integration(hass, gyre_config_entry)

        state = hass.states.get("sensor.maxspect_my_gyre_channel_2_voltage")
        assert state is not None
        assert float(state.state) == pytest.approx(23.60)


class TestGyrePowerSensors:

    async def test_ch1_power(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        await setup_integration(hass, gyre_config_entry)

        state = hass.states.get("sensor.maxspect_my_gyre_channel_1_power")
        assert state is not None
        assert state.state == "72"

    async def test_ch2_power(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        await setup_integration(hass, gyre_config_entry)

        state = hass.states.get("sensor.maxspect_my_gyre_channel_2_power")
        assert state is not None
        assert state.state == "65"

    @pytest.mark.usefixtures("_skip_cloud_seed")
    async def test_power_zero_is_none(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        mock_maxspect_client.state.ch1_power = 0

        await setup_integration(hass, gyre_config_entry)

        state = hass.states.get("sensor.maxspect_my_gyre_channel_1_power")
        assert state.state == STATE_UNKNOWN


class TestGyreDiagnosticSensors:

    async def test_timestamp(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        await setup_integration(hass, gyre_config_entry)

        state = hass.states.get("sensor.maxspect_my_gyre_device_timestamp")
        assert state is not None
        assert state.state == "2026-04-11 14:30:00"

    async def test_feed_duration(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        await setup_integration(hass, gyre_config_entry)

        state = hass.states.get("sensor.maxspect_my_gyre_feed_duration")
        assert state is not None
        assert state.state == "10"

    async def test_model_a(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        await setup_integration(hass, gyre_config_entry)

        state = hass.states.get("sensor.maxspect_my_gyre_pump_a_model")
        assert state is not None
        assert state.state == "XF 330CE"

    @pytest.mark.usefixtures("_skip_cloud_seed")
    async def test_model_b_xf350(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """Model B with non-zero value → XF 350CE."""
        mock_maxspect_client.state.model_b = 1

        await setup_integration(hass, gyre_config_entry)

        state = hass.states.get("sensor.maxspect_my_gyre_pump_b_model")
        assert state is not None
        assert state.state == "XF 350CE"

    async def test_wash_reminder(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        await setup_integration(hass, gyre_config_entry)

        state = hass.states.get("sensor.maxspect_my_gyre_wash_reminder")
        assert state is not None
        assert state.state == "7"

    @pytest.mark.usefixtures("_skip_cloud_seed")
    async def test_timestamp_empty_is_none(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        mock_maxspect_client.state.timestamp = ""

        await setup_integration(hass, gyre_config_entry)

        state = hass.states.get("sensor.maxspect_my_gyre_device_timestamp")
        assert state.state == STATE_UNKNOWN


class TestSensorStateUpdates:

    async def test_sensor_updates_when_coordinator_data_changes(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """Sensor reflects new values when coordinator pushes updated data."""
        await setup_integration(hass, gyre_config_entry)

        state = hass.states.get("sensor.maxspect_my_gyre_channel_1_rpm")
        assert state.state == "1500"

        # Simulate LAN push updating state
        coordinator = gyre_config_entry.runtime_data
        coordinator.client.state.ch1_rpm = 2000
        coordinator.async_set_updated_data(coordinator.client.state)
        await hass.async_block_till_done()

        state = hass.states.get("sensor.maxspect_my_gyre_channel_1_rpm")
        assert state.state == "2000"

    async def test_mode_updates_on_push(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """Mode sensor updates when coordinator receives mode change."""
        await setup_integration(hass, gyre_config_entry)

        assert hass.states.get("sensor.maxspect_my_gyre_mode").state == "On"

        coordinator = gyre_config_entry.runtime_data
        coordinator.client.state.mode = MODE_FEED
        coordinator.client.state.is_on = True
        coordinator.async_set_updated_data(coordinator.client.state)
        await hass.async_block_till_done()

        assert hass.states.get("sensor.maxspect_my_gyre_mode").state == "Feed"
