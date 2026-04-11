"""Config flow for Maxspect integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
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
    DEFAULT_CLOUD_REGION,
    DEFAULT_PORT,
    DOMAIN,
    GIZWITS_APP_ID,
    GIZWITS_KNOWN_PRODUCT_KEYS,
)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
    }
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


class MaxspectConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Maxspect."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise flow state."""
        self._lan_data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step — user provides device IP."""
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
                self._lan_data = user_input
                return await self.async_step_cloud()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle step 2 — Gizwits Cloud credentials for device control."""
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
                unique_id = (
                    f"maxspect_{device_name}"
                    if device_name
                    else f"{self._lan_data[CONF_HOST]}:{self._lan_data.get(CONF_PORT, DEFAULT_PORT)}"
                )
                title = f"Maxspect {device_name}" if device_name else f"Maxspect {self._lan_data[CONF_HOST]}"
                full_data = {
                    **self._lan_data,
                    **user_input,
                    CONF_CLOUD_DID: did,
                    CONF_CLOUD_PRODUCT_KEY: cloud.product_key or "",
                    CONF_CLOUD_DEVICE_NAME: device_name,
                }
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=title,
                    data=full_data,
                )
            finally:
                await cloud.async_close()

        return self.async_show_form(
            step_id="cloud",
            data_schema=STEP_CLOUD_DATA_SCHEMA,
            errors=errors,
        )
