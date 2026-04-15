"""Integration tests for async_setup_entry / async_unload_entry.

Tests the full entry lifecycle through the real HA runtime:
  - Gizwits setup: LAN connect → cloud login → cloud seed → platforms loaded
  - Gizwits setup: LAN connect fails → ConfigEntryNotReady
  - Gizwits setup: cloud login fails → entry still loads (control disabled)
  - Gizwits unload: platforms removed, client disconnected, cloud closed
  - ICV6 setup: hub device registered before child devices
  - Entry state transitions verified via config_entry.state
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.integration

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.maxspect.api import MaxspectConnectionError
from custom_components.maxspect.cloud import GizwitsCloudError
from custom_components.maxspect.const import CONF_DEVICE_PROTOCOL, DEVICE_PROTOCOL_ICV6, DOMAIN, MODE_ON

from .conftest import GYRE_CONFIG_DATA, setup_integration


# ---------------------------------------------------------------------------
# Gizwits setup happy path
# ---------------------------------------------------------------------------

class TestGizwitsSetup:

    async def test_setup_entry_success(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """Full Gizwits setup: connect, cloud login, cloud seed, platforms loaded."""
        await setup_integration(hass, gyre_config_entry)

        assert gyre_config_entry.state is ConfigEntryState.LOADED

        # Verify client was connected
        mock_maxspect_client.async_connect.assert_awaited_once()

        # Verify cloud login was called
        mock_gizwits_cloud.async_login.assert_awaited_once()

        # Verify coordinator is stored as runtime_data
        coordinator = gyre_config_entry.runtime_data
        assert coordinator is not None
        assert coordinator.data is not None
        assert coordinator.data.is_on is True

    async def test_setup_lan_failure_raises_not_ready(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """LAN connect failure → ConfigEntryNotReady, entry in SETUP_RETRY."""
        mock_maxspect_client.async_connect.side_effect = MaxspectConnectionError(
            "Connection refused"
        )

        gyre_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(gyre_config_entry.entry_id)
        await hass.async_block_till_done()

        assert gyre_config_entry.state is ConfigEntryState.SETUP_RETRY

    async def test_setup_cloud_login_failure_still_loads(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """Cloud login failure logs warning but entry still loads (control disabled)."""
        mock_gizwits_cloud.async_login.side_effect = GizwitsCloudError(
            "Network error"
        )

        await setup_integration(hass, gyre_config_entry)

        # Entry still loaded — LAN monitoring works, just no cloud control
        assert gyre_config_entry.state is ConfigEntryState.LOADED

    async def test_setup_cloud_seed_failure_still_loads(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """Cloud seed failure is non-fatal; entry loads and LAN data will arrive."""
        mock_gizwits_cloud.async_get_device_status.side_effect = GizwitsCloudError(
            "Timeout"
        )

        await setup_integration(hass, gyre_config_entry)

        assert gyre_config_entry.state is ConfigEntryState.LOADED


# ---------------------------------------------------------------------------
# Unload
# ---------------------------------------------------------------------------

class TestUnload:

    async def test_unload_entry(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """Unloading disconnects LAN and closes cloud session."""
        await setup_integration(hass, gyre_config_entry)
        assert gyre_config_entry.state is ConfigEntryState.LOADED

        await hass.config_entries.async_unload(gyre_config_entry.entry_id)
        await hass.async_block_till_done()

        assert gyre_config_entry.state is ConfigEntryState.NOT_LOADED
        # Verify cleanup was performed
        mock_maxspect_client.async_disconnect.assert_awaited()
        mock_gizwits_cloud.async_close.assert_awaited()

    async def test_unload_then_reload(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """Unload then reload should work cleanly."""
        await setup_integration(hass, gyre_config_entry)

        await hass.config_entries.async_unload(gyre_config_entry.entry_id)
        await hass.async_block_till_done()
        assert gyre_config_entry.state is ConfigEntryState.NOT_LOADED

        await hass.config_entries.async_setup(gyre_config_entry.entry_id)
        await hass.async_block_till_done()
        assert gyre_config_entry.state is ConfigEntryState.LOADED


# ---------------------------------------------------------------------------
# Entity registration verification
# ---------------------------------------------------------------------------

class TestEntityRegistration:

    async def test_gyre_entities_registered(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """Gyre setup creates the expected switch and sensor entities."""
        await setup_integration(hass, gyre_config_entry)

        entity_registry = hass.helpers.entity_registry.async_get()
        # Look up entities by unique_id prefix
        entries = [
            e for e in entity_registry.entities.values()
            if e.config_entry_id == gyre_config_entry.entry_id
        ]
        unique_ids = {e.unique_id for e in entries}

        # Switch
        assert "192.168.1.100:12416_power" in unique_ids

        # Gyre sensors
        assert "192.168.1.100:12416_mode" in unique_ids
        assert "192.168.1.100:12416_ch1_rpm" in unique_ids
        assert "192.168.1.100:12416_ch2_rpm" in unique_ids
        assert "192.168.1.100:12416_ch1_voltage" in unique_ids
        assert "192.168.1.100:12416_ch2_voltage" in unique_ids
        assert "192.168.1.100:12416_ch1_power" in unique_ids
        assert "192.168.1.100:12416_ch2_power" in unique_ids
        assert "192.168.1.100:12416_timestamp" in unique_ids
        assert "192.168.1.100:12416_feed_duration" in unique_ids
        assert "192.168.1.100:12416_model_a" in unique_ids
        assert "192.168.1.100:12416_model_b" in unique_ids
        assert "192.168.1.100:12416_wash_reminder" in unique_ids


# ---------------------------------------------------------------------------
# ICV6 setup
# ---------------------------------------------------------------------------

class TestICV6Setup:

    async def test_icv6_hub_device_registered(
        self,
        hass: HomeAssistant,
    ) -> None:
        """ICV6 setup registers the hub device before child devices are created."""
        icv6_config_entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                "host": "192.168.50.247",
                "port": 4196,
                CONF_DEVICE_PROTOCOL: DEVICE_PROTOCOL_ICV6,
            },
            unique_id="icv6_192.168.50.247",
            title="ICV6 192.168.50.247",
            version=1,
        )

        # Mock the ICV6Client
        with patch(
            "custom_components.maxspect.icv6_coordinator.ICV6Client",
        ) as mock_icv6_cls:
            mock_client = AsyncMock()
            mock_client.async_validate_connection = AsyncMock()
            mock_client.async_discover_devices = AsyncMock(return_value=[])
            mock_icv6_cls.return_value = mock_client

            await setup_integration(hass, icv6_config_entry)

            assert icv6_config_entry.state is ConfigEntryState.LOADED

            # Verify the hub device was registered
            device_registry = hass.helpers.device_registry.async_get()
            hub_device = device_registry.async_get_device(
                identifiers={(DOMAIN, "icv6_192.168.50.247")}
            )

            assert hub_device is not None
            assert hub_device.name == "ICV6 Hub (192.168.50.247)"
            assert hub_device.manufacturer == "Maxspect"
            assert hub_device.model == "ICV6 Controller"
            assert hub_device.config_entries == {icv6_config_entry.entry_id}
