"""Base entity for Maxspect integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_CLOUD_DEVICE_NAME, CONF_CLOUD_PRODUCT_KEY, DOMAIN, PRODUCT_KEY_TO_MODEL_NAME
from .coordinator import MaxspectCoordinator


class MaxspectEntity(CoordinatorEntity[MaxspectCoordinator]):
    """Base class for Maxspect entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: MaxspectCoordinator) -> None:
        super().__init__(coordinator)
        host = coordinator.client.host
        device_id = coordinator.config_entry.unique_id or host
        pk = coordinator.config_entry.data.get(CONF_CLOUD_PRODUCT_KEY, "")
        model = PRODUCT_KEY_TO_MODEL_NAME.get(pk, "Gyre XF330CE")
        cloud_name = coordinator.config_entry.data.get(CONF_CLOUD_DEVICE_NAME, "")
        device_name = f"Maxspect {cloud_name}" if cloud_name else f"Maxspect {host}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=device_name,
            manufacturer="Maxspect",
            model=model,
        )
