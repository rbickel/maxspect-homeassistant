"""Config flow for Maxspect integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import MaxspectClient, MaxspectConnectionError
from .cloud import (
    GizwitsCloudAuthError,
    GizwitsCloudClient,
    GizwitsCloudDeviceNotFoundError,
    GizwitsCloudError,
)
from .const import (
    CONF_CLOUD_DEVICE_NAME,
    CONF_CLOUD_DID,
    CONF_CLOUD_PASSWORD,
    CONF_CLOUD_PRODUCT_KEY,
    CONF_CLOUD_REGION,
    CONF_CLOUD_USERNAME,
    CONF_DEVICE_PROTOCOL,
    DEFAULT_CLOUD_REGION,
    DEFAULT_PORT,
    DEFAULT_LOCAL_CONTROL,
    DEVICE_PROTOCOL_GIZWITS,
    DEVICE_PROTOCOL_ICV6,
    DEVICE_TYPE_GYRE,
    DOMAIN,
    GIZWITS_APP_ID,
    GIZWITS_KNOWN_PRODUCT_KEYS,
    PRODUCT_KEY_TO_DEVICE_TYPE,
)
from .icv6_api import ICV6Client, ICV6ConnectionError, ICV6_TCP_PORT

_LOGGER = logging.getLogger(__name__)

# Step 1: device-type selector
STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DEVICE_PROTOCOL, default=DEVICE_PROTOCOL_GIZWITS): vol.In(
            {
                DEVICE_PROTOCOL_GIZWITS: "Gizwits device (Gyre pump, LED lights, Aquarium)",
                DEVICE_PROTOCOL_ICV6: "ICV6 Controller",
            }
        )
    }
)

# Step 2a: Gizwits — LAN connection details
STEP_GIZWITS_LAN_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
    }
)

# Step 2b: ICV6 — just an IP address
STEP_ICV6_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
    }
)

# Step 3 (Gizwits only): cloud credentials
STEP_CLOUD_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CLOUD_USERNAME): str,
        vol.Required(CONF_CLOUD_PASSWORD): str,
        vol.Optional(CONF_CLOUD_REGION, default=DEFAULT_CLOUD_REGION): vol.In(
            {"eu": "Europe", "us": "United States", "cn": "China"}
        ),
    }
)


class MaxspectConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Maxspect."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise flow state."""
        self._protocol: str = DEVICE_PROTOCOL_GIZWITS
        self._lan_data: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> MaxspectOptionsFlow:
        return MaxspectOptionsFlow()

    # ------------------------------------------------------------------
    # Step 1: device-type selection
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask whether the user is adding a Gizwits device or an ICV6."""
        if user_input is not None:
            self._protocol = user_input[CONF_DEVICE_PROTOCOL]
            if self._protocol == DEVICE_PROTOCOL_ICV6:
                return await self.async_step_icv6()
            return await self.async_step_gizwits_lan()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
        )

    # ------------------------------------------------------------------
    # ICV6 path: single IP step → discover → create entry
    # ------------------------------------------------------------------

    async def async_step_icv6(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle ICV6 IP entry and immediate discovery."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            client = ICV6Client(host=host, port=ICV6_TCP_PORT)

            try:
                await client.async_validate_connection()
            except ICV6ConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error validating ICV6 connection")
                errors["base"] = "unknown"
            else:
                unique_id = f"icv6_{host}"
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=f"ICV6 {host}",
                    data={
                        CONF_HOST: host,
                        CONF_PORT: ICV6_TCP_PORT,
                        CONF_DEVICE_PROTOCOL: DEVICE_PROTOCOL_ICV6,
                    },
                )

        return self.async_show_form(
            step_id="icv6",
            data_schema=STEP_ICV6_DATA_SCHEMA,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Gizwits path: LAN → Cloud (existing behaviour)
    # ------------------------------------------------------------------

    async def async_step_gizwits_lan(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate Gizwits LAN connection details."""
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
                self._lan_data = {
                    **user_input,
                    CONF_DEVICE_PROTOCOL: DEVICE_PROTOCOL_GIZWITS,
                }
                return await self.async_step_cloud()

        return self.async_show_form(
            step_id="gizwits_lan",
            data_schema=STEP_GIZWITS_LAN_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle Gizwits Cloud credentials."""
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
                did = await cloud.async_validate(known_keys=GIZWITS_KNOWN_PRODUCT_KEYS)
            except GizwitsCloudDeviceNotFoundError as err:
                _LOGGER.warning("Cloud device not found: %s", err)
                errors["base"] = "cloud_device_not_found"
            except GizwitsCloudAuthError as err:
                _LOGGER.warning("Cloud auth failed: %s", err)
                errors["base"] = "cloud_auth_failed"
            except GizwitsCloudError as err:
                _LOGGER.warning("Cloud error during validation: %s", err)
                errors["base"] = "cloud_auth_failed"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during cloud validation")
                errors["base"] = "unknown"
            else:
                device_name = cloud.device_name or ""
                # Use host:port as stable unique_id to avoid collisions from user-editable device names
                unique_id = f"{self._lan_data[CONF_HOST]}:{self._lan_data.get(CONF_PORT, DEFAULT_PORT)}"
                title = (
                    f"Maxspect {device_name}"
                    if device_name
                    else f"Maxspect {self._lan_data[CONF_HOST]}"
                )
                full_data = {
                    **self._lan_data,
                    **user_input,
                    CONF_CLOUD_DID: did,
                    CONF_CLOUD_PRODUCT_KEY: cloud.product_key or "",
                    CONF_CLOUD_DEVICE_NAME: device_name,
                }
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=title, data=full_data)
            finally:
                await cloud.async_close()

        return self.async_show_form(
            step_id="cloud",
            data_schema=STEP_CLOUD_DATA_SCHEMA,
            errors=errors,
        )


class MaxspectOptionsFlow(OptionsFlow):
    """Allow owners to identify pumps when firmware metadata is unreliable."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if (
            self.config_entry.data.get(CONF_DEVICE_PROTOCOL) == DEVICE_PROTOCOL_ICV6
            or PRODUCT_KEY_TO_DEVICE_TYPE.get(
                self.config_entry.data.get(CONF_CLOUD_PRODUCT_KEY, ""), DEVICE_TYPE_GYRE
            ) != DEVICE_TYPE_GYRE
        ):
            return self.async_abort(reason="options_not_supported")
        models = {-1: "Automatic", 0: "XF330CE", 1: "XF350CE"}
        schema = {
                vol.Required(key, default=self.config_entry.options.get(key, -1)): vol.In(models)
                for key in ("model_a", "model_b")
        }
        schema[vol.Required("local_control", default=self.config_entry.options.get("local_control", DEFAULT_LOCAL_CONTROL))] = bool
        schema[vol.Optional("device_mac", default=self.config_entry.options.get("device_mac", ""))] = vol.Any(
            "", vol.Match(r"^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$"),
        )
        schema[vol.Optional("firmware_version", default=self.config_entry.options.get("firmware_version", ""))] = str
        data_schema = vol.Schema(schema)
        errors = {}
        if user_input is not None:
            try:
                validated = data_schema(user_input)
            except vol.Invalid as err:
                _LOGGER.warning("Invalid Gyre options: %s", err)
                errors["base"] = "invalid_options"
            else:
                return self.async_create_entry(title="", data=validated)
        return self.async_show_form(
            step_id="init",
            data_schema=data_schema,
            errors=errors,
        )
