"""Switch platform for Maxspect integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MaxspectConfigEntry
from .const import MODE_OFF, MODE_ON
from .entity import MaxspectEntity
from .coordinator import MaxspectCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MaxspectConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([MaxspectPumpSwitch(coordinator)])


class MaxspectPumpSwitch(MaxspectEntity, SwitchEntity):
    """Representation of a Maxspect pump power switch."""

    _attr_translation_key = "pump_power"

    def __init__(self, coordinator: MaxspectCoordinator) -> None:
        super().__init__(coordinator)
        info = coordinator.client
        self._attr_unique_id = f"{info.host}_power"

    @property
    def is_on(self) -> bool:
        if self.coordinator.data is None:
            return False
        return self.coordinator.data.is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_mode(MODE_ON)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_mode(MODE_OFF)
