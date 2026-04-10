"""Gizwits Cloud REST API client for Maxspect devices.

Handles authentication, token management, and device control via the
Gizwits Open API.  Used for writing commands (Mode changes etc.) since
the Gizwits LAN protocol writes are ignored by the Maxspect MCU firmware.

Read/status monitoring stays on the LAN client for speed and locality.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Gizwits Cloud API base URLs per region
REGION_URLS: dict[str, str] = {
    "eu": "https://euapi.gizwits.com",
    "us": "https://usapi.gizwits.com",
    "cn": "https://api.gizwits.com",
}

# Token refresh buffer — refresh 5 minutes before actual expiry
_TOKEN_REFRESH_BUFFER = 300


class GizwitsCloudError(Exception):
    """Error communicating with the Gizwits Cloud API."""


class GizwitsCloudAuthError(GizwitsCloudError):
    """Authentication failure — bad credentials or wrong region."""


class GizwitsCloudDeviceNotFoundError(GizwitsCloudError):
    """Login succeeded but no matching device was found on the account."""


class GizwitsCloudClient:
    """Async client for the Gizwits Cloud REST API."""

    def __init__(
        self,
        app_id: str,
        username: str,
        password: str,
        region: str = "eu",
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._app_id = app_id
        self._username = username
        self._password = password
        self._base_url = REGION_URLS.get(region, REGION_URLS["eu"])
        self._owns_session = session is None
        self._session = session
        self._token: str | None = None
        self._token_expiry: float = 0
        self._uid: str | None = None
        self._did: str | None = None

    @property
    def did(self) -> str | None:
        """Return the device ID (set after bind/discover)."""
        return self._did

    @did.setter
    def did(self, value: str) -> None:
        """Set the device ID directly (e.g. from saved config)."""
        self._did = value

    # -- Session management --------------------------------------------

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def async_close(self) -> None:
        """Close the HTTP session if we own it."""
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # -- Authentication ------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "X-Gizwits-Application-Id": self._app_id,
            "Content-Type": "application/json",
        }
        if self._token:
            headers["X-Gizwits-User-token"] = self._token
        return headers

    async def async_login(self) -> None:
        """Log in and obtain a user token."""
        session = self._get_session()
        url = f"{self._base_url}/app/login"
        payload = {"username": self._username, "password": self._password}

        async with session.post(url, json=payload, headers=self._headers()) as resp:
            body = await resp.text()
            if resp.status != 200:
                _LOGGER.warning(
                    "Cloud login failed (HTTP %s) at %s: %s",
                    resp.status, self._base_url, body,
                )
                raise GizwitsCloudAuthError(
                    f"Login failed (HTTP {resp.status}): {body}"
                )
            try:
                data = await resp.json(content_type=None)
            except Exception as err:  # noqa: BLE001
                raise GizwitsCloudAuthError(
                    f"Login response not valid JSON: {body}"
                ) from err

        # Gizwits sometimes returns HTTP 200 with an error_code in the body
        # instead of a proper 4xx (e.g. error_code=9004 for bad credentials).
        if "error_code" in data:
            _LOGGER.warning(
                "Cloud login rejected by server (error_code=%s): %s",
                data["error_code"], data.get("detail", "no detail"),
            )
            raise GizwitsCloudAuthError(
                f"Login rejected (error_code={data['error_code']}): "
                f"{data.get('detail', 'check credentials and region')}"
            )

        if "token" not in data:
            raise GizwitsCloudAuthError(
                f"Login response missing token field: {data}"
            )

        self._token = data["token"]
        self._uid = data.get("uid")
        self._token_expiry = time.time() + data.get("expire_at", 7200)
        _LOGGER.debug("Cloud login OK, uid=%s", self._uid)

    async def _ensure_token(self) -> None:
        """Refresh the token if expired or about to expire."""
        if not self._token or time.time() >= self._token_expiry - _TOKEN_REFRESH_BUFFER:
            await self.async_login()

    # -- Device discovery ----------------------------------------------

    async def async_discover_device(self, product_key: str) -> str:
        """Find the first device matching the product key, or any bound device.

        Returns the device DID and stores it for subsequent calls.
        Falls back to the first bound device if no exact product_key match is
        found, logging the actual product key to aid support for new models.
        """
        await self._ensure_token()
        session = self._get_session()
        url = f"{self._base_url}/app/bindings"

        async with session.get(url, headers=self._headers()) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise GizwitsCloudError(
                    f"Bindings request failed (HTTP {resp.status}): {body}"
                )
            data = await resp.json(content_type=None)

        devices = data.get("devices", [])
        for dev in devices:
            if dev.get("product_key") == product_key:
                self._did = dev["did"]
                _LOGGER.debug(
                    "Discovered device did=%s (online=%s)",
                    self._did, dev.get("is_online"),
                )
                return self._did

        # No exact product_key match — fall back to the first bound device.
        # This supports models (e.g. L165) whose product_key is not yet known.
        if devices:
            dev = devices[0]
            self._did = dev["did"]
            _LOGGER.warning(
                "No device with known product_key %s found on account %s (%s). "
                "Falling back to first bound device: did=%s product_key=%s. "
                "Please report this product_key so it can be added to the integration.",
                product_key, self._username, self._base_url,
                self._did, dev.get("product_key"),
            )
            return self._did

        _LOGGER.warning(
            "No devices found on account %s (%s). "
            "Check that the device is bound to this account and that the "
            "correct region is selected.",
            self._username, self._base_url,
        )
        raise GizwitsCloudDeviceNotFoundError(
            "No devices found on this account — "
            "verify the device is bound to this account and the region is correct"
        )

    # -- Device control ------------------------------------------------

    async def async_set_mode(self, mode: int, did: str | None = None) -> None:
        """Send a Mode control command to the device."""
        target = did or self._did
        if not target:
            raise GizwitsCloudError("No device ID — call async_discover_device first")

        await self._ensure_token()
        session = self._get_session()
        url = f"{self._base_url}/app/control/{target}"
        payload: dict[str, Any] = {"attrs": {"Mode": mode}}

        async with session.post(url, json=payload, headers=self._headers()) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise GizwitsCloudError(
                    f"Control failed (HTTP {resp.status}): {body}"
                )

        _LOGGER.debug("Cloud control: Mode=%d sent to %s", mode, target)

    async def async_get_device_status(
        self, did: str | None = None,
    ) -> dict[str, Any]:
        """Get latest device attributes from the cloud."""
        target = did or self._did
        if not target:
            raise GizwitsCloudError("No device ID")

        await self._ensure_token()
        session = self._get_session()
        url = f"{self._base_url}/app/devdata/{target}/latest"

        async with session.get(url, headers=self._headers()) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise GizwitsCloudError(
                    f"Status request failed (HTTP {resp.status}): {body}"
                )
            return await resp.json(content_type=None)

    async def async_validate(self, product_key: str) -> str:
        """Login, discover device, return DID. Used by config flow."""
        await self.async_login()
        return await self.async_discover_device(product_key)
