"""Integration tests for the Maxspect config flow.

Tests all user-facing paths through the config flow:
  - Device protocol selection (Gizwits vs ICV6)
  - Gizwits: LAN connection → Cloud credentials → Entry created
  - ICV6: IP entry → Entry created
  - Error handling: cannot_connect, cloud_auth_failed, cloud_device_not_found
  - Duplicate detection (abort already_configured)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.integration

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.maxspect.api import MaxspectConnectionError
from custom_components.maxspect.cloud import (
    GizwitsCloudAuthError,
    GizwitsCloudDeviceNotFoundError,
    GizwitsCloudError,
)
from custom_components.maxspect.const import (
    CONF_CLOUD_DID,
    CONF_CLOUD_DEVICE_NAME,
    CONF_CLOUD_PASSWORD,
    CONF_CLOUD_PRODUCT_KEY,
    CONF_CLOUD_REGION,
    CONF_CLOUD_USERNAME,
    CONF_DEVICE_PROTOCOL,
    DEFAULT_PORT,
    DEVICE_PROTOCOL_GIZWITS,
    DEVICE_PROTOCOL_ICV6,
    DOMAIN,
    GIZWITS_PRODUCT_KEY,
)
from custom_components.maxspect.icv6_api import ICV6ConnectionError

from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import GYRE_CONFIG_DATA


@pytest.fixture(autouse=True)
def _bypass_setup_entry():
    """Prevent async_setup_entry/async_unload_entry from running.

    Config flow tests only validate the flow steps (forms, errors, entry creation).
    """
    with patch(
        "custom_components.maxspect.async_setup_entry",
        return_value=True,
    ), patch(
        "custom_components.maxspect.async_unload_entry",
        return_value=True,
    ), patch(
        "custom_components.maxspect.config_flow.async_get_clientsession",
        return_value=MagicMock(),
    ):
        yield


# ---------------------------------------------------------------------------
# Step 1: user step (protocol selection)
# ---------------------------------------------------------------------------

class TestStepUser:

    async def test_user_form_shown(self, hass: HomeAssistant) -> None:
        """First step shows the protocol selector form."""
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

    async def test_user_selects_gizwits(self, hass: HomeAssistant) -> None:
        """Selecting Gizwits advances to gizwits_lan step."""
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_DEVICE_PROTOCOL: DEVICE_PROTOCOL_GIZWITS},
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "gizwits_lan"

    async def test_user_selects_icv6(self, hass: HomeAssistant) -> None:
        """Selecting ICV6 advances to icv6 step."""
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_DEVICE_PROTOCOL: DEVICE_PROTOCOL_ICV6},
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "icv6"


# ---------------------------------------------------------------------------
# Gizwits flow: LAN step → Cloud step → Create entry
# ---------------------------------------------------------------------------

class TestGizwitsFlow:

    async def test_full_gizwits_flow(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
    ) -> None:
        """Complete Gizwits flow: user → gizwits_lan → cloud → entry created."""
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        # Step 1: select Gizwits
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_DEVICE_PROTOCOL: DEVICE_PROTOCOL_GIZWITS},
        )
        assert result["step_id"] == "gizwits_lan"

        # Step 2: LAN details
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": "192.168.1.100", "port": DEFAULT_PORT},
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "cloud"

        # Step 3: cloud credentials
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_CLOUD_USERNAME: "test@example.com",
                CONF_CLOUD_PASSWORD: "testpass123",
                CONF_CLOUD_REGION: "eu",
            },
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["title"] == "Maxspect My Gyre"
        assert result["data"]["host"] == "192.168.1.100"
        assert result["data"][CONF_CLOUD_DID] == "test-did-001"
        assert result["data"][CONF_CLOUD_PRODUCT_KEY] == GIZWITS_PRODUCT_KEY

        # Clean up the entry created by the flow so teardown doesn't error.
        entry = result["result"]
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()

    async def test_lan_connection_failure(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
    ) -> None:
        """LAN connection failure shows cannot_connect error."""
        mock_maxspect_client.async_validate_connection.side_effect = (
            MaxspectConnectionError("Connection refused")
        )

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_DEVICE_PROTOCOL: DEVICE_PROTOCOL_GIZWITS},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": "192.168.1.100", "port": DEFAULT_PORT},
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "cannot_connect"}

    async def test_cloud_auth_failure(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
    ) -> None:
        """Cloud auth failure shows cloud_auth_failed error."""
        mock_gizwits_cloud.async_validate.side_effect = GizwitsCloudAuthError(
            "Bad credentials"
        )

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_DEVICE_PROTOCOL: DEVICE_PROTOCOL_GIZWITS},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": "192.168.1.100", "port": DEFAULT_PORT},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_CLOUD_USERNAME: "bad@example.com",
                CONF_CLOUD_PASSWORD: "wrong",
                CONF_CLOUD_REGION: "eu",
            },
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "cloud_auth_failed"}

    async def test_cloud_device_not_found(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
    ) -> None:
        """No matching device on account shows cloud_device_not_found error."""
        mock_gizwits_cloud.async_validate.side_effect = (
            GizwitsCloudDeviceNotFoundError("No devices found")
        )

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_DEVICE_PROTOCOL: DEVICE_PROTOCOL_GIZWITS},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": "192.168.1.100", "port": DEFAULT_PORT},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_CLOUD_USERNAME: "test@example.com",
                CONF_CLOUD_PASSWORD: "testpass123",
                CONF_CLOUD_REGION: "eu",
            },
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "cloud_device_not_found"}

    async def test_cloud_generic_error(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
    ) -> None:
        """Generic cloud error shows cloud_auth_failed error."""
        mock_gizwits_cloud.async_validate.side_effect = GizwitsCloudError(
            "Network timeout"
        )

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_DEVICE_PROTOCOL: DEVICE_PROTOCOL_GIZWITS},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": "192.168.1.100", "port": DEFAULT_PORT},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_CLOUD_USERNAME: "test@example.com",
                CONF_CLOUD_PASSWORD: "testpass123",
                CONF_CLOUD_REGION: "eu",
            },
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "cloud_auth_failed"}

    async def test_duplicate_entry_aborted(
        self,
        hass: HomeAssistant,
        mock_maxspect_client: MagicMock,
        mock_gizwits_cloud: AsyncMock,
        gyre_config_entry: MockConfigEntry,
    ) -> None:
        """Adding the same host:port again aborts with already_configured."""
        gyre_config_entry.add_to_hass(hass)

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_DEVICE_PROTOCOL: DEVICE_PROTOCOL_GIZWITS},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": "192.168.1.100", "port": DEFAULT_PORT},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_CLOUD_USERNAME: "test@example.com",
                CONF_CLOUD_PASSWORD: "testpass123",
                CONF_CLOUD_REGION: "eu",
            },
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "already_configured"


# ---------------------------------------------------------------------------
# ICV6 flow
# ---------------------------------------------------------------------------

class TestICV6Flow:

    async def test_full_icv6_flow(self, hass: HomeAssistant) -> None:
        """Complete ICV6 flow: user → icv6 → entry created."""
        with patch(
            "custom_components.maxspect.config_flow.ICV6Client",
        ) as mock_icv6_cls:
            mock_icv6 = AsyncMock()
            mock_icv6_cls.return_value = mock_icv6

            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_DEVICE_PROTOCOL: DEVICE_PROTOCOL_ICV6},
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {"host": "192.168.1.200"},
            )
            assert result["type"] is FlowResultType.CREATE_ENTRY
            assert result["title"] == "ICV6 192.168.1.200"
            assert result["data"][CONF_DEVICE_PROTOCOL] == DEVICE_PROTOCOL_ICV6

    async def test_icv6_connection_failure(self, hass: HomeAssistant) -> None:
        """ICV6 connection failure shows cannot_connect error."""
        with patch(
            "custom_components.maxspect.config_flow.ICV6Client",
        ) as mock_icv6_cls:
            mock_icv6 = AsyncMock()
            mock_icv6.async_validate_connection.side_effect = ICV6ConnectionError(
                "Connection refused"
            )
            mock_icv6_cls.return_value = mock_icv6

            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_DEVICE_PROTOCOL: DEVICE_PROTOCOL_ICV6},
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {"host": "192.168.1.200"},
            )
            assert result["type"] is FlowResultType.FORM
            assert result["errors"] == {"base": "cannot_connect"}

    async def test_icv6_duplicate_aborted(self, hass: HomeAssistant) -> None:
        """Adding the same ICV6 host again aborts."""
        existing = MockConfigEntry(
            domain=DOMAIN,
            data={"host": "192.168.1.200", "port": 4196, CONF_DEVICE_PROTOCOL: DEVICE_PROTOCOL_ICV6},
            unique_id="icv6_192.168.1.200",
        )
        existing.add_to_hass(hass)

        with patch(
            "custom_components.maxspect.config_flow.ICV6Client",
        ) as mock_icv6_cls:
            mock_icv6 = AsyncMock()
            mock_icv6_cls.return_value = mock_icv6

            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_DEVICE_PROTOCOL: DEVICE_PROTOCOL_ICV6},
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {"host": "192.168.1.200"},
            )
            assert result["type"] is FlowResultType.ABORT
            assert result["reason"] == "already_configured"
