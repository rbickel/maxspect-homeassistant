"""Sensor platform for Maxspect integration."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MaxspectConfigEntry
from .const import MODE_NAMES
from .entity import MaxspectEntity
from .coordinator import MaxspectCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MaxspectConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    host = coordinator.client.host

    entities: list[SensorEntity] = [
        MaxspectModeSensor(coordinator, host),
        MaxspectRPMSensor(coordinator, host, 1),
        MaxspectRPMSensor(coordinator, host, 2),
        MaxspectVoltageSensor(coordinator, host, 1),
        MaxspectVoltageSensor(coordinator, host, 2),
        MaxspectPowerSensor(coordinator, host, 1),
        MaxspectPowerSensor(coordinator, host, 2),
        MaxspectTimestampSensor(coordinator, host),
        MaxspectModelSensor(coordinator, host, "a"),
        MaxspectModelSensor(coordinator, host, "b"),
    ]
    async_add_entities(entities)


class MaxspectModeSensor(MaxspectEntity, SensorEntity):
    """Current operational mode."""

    _attr_translation_key = "mode"

    def __init__(self, coordinator: MaxspectCoordinator, host: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{host}_mode"

    @property
    def native_value(self) -> str:
        return MODE_NAMES.get(
            self.coordinator.data.mode,
            f"Unknown ({self.coordinator.data.mode})",
        )


class MaxspectRPMSensor(MaxspectEntity, SensorEntity):
    """Channel RPM sensor."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "rpm"

    def __init__(self, coordinator: MaxspectCoordinator, host: str, channel: int) -> None:
        super().__init__(coordinator)
        self._channel = channel
        self._attr_translation_key = f"ch{channel}_rpm"
        self._attr_unique_id = f"{host}_ch{channel}_rpm"

    @property
    def native_value(self) -> int | None:
        val = self.coordinator.data.ch1_rpm if self._channel == 1 else self.coordinator.data.ch2_rpm
        return val if val > 0 else None


class MaxspectVoltageSensor(MaxspectEntity, SensorEntity):
    """Channel voltage sensor."""

    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT

    def __init__(self, coordinator: MaxspectCoordinator, host: str, channel: int) -> None:
        super().__init__(coordinator)
        self._channel = channel
        self._attr_translation_key = f"ch{channel}_voltage"
        self._attr_unique_id = f"{host}_ch{channel}_voltage"

    @property
    def native_value(self) -> float | None:
        val = self.coordinator.data.ch1_voltage if self._channel == 1 else self.coordinator.data.ch2_voltage
        return round(val, 2) if val > 0 else None


class MaxspectPowerSensor(MaxspectEntity, SensorEntity):
    """Channel power sensor."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, coordinator: MaxspectCoordinator, host: str, channel: int) -> None:
        super().__init__(coordinator)
        self._channel = channel
        self._attr_translation_key = f"ch{channel}_power"
        self._attr_unique_id = f"{host}_ch{channel}_power"

    @property
    def native_value(self) -> int | None:
        val = self.coordinator.data.ch1_power if self._channel == 1 else self.coordinator.data.ch2_power
        return val if val > 0 else None


class MaxspectTimestampSensor(MaxspectEntity, SensorEntity):
    """Device timestamp from state notify."""

    _attr_translation_key = "timestamp"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: MaxspectCoordinator, host: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{host}_timestamp"

    @property
    def native_value(self) -> str | None:
        ts = self.coordinator.data.timestamp
        return ts if ts else None


class MaxspectModelSensor(MaxspectEntity, SensorEntity):
    """Pump model sensor (from config, synced to device via cloud)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: MaxspectCoordinator, host: str, channel: str,
    ) -> None:
        super().__init__(coordinator)
        self._channel = channel
        self._attr_translation_key = f"model_{channel}"
        self._attr_unique_id = f"{host}_model_{channel}"

    @property
    def native_value(self) -> str:
        if self._channel == "a":
            return self.coordinator.model_a_name
        return self.coordinator.model_b_name
