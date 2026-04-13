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
from .icv6_api import ICV6ConnectionError
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
    """Set up an ICV6 hub entry.

    Only a quick TCP reachability check is performed here so that HA startup
    is not delayed by the slow serial-bus discovery (up to ~35 s on a cold bus).
    Discovery runs on the first regular coordinator poll in the background.
    Entities are added dynamically as devices are found.
    """
    coordinator = ICV6Coordinator(hass, entry)

    # Quick reachability check — fail fast if hub is completely unreachable.
    try:
        await coordinator.client.async_validate_connection()
    except ICV6ConnectionError as err:
        raise ConfigEntryNotReady(
            f"ICV6 at {coordinator.host} is not reachable: {err}"
        ) from err

    # Seed coordinator with empty data so platforms can register listeners
    # before the first refresh completes.
    coordinator.async_set_updated_data({})

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Kick off the first real refresh (discovery + state poll) in the background
    # so it doesn't block HA startup.  Entities are added dynamically when data
    # arrives via coordinator listeners in sensor.py / switch.py.
    entry.async_create_background_task(
        hass,
        coordinator.async_refresh(),
        "icv6_initial_discovery",
    )

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
