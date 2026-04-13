"""Switch platform for Maxspect integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MaxspectConfigEntry
from .const import (
    CONF_DEVICE_PROTOCOL,
    DEVICE_PROTOCOL_ICV6,
    DEVICE_TYPE_AQUARIUM_20,
    DEVICE_TYPE_AQUARIUM_SYS,
    DEVICE_TYPE_GYRE,
    DEVICE_TYPE_LED_6CH,
    DEVICE_TYPE_LED_8CH,
    DEVICE_TYPE_LED_E8,
)
from .coordinator import MaxspectCoordinator
from .entity import ICV6Entity, MaxspectEntity
from .icv6_coordinator import ICV6Coordinator

_LOGGER = logging.getLogger(__name__)

_TRANSLATION_KEY_BY_DEVICE_TYPE: dict[str, str] = {
    DEVICE_TYPE_GYRE:         "pump_power",
    DEVICE_TYPE_LED_6CH:      "light_power",
    DEVICE_TYPE_LED_8CH:      "light_power",
    DEVICE_TYPE_LED_E8:       "light_power",
    DEVICE_TYPE_AQUARIUM_20:  "power",
    DEVICE_TYPE_AQUARIUM_SYS: "power",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MaxspectConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data

    # ── ICV6 path ────────────────────────────────────────────────────────
    if entry.data.get(CONF_DEVICE_PROTOCOL) == DEVICE_PROTOCOL_ICV6:
        assert isinstance(coordinator, ICV6Coordinator)
        known_ids: set[str] = set()

        @callback
        def _add_new_icv6_switches() -> None:
            """Add switch entities for any newly discovered ICV6 devices."""
            new_switches = [
                ICV6PowerSwitch(coordinator, device_id)
                for device_id in coordinator.data
                if device_id not in known_ids
            ]
            for sw in new_switches:
                known_ids.add(sw._device_id)  # noqa: SLF001
            if new_switches:
                async_add_entities(new_switches)

        _add_new_icv6_switches()
        entry.async_on_unload(coordinator.async_add_listener(_add_new_icv6_switches))
        return

    # ── Gizwits path ─────────────────────────────────────────────────────
    assert isinstance(coordinator, MaxspectCoordinator)
    async_add_entities([MaxspectPowerSwitch(coordinator)])


# ---------------------------------------------------------------------------
# Gizwits power switch (unchanged)
# ---------------------------------------------------------------------------

class MaxspectPowerSwitch(MaxspectEntity, SwitchEntity):
    """Power switch for any Gizwits-based Maxspect device."""

    def __init__(self, coordinator: MaxspectCoordinator) -> None:
        super().__init__(coordinator)
        config_unique_id = getattr(coordinator.config_entry, "unique_id", None)
        host = coordinator.client.host
        port = getattr(coordinator.client, "port", None)
        unique_base = config_unique_id or (
            f"{host}:{port}" if port is not None else host
        )
        self._attr_unique_id = f"{unique_base}_power"
        self._attr_translation_key = _TRANSLATION_KEY_BY_DEVICE_TYPE.get(
            coordinator.device_type, "pump_power"
        )

    @property
    def is_on(self) -> bool:
        if self.coordinator.data is None:
            return False
        return self.coordinator.data.is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_power(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_power(False)


# ---------------------------------------------------------------------------
# ICV6 power switch
# ---------------------------------------------------------------------------

class ICV6PowerSwitch(ICV6Entity, SwitchEntity):
    """Power switch for a single ICV6 child device (LED ramp or pump)."""

    def __init__(self, coordinator: ICV6Coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        dev = coordinator.data.get(device_id)
        # Pumps → pump_power, everything with channels → light_power
        self._attr_translation_key = "pump_power" if dev and dev.num_channels == 0 else "light_power"
        self._attr_unique_id = f"icv6_{coordinator.host}_{device_id}_power"

    @property
    def is_on(self) -> bool:
        dev = self.child_device
        return dev.is_on if dev else False

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_power(self._device_id, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_power(self._device_id, False)
