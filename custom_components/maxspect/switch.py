"""Switch platform for Maxspect integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MaxspectConfigEntry
from .const import (
    DEVICE_TYPE_AQUARIUM_20,
    DEVICE_TYPE_AQUARIUM_SYS,
    DEVICE_TYPE_GYRE,
    DEVICE_TYPE_LED_6CH,
    DEVICE_TYPE_LED_8CH,
    DEVICE_TYPE_LED_E8,
)
from .entity import MaxspectEntity
from .coordinator import MaxspectCoordinator

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
    async_add_entities([MaxspectPowerSwitch(coordinator)])


class MaxspectPowerSwitch(MaxspectEntity, SwitchEntity):
    """Power switch for any Maxspect device."""

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
