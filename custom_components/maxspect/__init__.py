"""The Maxspect integration for Home Assistant."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .api import MaxspectConnectionError
from .cloud import GizwitsCloudError
from .const import DOMAIN
from .coordinator import MaxspectCoordinator

import logging

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SWITCH, Platform.SENSOR]

type MaxspectConfigEntry = ConfigEntry[MaxspectCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: MaxspectConfigEntry) -> bool:
    """Set up Maxspect from a config entry."""
    coordinator = MaxspectCoordinator(hass, entry)

    try:
        await coordinator.client.async_connect()
    except MaxspectConnectionError as err:
        raise ConfigEntryNotReady(
            f"Cannot connect to {coordinator.client.host}: {err}"
        ) from err

    try:
        await coordinator.async_cloud_login()
    except Exception as err:  # noqa: BLE001
        # Cloud login failure is not fatal — LAN sensors still work
        _LOGGER.warning("Cloud login failed (control disabled): %s", err)

    # Seed state from cloud so sensors have values immediately
    try:
        await coordinator.async_seed_from_cloud()
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Cloud seeding failed, will rely on LAN data")

    # Write configured pump models to device so it stays in sync
    try:
        await coordinator.async_sync_models_to_device()
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Model sync to device failed, will retry later")

    # Seed coordinator.data so entities can be created immediately
    coordinator.async_set_updated_data(coordinator.client.state)

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: MaxspectConfigEntry) -> bool:
    """Unload a Maxspect config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.client.async_disconnect()
        if entry.runtime_data.cloud is not None:
            await entry.runtime_data.cloud.async_close()
    return unload_ok
