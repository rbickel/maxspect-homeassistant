"""The Maxspect integration for Home Assistant."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .api import MaxspectConnectionError
from .cloud import GizwitsCloudError
from .const import CONF_DEVICE_PROTOCOL, DEVICE_PROTOCOL_ICV6, DOMAIN
from .coordinator import MaxspectCoordinator
from .icv6_coordinator import ICV6Coordinator

import logging

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SWITCH, Platform.SENSOR]

type MaxspectConfigEntry = ConfigEntry[MaxspectCoordinator | ICV6Coordinator]


async def async_setup_entry(hass: HomeAssistant, entry: MaxspectConfigEntry) -> bool:
    """Set up Maxspect from a config entry."""

    if entry.data.get(CONF_DEVICE_PROTOCOL) == DEVICE_PROTOCOL_ICV6:
        return await _async_setup_icv6(hass, entry)

    return await _async_setup_gizwits(hass, entry)


# ---------------------------------------------------------------------------
# ICV6 setup
# ---------------------------------------------------------------------------

async def _async_setup_icv6(
    hass: HomeAssistant, entry: MaxspectConfigEntry
) -> bool:
    """Set up an ICV6 hub entry."""
    coordinator = ICV6Coordinator(hass, entry)

    # Initial discovery — raises ConfigEntryNotReady if the hub is unreachable
    # or no devices are found.
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        raise ConfigEntryNotReady(
            f"ICV6 at {coordinator.host} is not ready: {err}"
        ) from err

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


# ---------------------------------------------------------------------------
# Gizwits setup (unchanged)
# ---------------------------------------------------------------------------

async def _async_setup_gizwits(
    hass: HomeAssistant, entry: MaxspectConfigEntry
) -> bool:
    """Set up a Gizwits (LAN + Cloud) device entry."""
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
        _LOGGER.warning("Cloud login failed (control disabled): %s", err)

    try:
        await coordinator.async_seed_from_cloud()
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Cloud seeding failed, will rely on LAN data")

    coordinator.async_set_updated_data(coordinator.client.state)

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


# ---------------------------------------------------------------------------
# Unload
# ---------------------------------------------------------------------------

async def async_unload_entry(hass: HomeAssistant, entry: MaxspectConfigEntry) -> bool:
    """Unload a Maxspect config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = entry.runtime_data
        if isinstance(coordinator, ICV6Coordinator):
            pass  # ICV6 connections are stateless (new socket per request)
        else:
            await coordinator.client.async_disconnect()
            if coordinator.cloud is not None:
                await coordinator.cloud.async_close()
    return unload_ok
