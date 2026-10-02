"""The Maxspect integration for Home Assistant."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import CONF_DEVICE_PROTOCOL, DEVICE_PROTOCOL_ICV6, DEVICE_TYPE_GYRE, DOMAIN, GYRE_INTERNAL_ONLY_DPS
from .coordinator import MaxspectCoordinator
from .icv6_api import ICV6ConnectionError
from .icv6_coordinator import ICV6Coordinator

import logging

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SWITCH, Platform.SENSOR, Platform.BUTTON, Platform.BINARY_SENSOR]

type MaxspectConfigEntry = ConfigEntry[MaxspectCoordinator | ICV6Coordinator]


async def async_setup_entry(hass: HomeAssistant, entry: MaxspectConfigEntry) -> bool:
    """Set up Maxspect from a config entry."""

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    if entry.data.get(CONF_DEVICE_PROTOCOL) == DEVICE_PROTOCOL_ICV6:
        return await _async_setup_icv6(hass, entry)

    return await _async_setup_gizwits(hass, entry)


async def _async_reload_entry(hass: HomeAssistant, entry: MaxspectConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


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

    # Register the ICV6 hub device before any child devices are created.
    # Child devices will reference this hub via via_device.
    device_registry = dr.async_get(hass)
    hub_id = f"icv6_{coordinator.host}"
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, hub_id)},
        name=f"ICV6 Hub ({coordinator.host})",
        manufacturer="Maxspect",
        model="ICV6 Controller",
    )

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
    await coordinator.async_load_settings()

    try:
        state = await coordinator.async_initialize()
    except UpdateFailed as err:
        await coordinator.async_shutdown()
        raise ConfigEntryNotReady(str(err)) from err

    coordinator.async_set_updated_data(state)

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if coordinator.device_type == DEVICE_TYPE_GYRE:
        registry = er.async_get(hass)
        base = entry.unique_id or coordinator.client.host
        retired_ids = {f"{base}_dp_{dp}" for dp in GYRE_INTERNAL_ONLY_DPS}
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
            if entity.platform == DOMAIN and entity.unique_id in retired_ids:
                registry.async_remove(entity.entity_id)
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
            await coordinator.async_shutdown()
    return unload_ok
