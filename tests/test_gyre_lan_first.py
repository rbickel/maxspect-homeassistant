"""LAN-first defaults, model parity, and real fallback routing."""

from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.maxspect.api import MaxspectConnectionError, _dp_is_flagged, _build_write_payload
from custom_components.maxspect.const import MODE_FEED, MODE_EXIT_FEED, MODE_OFF, MODE_ON
from custom_components.maxspect.cloud import GizwitsCloudError

from .conftest import setup_integration


@pytest.mark.parametrize("models", [(0, 0), (1, 1), (0, 1), (1, 0)])
async def test_default_lan_control_for_both_models_and_mixed_pairs(
    hass, gyre_config_entry, mock_maxspect_client, mock_gizwits_cloud, models,
):
    entry = type(gyre_config_entry)(
        domain=gyre_config_entry.domain, data=dict(gyre_config_entry.data),
        unique_id=gyre_config_entry.unique_id, title=gyre_config_entry.title,
        options={"model_a": models[0], "model_b": models[1]},
    )
    await setup_integration(hass, entry)
    coordinator = entry.runtime_data
    for mode in (MODE_FEED, MODE_EXIT_FEED, MODE_OFF, MODE_ON):
        await coordinator.async_set_mode(mode)
        mock_maxspect_client.async_set_mode.assert_awaited_with(mode)
        payload = _build_write_payload(18, mode)
        assert payload[0] == 0x11
        assert _dp_is_flagged(payload[1:7], 18)
        assert not _dp_is_flagged(payload[1:7], 20)
        assert not _dp_is_flagged(payload[1:7], 21)
    mock_gizwits_cloud.async_set_mode.assert_not_awaited()
    mock_gizwits_cloud.async_login.assert_not_awaited()


async def test_healthy_lan_updates_do_not_reconnect_or_poll_cloud(
    hass, gyre_config_entry, mock_maxspect_client, mock_gizwits_cloud,
):
    await setup_integration(hass, gyre_config_entry)
    coordinator = gyre_config_entry.runtime_data
    for _ in range(3):
        await coordinator._async_update_data()
    mock_maxspect_client.async_connect.assert_awaited_once()
    mock_gizwits_cloud.async_get_device_status.assert_not_awaited()
    mock_gizwits_cloud.async_login.assert_not_awaited()


async def test_failed_lan_read_uses_cloud_fallback(
    hass, gyre_config_entry, mock_maxspect_client, mock_gizwits_cloud,
):
    await setup_integration(hass, gyre_config_entry)
    mock_maxspect_client.async_request_status.side_effect = MaxspectConnectionError("no reply")
    state = await gyre_config_entry.runtime_data._async_update_data()
    assert state.mode == MODE_ON
    mock_gizwits_cloud.async_login.assert_awaited_once()
    mock_gizwits_cloud.async_get_device_status.assert_awaited_once()


async def test_failed_lan_write_uses_cloud_fallback(
    hass, gyre_config_entry, mock_maxspect_client, mock_gizwits_cloud,
):
    await setup_integration(hass, gyre_config_entry)
    mock_maxspect_client.async_set_mode.side_effect = MaxspectConnectionError("no confirmation")
    await gyre_config_entry.runtime_data.async_set_mode(MODE_FEED)
    mock_gizwits_cloud.async_set_mode.assert_awaited_once_with(MODE_FEED, did="test-did-001")


async def test_failed_reads_on_both_channels_surface_error(
    hass, gyre_config_entry, mock_maxspect_client, mock_gizwits_cloud,
):
    await setup_integration(hass, gyre_config_entry)
    mock_maxspect_client.async_request_status.side_effect = MaxspectConnectionError("LAN offline")
    mock_gizwits_cloud.async_get_device_status.side_effect = GizwitsCloudError("cloud offline")
    with pytest.raises(UpdateFailed, match="LAN and cloud status unavailable"):
        await gyre_config_entry.runtime_data._async_update_data()
