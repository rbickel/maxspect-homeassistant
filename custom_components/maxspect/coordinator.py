"""Data coordinator for Maxspect devices."""

from __future__ import annotations

from datetime import timedelta
import logging
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    MaxspectClient,
    MaxspectConnectionError,
    MaxspectDeviceState,
    _parse_compact_telemetry,
    _parse_state_notify,
)
from .cloud import GizwitsCloudClient, GizwitsCloudError
from .const import (
    CONF_CLOUD_DID,
    CONF_CLOUD_PASSWORD,
    CONF_CLOUD_PRODUCT_KEY,
    CONF_CLOUD_REGION,
    CONF_CLOUD_USERNAME,
    DEFAULT_CLOUD_REGION,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEVICE_CONTROL,
    DEVICE_TYPE_GYRE,
    DOMAIN,
    GIZWITS_APP_ID,
    GIZWITS_KNOWN_PRODUCT_KEYS,
    MODE_OFF,
    MODE_ON,
    PRODUCT_KEY_TO_DEVICE_TYPE,
)

_LOGGER = logging.getLogger(__name__)

# Seconds to suppress stale LAN push notifications after a cloud write.
# The device takes 1-5 s to receive and process cloud commands; old
# compact-telemetry pushes arriving in that window would otherwise flip
# is_on back to the pre-write value.
_WRITE_COOLDOWN = 8.0


class MaxspectCoordinator(DataUpdateCoordinator[MaxspectDeviceState]):
    """Coordinator to manage fetching Maxspect device data."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
            config_entry=entry,
        )
        self.client = MaxspectClient(
            host=entry.data[CONF_HOST],
            port=entry.data.get(CONF_PORT, DEFAULT_PORT),
        )
        self.client.set_update_callback(self._on_device_push)

        # Monotonic deadline below which stale LAN pushes are suppressed.
        self._write_lock_until: float = 0.0
        # The mode value written by the last cloud command (used to reapply
        # the optimistic state when a stale LAN push arrives during cooldown).
        self._pending_mode: int = MODE_ON

        # Cloud client for write operations (may be None for legacy entries)
        self.cloud: GizwitsCloudClient | None = None
        self._cloud_did: str = entry.data.get(CONF_CLOUD_DID, "")
        if CONF_CLOUD_USERNAME in entry.data:
            self.cloud = GizwitsCloudClient(
                app_id=GIZWITS_APP_ID,
                username=entry.data[CONF_CLOUD_USERNAME],
                password=entry.data[CONF_CLOUD_PASSWORD],
                region=entry.data.get(CONF_CLOUD_REGION, DEFAULT_CLOUD_REGION),
                session=async_get_clientsession(hass),
            )

    @property
    def device_type(self) -> str:
        """Return the device type derived from the stored product key."""
        pk = self.config_entry.data.get(CONF_CLOUD_PRODUCT_KEY, "")
        return PRODUCT_KEY_TO_DEVICE_TYPE.get(pk, DEVICE_TYPE_GYRE)

    def _on_device_push(self) -> None:
        if self.device_type != DEVICE_TYPE_GYRE:
            # LAN telemetry parsing is Gyre-specific; non-Gyre state comes
            # from cloud seeding only — ignore raw LAN pushes.
            _LOGGER.debug(
                "Ignoring LAN push for non-Gyre device type=%s", self.device_type
            )
            return
        if time.monotonic() < self._write_lock_until:
            state = self.client.state
            if state.mode == self._pending_mode:
                # Device confirmed our write via LAN — lift cooldown early.
                _LOGGER.debug(
                    "Device confirmed mode=%d via LAN, lifting write cooldown",
                    self._pending_mode,
                )
                self._write_lock_until = 0.0
            else:
                # Stale push — re-apply the pending state so the shared state
                # object stays consistent with the optimistic update.
                _LOGGER.debug(
                    "Suppressing stale LAN push (mode=%d), reapplying pending mode=%d",
                    state.mode, self._pending_mode,
                )
                state.mode = self._pending_mode
                state.is_on = self._pending_mode != MODE_OFF
                return
        self.async_set_updated_data(self.client.state)

    async def async_cloud_login(self) -> None:
        """Log in to the cloud and discover the device if needed."""
        if self.cloud is None:
            return
        await self.cloud.async_login()
        if not self._cloud_did:
            self._cloud_did = await self.cloud.async_discover_device(
                known_keys=GIZWITS_KNOWN_PRODUCT_KEYS
            )
        else:
            # Store the known DID so control works without discovery
            self.cloud.did = self._cloud_did

    async def async_set_mode(self, mode: int) -> None:
        """Set the device mode via the cloud API."""
        if self.cloud is None:
            raise GizwitsCloudError("Cloud credentials not configured")
        try:
            await self.cloud.async_set_mode(mode, did=self._cloud_did)
        except GizwitsCloudError as err:
            _LOGGER.error("Cloud control failed: %s", err)
            raise

        # Suppress stale LAN pushes until the device confirms the new mode.
        self._pending_mode = mode
        self._write_lock_until = time.monotonic() + _WRITE_COOLDOWN

        # Optimistic state update so the UI reflects the change immediately.
        state = self.client.state
        state.mode = mode
        state.is_on = mode != MODE_OFF
        self.async_set_updated_data(state)

    async def async_seed_from_cloud(self) -> None:
        """Fetch latest device data from the cloud and seed state."""
        if self.cloud is None:
            return
        try:
            data = await self.cloud.async_get_device_status(did=self._cloud_did)
        except GizwitsCloudError as err:
            _LOGGER.warning("Cloud status fetch failed: %s", err)
            return

        attrs = data.get("attr", {})
        if not attrs:
            _LOGGER.warning(
                "Cloud status for did=%s returned empty attr dict — "
                "device may be offline or not reporting data",
                self._cloud_did,
            )
            return

        state = self.client.state

        if self.device_type == DEVICE_TYPE_GYRE:
            # Compact telemetry (mode, RPM, voltage, power)
            bak24 = attrs.get("Bak24")
            if bak24:
                try:
                    _parse_compact_telemetry(bytes.fromhex(bak24), state)
                except (ValueError, TypeError):
                    _LOGGER.debug("Could not parse cloud Bak24: %s", bak24)

            # Timestamp
            time_hex = attrs.get("Time")
            if time_hex:
                try:
                    _parse_state_notify(bytes.fromhex(time_hex), state)
                except (ValueError, TypeError):
                    _LOGGER.debug("Could not parse cloud Time: %s", time_hex)

            # Scalar config attributes
            for attr_name, field_name in (
                ("Mode", "mode"),
                ("Time_Feed", "feed_duration"),
                ("Model_A", "model_a"),
                ("Model_B", "model_b"),
                ("Wash", "wash_reminder"),
            ):
                val = attrs.get(attr_name)
                if val is not None:
                    setattr(state, field_name, int(val))

            state.is_on = state.mode != MODE_OFF
            _LOGGER.debug("Seeded Gyre state from cloud: mode=%d is_on=%s", state.mode, state.is_on)
        else:
            # Non-Gyre devices: store all cloud attrs and derive is_on/mode
            ctrl = DEVICE_CONTROL.get(self.device_type, {})
            mode_attr = ctrl.get("attr", "Mode")
            off_val = ctrl.get("off", 1)
            _LOGGER.debug(
                "Cloud seed for %s (did=%s): received %d attrs: %s",
                self.device_type, self._cloud_did, len(attrs), attrs,
            )
            state.generic_attrs = dict(attrs)
            val = attrs.get(mode_attr)
            if val is not None:
                state.is_on = int(val) != off_val
                state.mode = int(val)
            _LOGGER.debug(
                "Seeded %s state from cloud: %s=%s is_on=%s generic_attrs keys=%s",
                self.device_type, mode_attr, val, state.is_on,
                list(state.generic_attrs.keys()),
            )

        self.async_set_updated_data(state)

    async def async_set_power(self, on: bool) -> None:
        """Turn the device on or off, routing to the correct cloud command."""
        if self.cloud is None:
            raise GizwitsCloudError("Cloud credentials not configured")

        ctrl = DEVICE_CONTROL.get(self.device_type)
        if ctrl is None:
            _LOGGER.error(
                "Power control not supported for unknown device_type=%s did=%s",
                self.device_type,
                self._cloud_did,
            )
            raise GizwitsCloudError(
                f"Power control not supported for device type: {self.device_type}"
            )
        val = ctrl["on"] if on else ctrl["off"]

        _LOGGER.debug(
            "async_set_power: device_type=%s on=%s → attr=%s value=%s did=%s",
            self.device_type, on, ctrl["attr"], val, self._cloud_did,
        )

        if self.device_type == DEVICE_TYPE_GYRE:
            # Gyre uses the cooldown + optimistic LAN path
            await self.async_set_mode(val)
            return

        try:
            await self.cloud.async_set_attr(ctrl["attr"], val, did=self._cloud_did)
        except GizwitsCloudError as err:
            _LOGGER.error("Cloud control failed: %s", err)
            raise

        state = self.client.state
        state.is_on = on
        state.mode = val
        state.generic_attrs[ctrl["attr"]] = val
        self.async_set_updated_data(state)

    async def _async_update_data(self) -> MaxspectDeviceState:
        if not self.client.connected:
            try:
                await self.client.async_connect()
            except MaxspectConnectionError as err:
                raise UpdateFailed(f"Error connecting: {err}") from err
        if self.device_type != DEVICE_TYPE_GYRE:
            # Non-Gyre devices ignore LAN pushes, so we must poll the cloud
            # on each scan interval to keep channel and mode state current.
            await self.async_seed_from_cloud()
        return self.client.state

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        await self.client.async_disconnect()
        if self.cloud is not None:
            await self.cloud.async_close()
