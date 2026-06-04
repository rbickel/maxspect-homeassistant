"""Data coordinator for the Maxspect ICV6 hub and its child devices."""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .icv6_api import (
    ICV6ChildDevice,
    ICV6Client,
    ICV6ConnectionError,
    ICV6_TCP_PORT,
)
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

# How often (seconds) to re-run full device discovery.
# Between discovery cycles only device state (mode/channels) is polled.
_REDISCOVER_INTERVAL = 300.0

# Exponential back-off configuration for unreachable devices
_BACKOFF_THRESHOLD = 3  # Number of consecutive failures before enabling back-off
_BACKOFF_MAX_MULTIPLIER = 8  # Maximum back-off multiplier (8x discovery interval)


class ICV6Coordinator(DataUpdateCoordinator[dict[str, ICV6ChildDevice]]):
    """Coordinator that manages all ICV6 child devices.

    coordinator.data is a dict keyed by device_id → ICV6ChildDevice.

    IMPORTANT: The ICV6 serial bus and its child devices (LED ramps, pumps)
    have very limited resources.  Aggressive polling with the full
    getAllData (0x14) command causes firmware instability — the device
    becomes unresponsive and its stored schedule can get corrupted.

    Strategy:
      - Full device reads (0x14) are performed ONLY during discovery
        cycles (every _REDISCOVER_INTERVAL seconds).
      - Between discoveries the cached state is returned as-is.
      - The schedule/manual channels rarely change (only through the
        Maxspect app), so caching is safe.
      - Devices that are persistently unreachable trigger exponential
        back-off to reduce log spam and unnecessary polling.
    """

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_icv6",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
            config_entry=entry,
        )
        self.host: str = entry.data[CONF_HOST]
        self.port: int = entry.data.get(CONF_PORT, ICV6_TCP_PORT)
        self.client = ICV6Client(self.host, self.port)
        self._last_discovery: float = 0.0
        # Track consecutive failures per device for exponential back-off
        self._device_failures: dict[str, int] = {}
        # Track when each device was last attempted
        self._device_last_attempt: dict[str, float] = {}

    # ------------------------------------------------------------------
    # DataUpdateCoordinator interface
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, ICV6ChildDevice]:
        """Fetch latest state; re-discover devices periodically.

        Full device reads (getAllData 0x14) are ONLY performed during
        discovery cycles to avoid stressing the ICV6 serial bus and
        its child devices.  Between discoveries the cached state is
        returned unchanged.
        """
        now = time.monotonic()
        needs_discovery = (
            not self.data
            or (now - self._last_discovery) >= _REDISCOVER_INTERVAL
        )

        if not needs_discovery:
            # Return cached state — no bus traffic
            return dict(self.data)

        _LOGGER.debug("ICV6 running device discovery for %s", self.host)
        try:
            discovered = await self.client.async_discover_devices()
        except ICV6ConnectionError as err:
            raise UpdateFailed(f"ICV6 discovery failed: {err}") from err

        if not discovered and not self.data:
            raise UpdateFailed(
                f"No ICV6 devices found at {self.host}. "
                "Ensure devices are connected and powered on."
            )

        current: dict[str, ICV6ChildDevice] = dict(self.data or {})
        for dev in discovered:
            if dev.device_id not in current:
                _LOGGER.info(
                    "ICV6: new child device found: %s (%s)",
                    dev.device_id, dev.type_name,
                )
                current[dev.device_id] = dev
            else:
                # Preserve runtime state but update discovery attrs
                existing = current[dev.device_id]
                existing.area = dev.area
                existing.is_on = dev.is_on
                existing.mode = dev.mode
                existing.group_num = dev.group_num

        self._last_discovery = now
        devices = current

        # Full device read — only during discovery cycles
        for device_id, dev in devices.items():
            if dev.num_channels == 0:
                continue

            # Implement exponential back-off for persistently unreachable devices
            if not self._should_poll_device(device_id, now):
                continue

            try:
                state = await self.client.async_read_device(
                    device_id, dev.proto_cmd, dev.num_channels
                )
            except ICV6ConnectionError as err:
                self._record_device_failure(device_id, now)
                failure_count = self._device_failures.get(device_id, 0)
                if failure_count == _BACKOFF_THRESHOLD:
                    _LOGGER.warning(
                        "ICV6: device %s unreachable after %d attempts, "
                        "enabling exponential back-off to reduce polling frequency",
                        device_id, failure_count,
                    )
                elif failure_count < _BACKOFF_THRESHOLD:
                    _LOGGER.warning("ICV6: failed to read %s: %s", device_id, err)
                continue

            if state is None:
                self._record_device_failure(device_id, now)
                failure_count = self._device_failures.get(device_id, 0)
                if failure_count == _BACKOFF_THRESHOLD:
                    _LOGGER.warning(
                        "ICV6: device %s unreachable (no data) after %d attempts, "
                        "enabling exponential back-off to reduce polling frequency",
                        device_id, failure_count,
                    )
                elif failure_count < _BACKOFF_THRESHOLD:
                    _LOGGER.warning(
                        "ICV6: no data returned from %s — device may be off or unreachable",
                        device_id,
                    )
                continue

            # Device successfully responded — reset failure tracking
            self._record_device_success(device_id)

            dev.mode = state.get("mode", dev.mode)
            dev.manual_channels = state.get("manual_channels", dev.manual_channels)
            dev.schedule = state.get("schedule", dev.schedule)

        return devices

    # ------------------------------------------------------------------
    # Exponential back-off helpers
    # ------------------------------------------------------------------

    def _should_poll_device(self, device_id: str, now: float) -> bool:
        """Check if device should be polled based on back-off state.

        Returns True if device should be polled, False if in back-off period.
        """
        failure_count = self._device_failures.get(device_id, 0)

        # No failures or below threshold — poll normally
        if failure_count < _BACKOFF_THRESHOLD:
            return True

        # Calculate back-off interval using exponential strategy
        # Base interval is _REDISCOVER_INTERVAL (300s)
        # Back-off multiplier: min(2^(failures - threshold), max_multiplier)
        backoff_exp = failure_count - _BACKOFF_THRESHOLD
        multiplier = min(2 ** backoff_exp, _BACKOFF_MAX_MULTIPLIER)
        backoff_interval = _REDISCOVER_INTERVAL * multiplier

        last_attempt = self._device_last_attempt.get(device_id, 0.0)
        time_since_attempt = now - last_attempt

        if time_since_attempt >= backoff_interval:
            _LOGGER.debug(
                "ICV6: retrying device %s after %d s back-off (failure count: %d)",
                device_id, int(backoff_interval), failure_count,
            )
            return True

        return False

    def _record_device_failure(self, device_id: str, now: float) -> None:
        """Record a device polling failure for back-off tracking."""
        self._device_failures[device_id] = self._device_failures.get(device_id, 0) + 1
        self._device_last_attempt[device_id] = now

    def _record_device_success(self, device_id: str) -> None:
        """Record successful device polling and reset back-off state."""
        previous_failures = self._device_failures.get(device_id, 0)
        if previous_failures >= _BACKOFF_THRESHOLD:
            _LOGGER.info(
                "ICV6: device %s recovered after %d failures, "
                "resuming normal polling frequency",
                device_id, previous_failures,
            )

        # Reset failure tracking
        self._device_failures.pop(device_id, None)
        self._device_last_attempt.pop(device_id, None)

    # ------------------------------------------------------------------
    # Control helpers
    # ------------------------------------------------------------------

    async def async_set_power(self, device_id: str, on: bool) -> None:
        """Turn a child device on or off and optimistically update state."""
        dev = self.data.get(device_id)
        if dev is None:
            _LOGGER.error("ICV6: async_set_power called for unknown device %s", device_id)
            return

        ok = await self.client.async_set_power(device_id, dev.proto_cmd, on)
        if not ok:
            _LOGGER.warning("ICV6: power command failed for %s", device_id)
            return

        dev.is_on = on
        self.async_set_updated_data(dict(self.data))

    async def async_set_brightness(self, device_id: str,
                                   channels: list[int]) -> None:
        """Set LED channel brightness (0-100 %) and optimistically update state."""
        dev = self.data.get(device_id)
        if dev is None:
            _LOGGER.error("ICV6: async_set_brightness called for unknown device %s", device_id)
            return

        ok = await self.client.async_set_brightness(device_id, dev.proto_cmd, channels)
        if not ok:
            _LOGGER.warning("ICV6: brightness command failed for %s", device_id)
            return

        dev.manual_channels = channels
        self.async_set_updated_data(dict(self.data))
