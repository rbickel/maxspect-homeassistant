"""Base entity for Maxspect integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import MaxspectCoordinator


class MaxspectEntity(CoordinatorEntity[MaxspectCoordinator]):
    """Base class for Maxspect entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: MaxspectCoordinator) -> None:
        super().__init__(coordinator)
        host = coordinator.client.host
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, host)},
            name=f"Maxspect {host}",
            manufacturer="Maxspect",
            model="Gyre XF330CE",
        )
