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
from .const import (
    CONF_MAX_BACKOFF_MULTIPLIER,
    CONF_UNAVAILABLE_AFTER_FAILURES,
    DEFAULT_MAX_BACKOFF_MULTIPLIER,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_UNAVAILABLE_AFTER_FAILURES,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# How often (seconds) to re-run full device discovery.
# Between discovery cycles only device state (mode/channels) is polled.
_REDISCOVER_INTERVAL = 300.0


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

        # Back-off and availability tracking configuration
        self._max_backoff_multiplier: int = entry.options.get(
            CONF_MAX_BACKOFF_MULTIPLIER, DEFAULT_MAX_BACKOFF_MULTIPLIER
        )
        self._unavailable_after: int = entry.options.get(
            CONF_UNAVAILABLE_AFTER_FAILURES, DEFAULT_UNAVAILABLE_AFTER_FAILURES
        )
        self._base_interval: float = DEFAULT_SCAN_INTERVAL

        # Per-device failure tracking: device_id → (failure_count, last_attempt_time, is_unavailable)
        self._device_failures: dict[str, tuple[int, float, bool]] = {}

    # ------------------------------------------------------------------
    # Back-off and availability tracking helpers
    # ------------------------------------------------------------------

    def _get_backoff_interval(self, failure_count: int) -> float:
        """Calculate effective poll interval based on consecutive failures.

        Returns the base interval multiplied by a power of 2, capped by max_backoff_multiplier.
        Examples (base=30s):
          0-2 failures → 30s (1×)
          3-5 failures → 60s (2×)
          6-10 failures → 120s (4×)
          >10 failures → 240s (8×, if max=8)
        """
        if failure_count <= 2:
            return self._base_interval
        # Calculate multiplier: 2^(tier) where tier = (failures - 3) // 3
        tier = (failure_count - 3) // 3
        multiplier = 2 ** (tier + 1)
        multiplier = min(multiplier, self._max_backoff_multiplier)
        return self._base_interval * multiplier

    def _should_poll_device(self, device_id: str, now: float) -> bool:
        """Check if enough time has passed to poll this device based on its back-off interval."""
        if device_id not in self._device_failures:
            return True
        failure_count, last_attempt, _ = self._device_failures[device_id]
        if failure_count == 0:
            return True
        interval = self._get_backoff_interval(failure_count)
        return (now - last_attempt) >= interval

    def _record_device_failure(self, device_id: str, now: float) -> None:
        """Record a device read failure and update back-off state."""
        if device_id in self._device_failures:
            failure_count, _, was_unavailable = self._device_failures[device_id]
            failure_count += 1
        else:
            failure_count = 1
            was_unavailable = False

        # Check if we just crossed the unavailability threshold
        is_unavailable = failure_count >= self._unavailable_after
        self._device_failures[device_id] = (failure_count, now, is_unavailable)

        # Log appropriately based on failure count
        backoff_interval = self._get_backoff_interval(failure_count)
        if failure_count == 1:
            _LOGGER.warning(
                "ICV6: no data returned from %s — device may be off or unreachable",
                device_id,
            )
        else:
            _LOGGER.debug(
                "ICV6: no data from %s (failure %d, backing off to %.0f s)",
                device_id, failure_count, backoff_interval,
            )

    def _record_device_success(self, device_id: str) -> None:
        """Record a successful device read and reset back-off state."""
        if device_id not in self._device_failures:
            # First successful read, no previous failures
            self._device_failures[device_id] = (0, 0.0, False)
            return

        failure_count, _, was_unavailable = self._device_failures[device_id]
        if failure_count > 0:
            _LOGGER.info(
                "ICV6: device %s back online after %d consecutive failures",
                device_id, failure_count,
            )
        # Reset failure state
        self._device_failures[device_id] = (0, 0.0, False)

    def is_device_unavailable(self, device_id: str) -> bool:
        """Check if a device is marked as unavailable due to consecutive failures."""
        if device_id not in self._device_failures:
            return False
        _, _, is_unavailable = self._device_failures[device_id]
        return is_unavailable

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

            # Check if we should poll this device based on back-off interval
            if not self._should_poll_device(device_id, now):
                _LOGGER.debug(
                    "ICV6: skipping %s due to back-off (will retry later)",
                    device_id,
                )
                continue

            try:
                state = await self.client.async_read_device(
                    device_id, dev.proto_cmd, dev.num_channels
                )
            except ICV6ConnectionError as err:
                _LOGGER.warning("ICV6: failed to read %s: %s", device_id, err)
                self._record_device_failure(device_id, now)
                continue

            if state is None:
                self._record_device_failure(device_id, now)
                continue

            # Successful read — update device state and reset failure tracking
            self._record_device_success(device_id)
            dev.mode = state.get("mode", dev.mode)
            dev.manual_channels = state.get("manual_channels", dev.manual_channels)
            dev.schedule = state.get("schedule", dev.schedule)

        return devices

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
