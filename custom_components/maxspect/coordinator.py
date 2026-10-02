"""Data coordinator for Maxspect devices."""

from __future__ import annotations

from datetime import timedelta
import logging
import asyncio
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
from .gyre_program import decode_program, active_entry, decode_serial_number

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    MaxspectClient,
    MaxspectConnectionError,
    MaxspectDeviceState,
    _mode_from_report,
    _mode_matches_request,
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
    DEFAULT_LOCAL_CONTROL,
    DEVICE_CONTROL,
    DEVICE_TYPE_GYRE,
    DOMAIN,
    GIZWITS_APP_ID,
    GIZWITS_KNOWN_PRODUCT_KEYS,
    MODE_OFF,
    MODE_ON,
    MODE_NAMES,
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
            polling=self.device_type == DEVICE_TYPE_GYRE,
        )
        self.client.set_update_callback(self._on_device_push)
        self._program_store = Store(hass, 1, f"maxspect_program_{entry.entry_id}")
        self.saved_settings: dict = {}
        self.settings_received: dict = {}
        self.refresh_status = "Saved values; awaiting device report"
        self._program_refresh_lock = asyncio.Lock()
        self._control_lock = asyncio.Lock()
        self._local_pending_mode: int | None = None
        self._program_generation = 0
        self._cloud_ready = False
        self._cloud_login_lock = asyncio.Lock()

        for dp_id, field_name in ((20, "model_a"), (21, "model_b")):
            model = entry.options.get(field_name, -1)
            if model in (0, 1):
                setattr(self.client.state, field_name, model)
                self.client.state._initialized_models.add(dp_id)
        self.client.state._model_initialized = self.client.state._initialized_models == {20, 21}

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
        self._remember_settings(self.client.last_report_attrs)
        reported_mode = _mode_from_report(self.client.last_report_attrs)
        if (
            self._local_pending_mode is not None and reported_mode is not None
            and _mode_matches_request(self._local_pending_mode, reported_mode)
        ):
            self._pending_mode = self.client.state.mode
            self._write_lock_until = 0.0
        if time.monotonic() < self._write_lock_until:
            state = self.client.state
            if reported_mode is not None and _mode_matches_request(self._pending_mode, reported_mode):
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
        self._cloud_ready = True

    async def _ensure_cloud_ready(self) -> None:
        if self.cloud is None:
            raise GizwitsCloudError("Cloud credentials not configured")
        async with self._cloud_login_lock:
            if not self._cloud_ready:
                await self.async_cloud_login()

    async def async_set_mode(self, mode: int) -> None:
        """Use confirmed LAN control when selected, with cloud fallback."""
        if type(mode) is not int or mode not in MODE_NAMES:
            _LOGGER.error("Unsupported Gyre mode: %r", mode)
            raise ValueError("Unsupported Gyre mode")
        async with self._control_lock:
            await self._async_set_mode(mode)

    async def _async_set_mode(self, mode: int) -> None:
        if self.device_type == DEVICE_TYPE_GYRE and self.config_entry.options.get(
            "local_control", DEFAULT_LOCAL_CONTROL
        ):
            self._local_pending_mode = mode
            try:
                await self.client.async_set_mode(mode)
            except (MaxspectConnectionError, OSError) as err:
                _LOGGER.warning("LAN control failed; trying cloud: %s", err)
            else:
                self._pending_mode = self.client.state.mode
                self._write_lock_until = 0.0
                self.async_set_updated_data(self.client.state)
                return
            finally:
                self._local_pending_mode = None
        if self.cloud is None:
            _LOGGER.error("Cloud control unavailable: credentials not configured")
            raise GizwitsCloudError("Cloud credentials not configured")
        await self._ensure_cloud_ready()
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
        state.generic_attrs["Mode"] = mode
        self.async_set_updated_data(state)

    async def async_seed_from_cloud(self, *, fallback: bool = False) -> None:
        """Fetch latest device data from the cloud and seed state."""
        if self.cloud is None:
            if fallback:
                raise GizwitsCloudError("Cloud fallback is not configured")
            return
        try:
            await self._ensure_cloud_ready()
            data = await self.cloud.async_get_device_status(did=self._cloud_did)
        except GizwitsCloudError as err:
            _LOGGER.warning("Cloud status fetch failed: %s", err)
            if fallback:
                raise
            return

        attrs = data.get("attr", {})
        if not attrs:
            _LOGGER.debug(
                "Cloud status for did=%s returned no new attributes",
                self._cloud_did,
            )
            if fallback:
                raise GizwitsCloudError("Cloud fallback returned no status attributes")
            return

        state = self.client.state
        if self.device_type == DEVICE_TYPE_GYRE:
            attrs = self.client.apply_attributes({
                key: value for key, value in attrs.items()
                if fallback or key not in self.client.received_attribute_names
            })
            self._remember_settings(attrs, fresh=False)
            if fallback and _mode_from_report(attrs) is None:
                raise GizwitsCloudError("Cloud fallback did not return a valid operating mode")
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
            state.generic_attrs.update(attrs)
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

    async def async_initialize(self) -> MaxspectDeviceState:
        try:
            await self.client.async_connect()
        except MaxspectConnectionError as err:
            return await self._async_cloud_fallback(err)
        return await self._async_update_data()

    async def _async_cloud_fallback(self, reason: Exception) -> MaxspectDeviceState:
        _LOGGER.warning("LAN status unavailable; using cloud fallback: %s", reason)
        try:
            await self.async_seed_from_cloud(fallback=True)
        except GizwitsCloudError as err:
            raise UpdateFailed(f"LAN and cloud status unavailable: {reason}; {err}") from err
        return self.client.state

    async def async_load_settings(self) -> None:
        """Restore configuration, never old live telemetry or error flags."""
        saved = await self._program_store.async_load() or {}
        self.saved_settings = saved.get("settings", {})
        self.settings_received = saved.get("received", {})
        self.client.apply_attributes(self.saved_settings)

    def _remember_settings(self, attrs: dict, *, fresh: bool = True) -> None:
        if self.device_type != DEVICE_TYPE_GYRE:
            return
        changed = False
        for key in ("Manual", "Auto", "Time_Feed", "Model_A", "Model_B", "Version_Firmware", "Serial_Number", "Wash"):
            if key not in attrs:
                continue
            value = attrs[key]
            if key in ("Manual", "Auto") and not decode_program(value, scheduled=key == "Auto"):
                _LOGGER.debug("Ignoring invalid or empty %s program", key)
                continue
            ranges = {"Time_Feed": (5, 120), "Model_A": (0, 1), "Model_B": (0, 1),
                      "Wash": (0, 255), "Version_Firmware": (0, 255)}
            if key in ranges and (
                type(value) is not int or not ranges[key][0] <= value <= ranges[key][1]
            ):
                _LOGGER.warning("Not saving invalid %s=%r", key, value)
                continue
            if key == "Serial_Number":
                serial = decode_serial_number(value)
                if serial is None:
                    continue
                registry = dr.async_get(self.hass)
                device = registry.async_get_device(identifiers={
                    (DOMAIN, self.config_entry.unique_id or self.client.host),
                })
                if device is not None and device.serial_number != serial:
                    registry.async_update_device(device.id, serial_number=serial)
            self.saved_settings[key] = value
            if fresh:
                self.settings_received[key] = dt_util.utcnow().isoformat()
                if key == "Auto":
                    self._program_generation += 1
            changed = True
        if changed:
            self._program_store.async_delay_save(
                lambda: {"settings": self.saved_settings, "received": self.settings_received}, 2,
            )

    def current_program_entry(self) -> dict | None:
        state = self.client.state
        if state.mode == 0:
            entries = decode_program(self.saved_settings.get("Manual"), scheduled=False)
            return entries[0] if entries else None
        if state.mode != 1:
            return None
        # Advance the last reported controller clock by elapsed monotonic time.
        clock = self.client.controller_time_now()
        if clock is None:
            clock = dt_util.now()
        return active_entry(decode_program(self.saved_settings.get("Auto"), scheduled=True), clock.hour * 60 + clock.minute)

    async def async_refresh_program(self) -> None:
        """Request a fresh report; never clear saved data on missing responses."""
        async with self._program_refresh_lock:
            before = self._program_generation
            self.refresh_status = "Requesting schedule from controller"
            self.async_set_updated_data(self.client.state)
            try:
                await self.client.async_request_full_status()
                for _ in range(10):
                    await asyncio.sleep(1)
                    if self._program_generation != before:
                        self.refresh_status = "Schedule received from controller"
                        break
                else:
                    await self.async_seed_from_cloud()
                    self.refresh_status = (
                        "Schedule received from controller" if self._program_generation != before
                        else "No fresh schedule received; saved values retained"
                    )
            except (MaxspectConnectionError, OSError) as err:
                self.refresh_status = "Refresh failed; saved values retained"
                _LOGGER.warning("Program refresh failed: %s", err)
                self.async_set_updated_data(self.client.state)
                raise UpdateFailed(f"Program refresh failed: {err}") from err
            self.async_set_updated_data(self.client.state)

    async def async_set_power(self, on: bool) -> None:
        """Turn the device on or off, routing to the correct cloud command."""
        if self.cloud is None and not (
            self.device_type == DEVICE_TYPE_GYRE
            and self.config_entry.options.get("local_control", DEFAULT_LOCAL_CONTROL)
        ):
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
        if self.device_type == DEVICE_TYPE_GYRE:
            try:
                if not self.client.connected:
                    await self.client.async_connect()
                return await self.client.async_request_status()
            except (MaxspectConnectionError, OSError) as err:
                if time.monotonic() < self._write_lock_until:
                    _LOGGER.warning("LAN unavailable during command cooldown: %s", err)
                    return self.client.state
                return await self._async_cloud_fallback(err)
        await self.async_seed_from_cloud(fallback=True)
        return self.client.state

    async def async_shutdown(self) -> None:
        await self._program_store.async_save({"settings": self.saved_settings, "received": self.settings_received})
        await super().async_shutdown()
        await self.client.async_disconnect()
        if self.cloud is not None:
            await self.cloud.async_close()
