"""Feeding controls for Gyre pumps."""

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MaxspectConfigEntry
from .const import DEVICE_TYPE_GYRE, MODE_EXIT_FEED, MODE_FEED
from .coordinator import MaxspectCoordinator
from .entity import MaxspectEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MaxspectConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    if not isinstance(coordinator, MaxspectCoordinator) or coordinator.device_type != DEVICE_TYPE_GYRE:
        return
    async_add_entities([
        MaxspectRefreshButton(coordinator),
        MaxspectFeedingButton(coordinator, MODE_FEED, "start_feeding", "mdi:fish"),
        MaxspectFeedingButton(coordinator, MODE_EXIT_FEED, "resume_pumps", "mdi:play"),
    ])


class MaxspectFeedingButton(MaxspectEntity, ButtonEntity):
    """Start the controller's feeding timer or resume normal operation."""

    def __init__(self, coordinator: MaxspectCoordinator, mode: int, key: str, icon: str) -> None:
        super().__init__(coordinator)
        self._mode = mode
        self._attr_translation_key = key
        self._attr_icon = icon
        base = coordinator.config_entry.unique_id or coordinator.client.host
        self._attr_unique_id = f"{base}_{key}"

    async def async_press(self) -> None:
        await self.coordinator.async_set_mode(self._mode)


class MaxspectRefreshButton(MaxspectEntity, ButtonEntity):
    """Request program/settings without writing to the pumps."""

    _attr_translation_key = "refresh_program"
    _attr_icon = "mdi:refresh"

    def __init__(self, coordinator: MaxspectCoordinator) -> None:
        super().__init__(coordinator)
        base = coordinator.config_entry.unique_id or coordinator.client.host
        self._attr_unique_id = f"{base}_refresh_program"

    async def async_press(self) -> None:
        await self.coordinator.async_refresh_program()
