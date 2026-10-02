"""Gyre connection, error and diagnostic flags."""

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory

from .const import DEVICE_TYPE_GYRE, GYRE_DP_NAMES, GYRE_INTERNAL_ONLY_DPS
from .coordinator import MaxspectCoordinator
from .entity import MaxspectReportedEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    if isinstance(coordinator, MaxspectCoordinator) and coordinator.device_type == DEVICE_TYPE_GYRE:
        async_add_entities(
            MaxspectDatapointBinarySensor(coordinator, dp)
            for dp in range(17) if dp not in GYRE_INTERNAL_ONLY_DPS
        )


class MaxspectDatapointBinarySensor(MaxspectReportedEntity, BinarySensorEntity):
    """Expose a reported boolean without inventing values for absent fields."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, dp: int) -> None:
        super().__init__(coordinator)
        self._dp = dp
        self._key = GYRE_DP_NAMES[dp]
        base = coordinator.config_entry.unique_id or coordinator.client.host
        self._attr_unique_id = f"{base}_dp_{dp}"
        self._attr_name = self._key.replace("_", " ")
        if dp in (3, 4, 5, 6):
            self._attr_entity_category = None
        if dp in (3, 4):
            self._attr_name = f"Pump {'A' if dp == 3 else 'B'} connected"
            self._attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
        elif dp in (5, 6):
            self._attr_name = f"Pump {'A' if dp == 5 else 'B'} error"
            self._attr_device_class = BinarySensorDeviceClass.PROBLEM

    @property
    def report_missing(self) -> bool:
        if self._dp in (3, 4, 5, 6):
            return False
        return super().report_missing

    @property
    def is_on(self):
        value = self.coordinator.data.generic_attrs.get(self._key)
        if value is None:
            return None
        return not bool(value) if self._dp in (3, 4) else bool(value)

    @property
    def extra_state_attributes(self):
        return {"datapoint_id": self._dp, "datapoint_name": self._key}
