"""Base entities for Maxspect integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_CLOUD_DEVICE_NAME,
    CONF_CLOUD_PRODUCT_KEY,
    DOMAIN,
    PRODUCT_KEY_TO_MODEL_NAME,
)
from .coordinator import MaxspectCoordinator


class MaxspectEntity(CoordinatorEntity[MaxspectCoordinator]):
    """Base class for Gizwits-based Maxspect entities."""

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


# ---------------------------------------------------------------------------
# ICV6 base entity
# ---------------------------------------------------------------------------

# Import here to avoid a circular import at module level.
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .icv6_api import ICV6ChildDevice
    from .icv6_coordinator import ICV6Coordinator as _ICV6Coordinator


class ICV6Entity(CoordinatorEntity["_ICV6Coordinator"]):
    """Base class for entities belonging to a child device on an ICV6 hub.

    Each ICV6 child device (LED ramp, pump, …) becomes its own HA device
    linked to the ICV6 hub via *via_device*.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: "_ICV6Coordinator", device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id

        hub_id = f"icv6_{coordinator.host}"
        child_id = f"icv6_{coordinator.host}_{device_id}"

        child = coordinator.data[device_id]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, child_id)},
            name=f"{child.type_name} ({device_id})",
            manufacturer="Maxspect",
            model=child.type_name,
            via_device=(DOMAIN, hub_id),
        )

    @property
    def child_device(self) -> "ICV6ChildDevice":
        """Return the current state for this child device."""
        return self.coordinator.data[self._device_id]

    @property
    def available(self) -> bool:
        """Mark unavailable if the coordinator failed or the device is gone."""
        return (
            super().available
            and self._device_id in self.coordinator.data
        )
