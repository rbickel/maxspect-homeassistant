"""Base entities for Maxspect integration."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo, CONNECTION_NETWORK_MAC, format_mac
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_CLOUD_DEVICE_NAME,
    CONF_CLOUD_PRODUCT_KEY,
    DOMAIN,
    PRODUCT_KEY_TO_MODEL_NAME,
)
from .coordinator import MaxspectCoordinator
from .gyre_program import decode_serial_number


class MaxspectEntity(CoordinatorEntity[MaxspectCoordinator]):
    """Base class for Gizwits-based Maxspect entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: MaxspectCoordinator) -> None:
        super().__init__(coordinator)
        host = coordinator.client.host
        device_id = coordinator.config_entry.unique_id or host
        pk = coordinator.config_entry.data.get(CONF_CLOUD_PRODUCT_KEY, "")
        model = PRODUCT_KEY_TO_MODEL_NAME.get(pk, "Maxspect device")
        options = coordinator.config_entry.options
        if pk == "cd01d1f3ab2647ea9da51e045cf53d61" and options.get("model_a") == options.get("model_b"):
            model = {0: "Gyre XF330CE", 1: "Gyre XF350CE"}.get(options.get("model_a"), model)
        cloud_name = coordinator.config_entry.data.get(CONF_CLOUD_DEVICE_NAME, "")
        device_name = f"Maxspect {cloud_name}" if cloud_name else f"Maxspect {host}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=device_name,
            manufacturer="Maxspect",
            model=model,
        )
        serial = decode_serial_number(coordinator.client.state.generic_attrs.get("Serial_Number"))
        if serial:
            self._attr_device_info["serial_number"] = serial
        mac = options.get("device_mac")
        if mac:
            self._attr_device_info["connections"] = {(CONNECTION_NETWORK_MAC, format_mac(mac))}
        if version := options.get("firmware_version"):
            self._attr_device_info["sw_version"] = version


class MaxspectReportedEntity(MaxspectEntity):
    """Hide unreported diagnostics while keeping them enabled for future reports."""

    @property
    def report_missing(self) -> bool:
        return self.coordinator.data.generic_attrs.get(self._key) is None

    @callback
    def _sync_visibility(self) -> None:
        registry = er.async_get(self.hass)
        entry = registry.async_get(self.entity_id)
        if entry is None or entry.hidden_by == er.RegistryEntryHider.USER:
            return
        hidden = er.RegistryEntryHider.INTEGRATION if self.report_missing else None
        if entry.hidden_by != hidden:
            registry.async_update_entity(self.entity_id, hidden_by=hidden)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._sync_visibility()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._sync_visibility()
        super()._handle_coordinator_update()


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

        child = coordinator.data.get(device_id)
        child_type_name = child.type_name if child is not None else "Device"
        info = DeviceInfo(
            identifiers={(DOMAIN, child_id)},
            name=f"{child_type_name} ({device_id})",
            manufacturer="Maxspect",
            model=child_type_name,
            via_device=(DOMAIN, hub_id),
        )
        if child is not None:
            if child.serial_number:
                info["serial_number"] = child.serial_number
            if child.hw_version:
                info["hw_version"] = child.hw_version
        self._attr_device_info = info

    @property
    def child_device(self) -> "ICV6ChildDevice | None":
        """Return the current state for this child device."""
        return self.coordinator.data.get(self._device_id)

    @property
    def available(self) -> bool:
        """Mark unavailable if the coordinator failed or the device is gone."""
        return (
            super().available
            and self._device_id in self.coordinator.data
        )
