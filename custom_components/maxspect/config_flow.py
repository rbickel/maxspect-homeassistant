"""Config flow for Maxspect integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.dhcp import DhcpServiceInfo
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import MaxspectClient, MaxspectConnectionError
from .cloud import GizwitsCloudClient, GizwitsCloudError
from .const import (
    CONF_CLOUD_DID,
    CONF_CLOUD_PASSWORD,
    CONF_CLOUD_REGION,
    CONF_CLOUD_USERNAME,
    CONF_DEVICE_NAME,
    CONF_MODEL_A,
    CONF_MODEL_B,
    DEFAULT_CLOUD_REGION,
    DEFAULT_PORT,
    DOMAIN,
    GIZWITS_APP_ID,
    GIZWITS_PRODUCT_KEY,
    MODEL_NAMES,
)

STEP_CLOUD_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CLOUD_USERNAME): str,
        vol.Required(CONF_CLOUD_PASSWORD): str,
        vol.Optional(CONF_CLOUD_REGION, default=DEFAULT_CLOUD_REGION): vol.In(
            {"eu": "Europe", "us": "United States", "cn": "China"}
        ),
    }
)

_LOGGER = logging.getLogger(__name__)


def _format_mac(mac: str) -> str:
    """Format a raw MAC string as AA:BB:CC:DD:EE:FF."""
    mac = mac.upper()
    return ":".join(mac[i : i + 2] for i in range(0, 12, 2))


class MaxspectConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Maxspect."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise flow state."""
        self._cloud_data: dict[str, Any] = {}
        self._devices: list[dict[str, Any]] = []
        self._selected_device: dict[str, Any] = {}
        self._discovered_host: str | None = None
        self._discovered_mac: str | None = None

    # -- Step 1: Cloud credentials ------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Step 1 — enter Gizwits Cloud credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            cloud = GizwitsCloudClient(
                app_id=GIZWITS_APP_ID,
                username=user_input[CONF_CLOUD_USERNAME],
                password=user_input[CONF_CLOUD_PASSWORD],
                region=user_input.get(CONF_CLOUD_REGION, DEFAULT_CLOUD_REGION),
                session=async_get_clientsession(self.hass),
            )
            try:
                await cloud.async_login()
                self._devices = await cloud.async_list_devices(GIZWITS_PRODUCT_KEY)
            except (GizwitsCloudError, Exception):  # noqa: BLE001
                errors["base"] = "cloud_auth_failed"
            else:
                if not self._devices:
                    errors["base"] = "no_devices_found"
                else:
                    self._cloud_data = user_input

                    # Auto-match if we have a MAC from DHCP discovery
                    if self._discovered_mac:
                        clean_mac = self._discovered_mac.replace(":", "").lower()
                        for dev in self._devices:
                            if dev.get("mac", "").lower() == clean_mac:
                                self._selected_device = dev
                                return await self.async_step_device_config()

                    # Single device? Skip the picker.
                    if len(self._devices) == 1:
                        self._selected_device = self._devices[0]
                        return await self.async_step_device_config()

                    return await self.async_step_pick_device()
            finally:
                await cloud.async_close()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_CLOUD_DATA_SCHEMA,
            errors=errors,
        )

    # -- Step 2: Pick a device ----------------------------------------

    async def async_step_pick_device(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Step 2 — select a device from the cloud account."""
        if user_input is not None:
            selected_did = user_input["device"]
            for dev in self._devices:
                if dev["did"] == selected_did:
                    self._selected_device = dev
                    break
            return await self.async_step_device_config()

        # Build selection list: "MAC (online/offline)"
        device_options = {}
        for dev in self._devices:
            mac = _format_mac(dev.get("mac", ""))
            status = "online" if dev.get("is_online") else "offline"
            label = f"{mac} ({status})"
            device_options[dev["did"]] = label

        return self.async_show_form(
            step_id="pick_device",
            data_schema=vol.Schema(
                {vol.Required("device"): vol.In(device_options)}
            ),
        )

    # -- Step 3: LAN IP + friendly name --------------------------------

    async def async_step_device_config(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Step 3 — set LAN IP, friendly name, and pump models."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input.get(CONF_PORT, DEFAULT_PORT)

            # Validate LAN connection
            client = MaxspectClient(host=host, port=port)
            try:
                await client.async_validate_connection()
            except MaxspectConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
            else:
                return await self._async_create_device_entry(user_input)

        # Pre-fill host from DHCP discovery if available
        suggested_host = self._discovered_host or ""
        mac = _format_mac(self._selected_device.get("mac", ""))

        model_options = {str(k): v for k, v in MODEL_NAMES.items()}

        return self.async_show_form(
            step_id="device_config",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_DEVICE_NAME,
                        description={"suggested_value": f"Maxspect {mac}"},
                    ): str,
                    vol.Required(
                        CONF_HOST,
                        description={"suggested_value": suggested_host},
                    ): str,
                    vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
                    vol.Required(
                        CONF_MODEL_A, default="0",
                    ): vol.In(model_options),
                    vol.Required(
                        CONF_MODEL_B, default="0",
                    ): vol.In(model_options),
                }
            ),
            errors=errors,
            description_placeholders={"mac": mac},
        )

    async def _async_create_device_entry(
        self, user_input: dict[str, Any],
    ) -> ConfigFlowResult:
        """Create the config entry from collected data."""
        host = user_input[CONF_HOST]
        port = user_input.get(CONF_PORT, DEFAULT_PORT)
        did = self._selected_device["did"]

        await self.async_set_unique_id(f"{host}:{port}")
        self._abort_if_unique_id_configured()

        name = user_input.get(CONF_DEVICE_NAME) or f"Maxspect {host}"
        full_data = {
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_DEVICE_NAME: name,
            CONF_MODEL_A: int(user_input.get(CONF_MODEL_A, 0)),
            CONF_MODEL_B: int(user_input.get(CONF_MODEL_B, 0)),
            **self._cloud_data,
            CONF_CLOUD_DID: did,
        }
        return self.async_create_entry(title=name, data=full_data)

    # -- DHCP discovery ------------------------------------------------

    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo,
    ) -> ConfigFlowResult:
        """Handle DHCP discovery of a potential Maxspect device."""
        host = discovery_info.ip
        mac = discovery_info.macaddress  # format: aabbccddeeff

        # Check if already configured
        await self.async_set_unique_id(f"{host}:{DEFAULT_PORT}")
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})

        # Verify this is actually a Maxspect device
        client = MaxspectClient(host=host, port=DEFAULT_PORT)
        try:
            await client.async_validate_connection()
        except (MaxspectConnectionError, Exception):  # noqa: BLE001
            return self.async_abort(reason="not_maxspect_device")

        self._discovered_host = host
        self._discovered_mac = mac
        self.context["title_placeholders"] = {"host": host}

        # Go straight to cloud credentials — we already have the IP
        return await self.async_step_user()

    # -- Legacy manual entry (direct IP, no cloud-first) ---------------

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Fallback step for manual IP entry without cloud discovery."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input.get(CONF_PORT, DEFAULT_PORT)
            client = MaxspectClient(host=host, port=port)

            try:
                await client.async_validate_connection()
            except MaxspectConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
            else:
                self._discovered_host = host
                return await self.async_step_user()

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
                }
            ),
            errors=errors,
        )
