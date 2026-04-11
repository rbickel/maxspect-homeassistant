"""Sensor platform for Maxspect integration."""

from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MaxspectConfigEntry
from .const import (
    AQUARIUM_20_MODE_NAMES,
    DEVICE_TYPE_AQUARIUM_20,
    DEVICE_TYPE_AQUARIUM_SYS,
    DEVICE_TYPE_GYRE,
    DEVICE_TYPE_LED_6CH,
    DEVICE_TYPE_LED_8CH,
    DEVICE_TYPE_LED_E8,
    LED_6CH_MODE_NAMES,
    LED_8CH_MODE_NAMES,
    LED_CHANNEL_COUNT,
    MODE_NAMES,
)
from .coordinator import MaxspectCoordinator
from .entity import MaxspectEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MaxspectConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    unique_base = coordinator.config_entry.unique_id or coordinator.client.host
    dt = coordinator.device_type

    if dt == DEVICE_TYPE_GYRE:
        entities: list[SensorEntity] = _gyre_sensors(coordinator, unique_base)
    elif dt in (DEVICE_TYPE_LED_6CH, DEVICE_TYPE_LED_8CH, DEVICE_TYPE_LED_E8):
        entities = _led_sensors(coordinator, unique_base, dt)
    elif dt == DEVICE_TYPE_AQUARIUM_20:
        entities = _aquarium_20_sensors(coordinator, unique_base)
    elif dt == DEVICE_TYPE_AQUARIUM_SYS:
        entities = _aquarium_sys_sensors(coordinator, unique_base)
    else:
        entities = _gyre_sensors(coordinator, unique_base)

    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Sensor factories per device type
# ---------------------------------------------------------------------------

def _gyre_sensors(coordinator: MaxspectCoordinator, unique_base: str) -> list[SensorEntity]:
    return [
        MaxspectModeSensor(coordinator, unique_base, MODE_NAMES),
        MaxspectRPMSensor(coordinator, unique_base, 1),
        MaxspectRPMSensor(coordinator, unique_base, 2),
        MaxspectVoltageSensor(coordinator, unique_base, 1),
        MaxspectVoltageSensor(coordinator, unique_base, 2),
        MaxspectPowerSensor(coordinator, unique_base, 1),
        MaxspectPowerSensor(coordinator, unique_base, 2),
        MaxspectTimestampSensor(coordinator, unique_base),
        MaxspectFeedDurationSensor(coordinator, unique_base),
        MaxspectModelSensor(coordinator, unique_base, "a"),
        MaxspectModelSensor(coordinator, unique_base, "b"),
        MaxspectWashReminderSensor(coordinator, unique_base),
    ]


def _led_sensors(
    coordinator: MaxspectCoordinator, unique_base: str, device_type: str
) -> list[SensorEntity]:
    mode_names = LED_6CH_MODE_NAMES if device_type == DEVICE_TYPE_LED_6CH else LED_8CH_MODE_NAMES
    n_channels = LED_CHANNEL_COUNT[device_type]
    sensors: list[SensorEntity] = [MaxspectModeSensor(coordinator, unique_base, mode_names)]
    for ch in range(1, n_channels + 1):
        sensors.append(MaxspectChannelSensor(coordinator, unique_base, ch))
    return sensors


def _aquarium_20_sensors(coordinator: MaxspectCoordinator, unique_base: str) -> list[SensorEntity]:
    return [
        MaxspectModeSensor(coordinator, unique_base, AQUARIUM_20_MODE_NAMES),
        MaxspectGenericTempSensor(coordinator, unique_base, "Temperature1", "temperature_1", is_uint16=True),
        MaxspectGenericTempSensor(coordinator, unique_base, "Temperature2", "temperature_2", is_uint16=True),
        MaxspectGenericUint8Sensor(coordinator, unique_base, "Level_Pump", "pump_level"),
        MaxspectGenericUint8Sensor(coordinator, unique_base, "Level_Skimmer", "skimmer_level"),
    ]


def _aquarium_sys_sensors(coordinator: MaxspectCoordinator, unique_base: str) -> list[SensorEntity]:
    return [
        MaxspectGenericTempSensor(coordinator, unique_base, "Temp_051", "temperature_1"),
        MaxspectGenericTempSensor(coordinator, unique_base, "Temp_052", "temperature_2"),
    ]


# ---------------------------------------------------------------------------
# Sensor entity classes
# ---------------------------------------------------------------------------

class MaxspectModeSensor(MaxspectEntity, SensorEntity):
    """Current operational mode — works for all device types."""

    _attr_translation_key = "mode"

    def __init__(
        self,
        coordinator: MaxspectCoordinator,
        unique_base: str,
        mode_names: dict[int, str],
    ) -> None:
        super().__init__(coordinator)
        self._mode_names = mode_names
        self._attr_unique_id = f"{unique_base}_mode"

    @property
    def native_value(self) -> str:
        return self._mode_names.get(
            self.coordinator.data.mode,
            f"Unknown ({self.coordinator.data.mode})",
        )


class MaxspectChannelSensor(MaxspectEntity, SensorEntity):
    """LED light channel brightness (0-100 %)."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "%"

    def __init__(self, coordinator: MaxspectCoordinator, unique_base: str, channel: int) -> None:
        super().__init__(coordinator)
        self._channel = channel
        self._attr_translation_key = f"channel_{channel}"
        self._attr_unique_id = f"{unique_base}_channel_{channel}"

    @property
    def native_value(self) -> int | None:
        val = self.coordinator.data.generic_attrs.get(f"channel_{self._channel}")
        if val is None:
            _LOGGER.debug(
                "channel_%d not in generic_attrs (keys: %s)",
                self._channel, list(self.coordinator.data.generic_attrs.keys()),
            )
            return None
        return int(val)


class MaxspectGenericTempSensor(MaxspectEntity, SensorEntity):
    """Temperature sensor seeded from cloud attrs (value / 2.0 = °C)."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(
        self,
        coordinator: MaxspectCoordinator,
        unique_base: str,
        attr: str,
        translation_key: str,
        is_uint16: bool = False,
    ) -> None:
        super().__init__(coordinator)
        self._attr = attr
        self._is_uint16 = is_uint16
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{unique_base}_{translation_key}"

    @property
    def native_value(self) -> float | None:
        val = self.coordinator.data.generic_attrs.get(self._attr)
        if val is None:
            _LOGGER.debug(
                "%s not in generic_attrs (keys: %s)",
                self._attr, list(self.coordinator.data.generic_attrs.keys()),
            )
            return None
        return round(int(val) / 2.0, 1)


class MaxspectGenericUint8Sensor(MaxspectEntity, SensorEntity):
    """Generic integer sensor seeded from cloud attrs."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: MaxspectCoordinator,
        unique_base: str,
        attr: str,
        translation_key: str,
    ) -> None:
        super().__init__(coordinator)
        self._attr = attr
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{unique_base}_{translation_key}"

    @property
    def native_value(self) -> int | None:
        val = self.coordinator.data.generic_attrs.get(self._attr)
        if val is None:
            _LOGGER.debug(
                "%s not in generic_attrs (keys: %s)",
                self._attr, list(self.coordinator.data.generic_attrs.keys()),
            )
            return None
        return int(val)


# ---------------------------------------------------------------------------
# Gyre-only sensor classes (unchanged)
# ---------------------------------------------------------------------------

class MaxspectRPMSensor(MaxspectEntity, SensorEntity):
    """Channel RPM sensor."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "rpm"

    def __init__(self, coordinator: MaxspectCoordinator, unique_base: str, channel: int) -> None:
        super().__init__(coordinator)
        self._channel = channel
        self._attr_translation_key = f"ch{channel}_rpm"
        self._attr_unique_id = f"{unique_base}_ch{channel}_rpm"

    @property
    def native_value(self) -> int | None:
        val = self.coordinator.data.ch1_rpm if self._channel == 1 else self.coordinator.data.ch2_rpm
        return val if val > 0 else None


class MaxspectVoltageSensor(MaxspectEntity, SensorEntity):
    """Channel voltage sensor."""

    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT

    def __init__(self, coordinator: MaxspectCoordinator, unique_base: str, channel: int) -> None:
        super().__init__(coordinator)
        self._channel = channel
        self._attr_translation_key = f"ch{channel}_voltage"
        self._attr_unique_id = f"{unique_base}_ch{channel}_voltage"

    @property
    def native_value(self) -> float | None:
        val = self.coordinator.data.ch1_voltage if self._channel == 1 else self.coordinator.data.ch2_voltage
        return round(val, 2) if val > 0 else None


class MaxspectPowerSensor(MaxspectEntity, SensorEntity):
    """Channel power sensor."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, coordinator: MaxspectCoordinator, unique_base: str, channel: int) -> None:
        super().__init__(coordinator)
        self._channel = channel
        self._attr_translation_key = f"ch{channel}_power"
        self._attr_unique_id = f"{unique_base}_ch{channel}_power"

    @property
    def native_value(self) -> int | None:
        val = self.coordinator.data.ch1_power if self._channel == 1 else self.coordinator.data.ch2_power
        return val if val > 0 else None


class MaxspectTimestampSensor(MaxspectEntity, SensorEntity):
    """Device timestamp from state notify."""

    _attr_translation_key = "timestamp"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: MaxspectCoordinator, unique_base: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{unique_base}_timestamp"

    @property
    def native_value(self) -> str | None:
        ts = self.coordinator.data.timestamp
        return ts if ts else None


class MaxspectFeedDurationSensor(MaxspectEntity, SensorEntity):
    """Feed duration setting (DP 19, minutes)."""

    _attr_translation_key = "feed_duration"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: MaxspectCoordinator, unique_base: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{unique_base}_feed_duration"

    @property
    def native_value(self) -> int | None:
        val = self.coordinator.data.feed_duration
        return val if val > 0 else None


class MaxspectModelSensor(MaxspectEntity, SensorEntity):
    """Pump model (DP 20/21): 0 = XF 330CE, non-zero = XF 350CE."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: MaxspectCoordinator, unique_base: str, channel: str) -> None:
        super().__init__(coordinator)
        self._channel = channel
        self._attr_translation_key = f"model_{channel}"
        self._attr_unique_id = f"{unique_base}_model_{channel}"

    @property
    def native_value(self) -> str | None:
        val = self.coordinator.data.model_a if self._channel == "a" else self.coordinator.data.model_b
        return "XF 330CE" if val == 0 else "XF 350CE"


class MaxspectWashReminderSensor(MaxspectEntity, SensorEntity):
    """Wash reminder interval (DP 22, days)."""

    _attr_translation_key = "wash_reminder"
    _attr_native_unit_of_measurement = UnitOfTime.DAYS
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: MaxspectCoordinator, unique_base: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{unique_base}_wash_reminder"

    @property
    def native_value(self) -> int | None:
        val = self.coordinator.data.wash_reminder
        return val if val > 0 else None
