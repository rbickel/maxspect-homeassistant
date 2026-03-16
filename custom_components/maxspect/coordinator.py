"""Data coordinator for Maxspect devices."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import MaxspectClient, MaxspectConnectionError, MaxspectDeviceState
from .cloud import GizwitsCloudClient, GizwitsCloudError
from .const import (
    CONF_CLOUD_DID,
    CONF_CLOUD_PASSWORD,
    CONF_CLOUD_REGION,
    CONF_CLOUD_USERNAME,
    DEFAULT_CLOUD_REGION,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    GIZWITS_APP_ID,
    GIZWITS_PRODUCT_KEY,
    MODE_OFF,
)

_LOGGER = logging.getLogger(__name__)


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

    def _on_device_push(self) -> None:
        self.async_set_updated_data(self.client.state)

    async def async_cloud_login(self) -> None:
        """Log in to the cloud and discover the device if needed."""
        if self.cloud is None:
            return
        await self.cloud.async_login()
        if not self._cloud_did:
            self._cloud_did = await self.cloud.async_discover_device(
                GIZWITS_PRODUCT_KEY
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

        # Optimistic state update
        state = self.client.state
        state.mode = mode
        state.is_on = mode != MODE_OFF
        self.async_set_updated_data(state)

    async def _async_update_data(self) -> MaxspectDeviceState:
        if not self.client.connected:
            try:
                await self.client.async_connect()
            except MaxspectConnectionError as err:
                raise UpdateFailed(f"Error connecting: {err}") from err
        return self.client.state

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        await self.client.async_disconnect()
        if self.cloud is not None:
            await self.cloud.async_close()
