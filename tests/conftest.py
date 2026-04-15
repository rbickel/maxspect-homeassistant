"""Shared fixtures for Maxspect integration tests.

Layer 2 (HA integration) tests use the real HA core runtime provided by
``pytest-homeassistant-custom-component``.  All I/O boundaries (TCP LAN
client, Gizwits Cloud HTTP client) are mocked so tests run without any
physical device.

Fixtures here are reusable across test_config_flow, test_init, test_switch,
test_sensor, and test_coordinator_integration.
"""

from __future__ import annotations

import struct
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.maxspect.api import MaxspectDeviceState
from custom_components.maxspect.const import (
    CONF_CLOUD_DEVICE_NAME,
    CONF_CLOUD_DID,
    CONF_CLOUD_PASSWORD,
    CONF_CLOUD_PRODUCT_KEY,
    CONF_CLOUD_REGION,
    CONF_CLOUD_USERNAME,
    CONF_DEVICE_PROTOCOL,
    DEFAULT_PORT,
    DEVICE_PROTOCOL_GIZWITS,
    DEVICE_TYPE_GYRE,
    DOMAIN,
    GIZWITS_APP_ID,
    GIZWITS_PRODUCT_KEY,
    MODE_OFF,
    MODE_ON,
)


# ---------------------------------------------------------------------------
# Enable loading custom_components from the workspace
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,  # noqa: ARG001
) -> None:
    """Enable custom integrations in all tests."""


# ---------------------------------------------------------------------------
# Payload builders (shared with unit tests)
# ---------------------------------------------------------------------------

def build_compact_payload(
    mode: int = MODE_ON,
    ch1_rpm: int = 1500,
    ch1_v_x100: int = 2437,
    ch1_w: int = 72,
    ch2_rpm: int = 1200,
    ch2_v_x100: int = 2360,
    ch2_w: int = 65,
) -> bytes:
    """Build a 25-byte compact-telemetry payload."""
    payload = bytearray(25)
    payload[0] = mode
    struct.pack_into(">H", payload, 2, ch1_rpm)
    struct.pack_into(">H", payload, 4, ch1_v_x100)
    payload[7] = ch1_w
    struct.pack_into(">H", payload, 11, ch2_rpm)
    struct.pack_into(">H", payload, 13, ch2_v_x100)
    payload[16] = ch2_w
    return bytes(payload)


def build_bak24_hex(**kwargs) -> str:
    """Build compact-telemetry hex string (cloud Bak24 attribute)."""
    return build_compact_payload(**kwargs).hex()


def build_time_hex(
    power: int = 1,
    year: int = 26,
    month: int = 4,
    day: int = 11,
    hour: int = 14,
    minute: int = 30,
    second: int = 0,
) -> str:
    """Build state-notify hex string (cloud Time attribute)."""
    return bytes([power, year, month, day, hour, minute, second]).hex()


# ---------------------------------------------------------------------------
# Sample cloud payloads
# ---------------------------------------------------------------------------

GYRE_CLOUD_ATTRS_ON: dict = {
    "Mode": MODE_ON,
    "Bak24": build_bak24_hex(
        mode=MODE_ON, ch1_rpm=1500, ch1_v_x100=2437, ch1_w=72,
        ch2_rpm=1200, ch2_v_x100=2360, ch2_w=65,
    ),
    "Time": build_time_hex(power=1),
    "Time_Feed": 10,
    "Model_A": 0,
    "Model_B": 0,
    "Wash": 7,
}


# ---------------------------------------------------------------------------
# Config entry data
# ---------------------------------------------------------------------------

GYRE_CONFIG_DATA: dict = {
    "host": "192.168.1.100",
    "port": DEFAULT_PORT,
    CONF_DEVICE_PROTOCOL: DEVICE_PROTOCOL_GIZWITS,
    CONF_CLOUD_USERNAME: "test@example.com",
    CONF_CLOUD_PASSWORD: "testpass123",
    CONF_CLOUD_REGION: "eu",
    CONF_CLOUD_DID: "test-did-001",
    CONF_CLOUD_PRODUCT_KEY: GIZWITS_PRODUCT_KEY,
    CONF_CLOUD_DEVICE_NAME: "My Gyre",
}


@pytest.fixture
def gyre_config_entry() -> MockConfigEntry:
    """Return a MockConfigEntry for a Gyre pump."""
    return MockConfigEntry(
        domain=DOMAIN,
        data=GYRE_CONFIG_DATA,
        unique_id="192.168.1.100:12416",
        title="Maxspect My Gyre",
        version=1,
    )


# ---------------------------------------------------------------------------
# Mock LAN client
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_lan_client() -> MagicMock:
    """Return a mock MaxspectClient that never opens a real TCP connection."""
    client = MagicMock()
    client.host = "192.168.1.100"
    client.port = DEFAULT_PORT
    client.connected = True
    client.state = MaxspectDeviceState(
        is_on=True,
        mode=MODE_ON,
        last_active_mode=MODE_ON,
        ch1_rpm=1500,
        ch1_voltage=24.37,
        ch1_power=72,
        ch2_rpm=1200,
        ch2_voltage=23.60,
        ch2_power=65,
        timestamp="2026-04-11 14:30:00",
        feed_duration=10,
        model_a=0,
        model_b=0,
        wash_reminder=7,
    )
    client.async_connect = AsyncMock()
    client.async_disconnect = AsyncMock()
    client.async_validate_connection = AsyncMock()
    client.async_request_status = AsyncMock(return_value=client.state)
    client.async_set_mode = AsyncMock()
    client.set_update_callback = MagicMock()
    return client


# ---------------------------------------------------------------------------
# Mock cloud client
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_cloud_client() -> AsyncMock:
    """Return a mock GizwitsCloudClient that never makes HTTP requests."""
    cloud = AsyncMock()
    cloud.did = "test-did-001"
    cloud.product_key = GIZWITS_PRODUCT_KEY
    cloud.device_name = "My Gyre"
    cloud.async_login = AsyncMock()
    cloud.async_set_mode = AsyncMock()
    cloud.async_set_attr = AsyncMock()
    cloud.async_close = AsyncMock()
    cloud.async_discover_device = AsyncMock(return_value="test-did-001")
    cloud.async_validate = AsyncMock(return_value="test-did-001")
    cloud.async_get_device_status = AsyncMock(
        return_value={"attr": GYRE_CLOUD_ATTRS_ON}
    )
    return cloud


@pytest.fixture
def no_cloud_seed(mock_cloud_client: AsyncMock) -> AsyncMock:
    """Disable cloud seeding so tests control initial state via mock_lan_client."""
    mock_cloud_client.async_get_device_status.return_value = {"attr": {}}
    return mock_cloud_client


# ---------------------------------------------------------------------------
# Patch helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_maxspect_client(mock_lan_client: MagicMock) -> Generator[MagicMock]:
    """Patch MaxspectClient constructor to return the mock."""
    with patch(
        "custom_components.maxspect.coordinator.MaxspectClient",
        return_value=mock_lan_client,
    ) as patched:
        # Also patch config_flow imports
        with patch(
            "custom_components.maxspect.config_flow.MaxspectClient",
            return_value=mock_lan_client,
        ):
            yield mock_lan_client


@pytest.fixture
def mock_gizwits_cloud(mock_cloud_client: AsyncMock) -> Generator[AsyncMock]:
    """Patch GizwitsCloudClient constructor to return the mock."""
    with patch(
        "custom_components.maxspect.coordinator.GizwitsCloudClient",
        return_value=mock_cloud_client,
    ):
        with patch(
            "custom_components.maxspect.config_flow.GizwitsCloudClient",
            return_value=mock_cloud_client,
        ):
            # Prevent real aiohttp sessions (thread leak in tests)
            with patch(
                "custom_components.maxspect.coordinator.async_get_clientsession",
                return_value=MagicMock(),
            ):
                with patch(
                    "custom_components.maxspect.config_flow.async_get_clientsession",
                    return_value=MagicMock(),
                ):
                    yield mock_cloud_client


# ---------------------------------------------------------------------------
# Full setup helper
# ---------------------------------------------------------------------------

async def setup_integration(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
) -> MockConfigEntry:
    """Set up the integration with mocked I/O, return the config entry."""
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry
