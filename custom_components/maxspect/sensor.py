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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MaxspectConfigEntry
from .const import (
    AQUARIUM_20_MODE_NAMES,
    CONF_DEVICE_PROTOCOL,
    DEVICE_PROTOCOL_ICV6,
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
from .entity import ICV6Entity, MaxspectEntity
from .icv6_api import ICV6_MODE_NAMES, ICV6_DEVICE_TYPES, compute_current_levels
from .icv6_coordinator import ICV6Coordinator

# Maximum number of schedule point entities pre-created per LED device.
# Slots beyond the actual schedule count return None (unavailable).
MAX_SCHEDULE_POINTS = 10

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MaxspectConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data

    # ── ICV6 path ────────────────────────────────────────────────────────
    if entry.data.get(CONF_DEVICE_PROTOCOL) == DEVICE_PROTOCOL_ICV6:
        assert isinstance(coordinator, ICV6Coordinator)
        known_ids: set[str] = set()

        @callback
        def _add_new_icv6_sensors() -> None:
            """Add sensor entities for any newly discovered ICV6 devices."""
            new_entities: list[SensorEntity] = []
            for device_id, dev in coordinator.data.items():
                if device_id not in known_ids:
                    known_ids.add(device_id)
                    new_entities.extend(_icv6_sensors_for_device(coordinator, device_id, dev))
            if new_entities:
                async_add_entities(new_entities)

        # Run immediately (data may already be populated on reload) and on every update.
        _add_new_icv6_sensors()
        entry.async_on_unload(coordinator.async_add_listener(_add_new_icv6_sensors))
        return

    # ── Gizwits path ─────────────────────────────────────────────────────
    assert isinstance(coordinator, MaxspectCoordinator)
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


# ---------------------------------------------------------------------------
# ICV6 sensor factories
# ---------------------------------------------------------------------------

def _icv6_sensors_for_device(
    coordinator: ICV6Coordinator, device_id: str, dev: object
) -> list[SensorEntity]:
    """Create sensor entities for a single ICV6 child device."""
    entities: list[SensorEntity] = [
        ICV6GroupSensor(coordinator, device_id),
        ICV6DeviceIdSensor(coordinator, device_id),
    ]

    if getattr(dev, "num_channels", 0) == 0:
        # Pumps — no brightness/schedule sensors; only a power switch (switch.py)
        return entities

    entities.append(ICV6ModeSensor(coordinator, device_id))
    entities.append(ICV6SchedulePointsSensor(coordinator, device_id))

    num_ch = dev.num_channels
    for ch in range(1, num_ch + 1):
        entities.append(ICV6ChannelSensor(coordinator, device_id, ch))
        entities.append(ICV6ManualBrightnessSensor(coordinator, device_id, ch))

    for slot in range(1, MAX_SCHEDULE_POINTS + 1):
        entities.append(ICV6ScheduleTimeSensor(coordinator, device_id, slot))
        for ch in range(1, num_ch + 1):
            entities.append(ICV6ScheduleChannelSensor(coordinator, device_id, slot, ch))

    return entities


# ---------------------------------------------------------------------------
# ICV6 sensor entity classes
# ---------------------------------------------------------------------------

class ICV6ModeSensor(ICV6Entity, SensorEntity):
    """Current operating mode of an ICV6 LED device (Manual / Auto Schedule).

    The full schedule is exposed as extra state attributes so automations
    and the HA history panel can access it without needing separate entities.
    """

    _attr_translation_key = "icv6_mode"

    def __init__(self, coordinator: ICV6Coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"icv6_{coordinator.host}_{device_id}_mode"

    @property
    def native_value(self) -> str | None:
        dev = self.child_device
        if dev is None:
            return None
        return ICV6_MODE_NAMES.get(dev.mode, f"Unknown ({dev.mode})")

    @property
    def extra_state_attributes(self) -> dict:
        dev = self.child_device
        if dev is None:
            return {}
        return {
            "schedule": dev.schedule,
            "schedule_points": len(dev.schedule),
        }


class ICV6SchedulePointsSensor(ICV6Entity, SensorEntity):
    """Number of programmed auto-schedule points for an ICV6 LED device.

    The complete schedule (time + per-channel brightness for each point)
    is included as extra state attributes.
    """

    _attr_translation_key = "icv6_schedule_points"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ICV6Coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"icv6_{coordinator.host}_{device_id}_schedule_points"

    @property
    def native_value(self) -> int:
        dev = self.child_device
        if dev is None:
            return 0
        return len(dev.schedule)

    @property
    def extra_state_attributes(self) -> dict:
        dev = self.child_device
        if dev is None:
            return {}
        attrs: dict = {}
        for pt in dev.schedule:
            key = pt["time"]                          # e.g. "12:00"
            attrs[key] = pt["channels"]               # e.g. [40, 60, 60, 60]
        return attrs


class ICV6GroupSensor(ICV6Entity, SensorEntity):
    """Group (zone) number this device belongs to on the ICV6 bus."""

    _attr_translation_key = "icv6_group"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ICV6Coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"icv6_{coordinator.host}_{device_id}_group"

    @property
    def native_value(self) -> int | None:
        dev = self.child_device
        if dev is None:
            return None
        return dev.group_num


class ICV6ChannelSensor(ICV6Entity, SensorEntity):
    """Current brightness for one LED channel (0-100 %).

    In Manual mode: the stored setpoint.
    In Auto Schedule mode: the live interpolated value currently being output.
    Each point in the auto schedule for this channel is included as an
    extra state attribute (keyed by time string, e.g. "12:00": 40).
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "%"

    def __init__(
        self, coordinator: ICV6Coordinator, device_id: str, channel: int
    ) -> None:
        super().__init__(coordinator, device_id)
        self._channel = channel
        # Reuse the existing channel_N translation keys
        self._attr_translation_key = f"channel_{channel}"
        self._attr_unique_id = f"icv6_{coordinator.host}_{device_id}_ch{channel}"

    @property
    def native_value(self) -> int | None:
        dev = self.child_device
        if dev is None:
            return None
        idx = self._channel - 1
        if not dev.manual_channels or idx >= len(dev.manual_channels):
            return None
        current = compute_current_levels(dev.schedule, dev.mode, dev.manual_channels)
        return current[idx]

    @property
    def extra_state_attributes(self) -> dict:
        """Per-channel schedule values keyed by time string, plus the raw manual setpoint."""
        dev = self.child_device
        if dev is None:
            return {}
        idx = self._channel - 1
        attrs: dict = {}
        if dev.manual_channels and idx < len(dev.manual_channels):
            attrs["manual"] = dev.manual_channels[idx]
        for pt in dev.schedule:
            channels = pt.get("channels", [])
            if idx < len(channels):
                attrs[pt["time"]] = channels[idx]
        return attrs


class ICV6ManualBrightnessSensor(ICV6Entity, SensorEntity):
    """Raw manual setpoint for one LED channel (0-100 %).

    Unlike ICV6ChannelSensor which shows the interpolated current output,
    this sensor always shows the stored manual brightness regardless of mode.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "%"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: ICV6Coordinator, device_id: str, channel: int
    ) -> None:
        super().__init__(coordinator, device_id)
        self._channel = channel
        self._attr_translation_key = f"icv6_manual_ch{channel}"
        self._attr_unique_id = f"icv6_{coordinator.host}_{device_id}_manual_ch{channel}"

    @property
    def native_value(self) -> int | None:
        dev = self.child_device
        if dev is None:
            return None
        idx = self._channel - 1
        if not dev.manual_channels or idx >= len(dev.manual_channels):
            return None
        return dev.manual_channels[idx]


class ICV6ScheduleTimeSensor(ICV6Entity, SensorEntity):
    """Time for one schedule point slot (e.g. "10:00").

    Returns None when the slot does not exist in the current schedule.
    Per-channel brightness values for this point are in extra_state_attributes.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: ICV6Coordinator, device_id: str, slot: int
    ) -> None:
        super().__init__(coordinator, device_id)
        self._slot = slot  # 1-based
        self._attr_translation_key = f"icv6_schedule_{slot}_time"
        self._attr_unique_id = f"icv6_{coordinator.host}_{device_id}_sched_{slot}_time"

    @property
    def native_value(self) -> str | None:
        dev = self.child_device
        if dev is None or self._slot > len(dev.schedule):
            return None
        return dev.schedule[self._slot - 1]["time"]

    @property
    def extra_state_attributes(self) -> dict:
        dev = self.child_device
        if dev is None or self._slot > len(dev.schedule):
            return {}
        pt = dev.schedule[self._slot - 1]
        attrs: dict = {}
        for i, val in enumerate(pt.get("channels", []), start=1):
            attrs[f"channel_{i}"] = val
        return attrs


class ICV6ScheduleChannelSensor(ICV6Entity, SensorEntity):
    """Brightness for one channel at one schedule point (0-100 %).

    Returns None when the slot does not exist in the current schedule.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "%"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: ICV6Coordinator,
        device_id: str,
        slot: int,
        channel: int,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._slot = slot  # 1-based
        self._channel = channel  # 1-based
        self._attr_translation_key = f"icv6_schedule_{slot}_ch{channel}"
        self._attr_unique_id = (
            f"icv6_{coordinator.host}_{device_id}_sched_{slot}_ch{channel}"
        )

    @property
    def native_value(self) -> int | None:
        dev = self.child_device
        if dev is None or self._slot > len(dev.schedule):
            return None
        channels = dev.schedule[self._slot - 1].get("channels", [])
        idx = self._channel - 1
        if idx >= len(channels):
            return None
        return channels[idx]


class ICV6DeviceIdSensor(ICV6Entity, SensorEntity):
    """Full device ID string as a diagnostic sensor."""

    _attr_translation_key = "icv6_device_id"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ICV6Coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"icv6_{coordinator.host}_{device_id}_device_id"

    @property
    def native_value(self) -> str | None:
        dev = self.child_device
        if dev is None:
            return None
        return dev.device_id
