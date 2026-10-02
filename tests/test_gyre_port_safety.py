"""Safety regressions for importing the Gyre features into push-only monitoring."""

from unittest.mock import AsyncMock, MagicMock, patch
import time

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.maxspect.api import MaxspectClient, READ_CONFIG_DPS
from custom_components.maxspect.const import MODE_EXIT_FEED, MODE_OFF, MODE_ON
from custom_components.maxspect.coordinator import MaxspectCoordinator

from .conftest import setup_integration


async def test_timestamp_report_cannot_confirm_pending_mode(
    hass, gyre_config_entry, mock_maxspect_client, mock_gizwits_cloud,
):
    await setup_integration(hass, gyre_config_entry)
    coordinator = gyre_config_entry.runtime_data
    await coordinator.async_set_mode(MODE_OFF)
    deadline = coordinator._write_lock_until
    coordinator.client.last_report_attrs = {"Time": "011a0a02100000"}

    coordinator._on_device_push()

    assert coordinator._write_lock_until == deadline
    assert coordinator.data.is_on is False


async def test_resume_terminal_report_confirms_cloud_command(
    hass, gyre_config_entry, mock_maxspect_client, mock_gizwits_cloud,
):
    await setup_integration(hass, gyre_config_entry)
    coordinator = gyre_config_entry.runtime_data
    await coordinator.async_set_mode(MODE_EXIT_FEED)
    coordinator.client.state.mode = 1
    coordinator.client.state.is_on = True
    coordinator.client.last_report_attrs = {"Mode": 1}

    coordinator._on_device_push()

    assert coordinator._write_lock_until == 0
    assert coordinator.data.mode == 1
    assert coordinator.data.is_on is True


async def test_previous_cloud_cooldown_cannot_revert_confirmed_local_resume(
    hass, gyre_config_entry, mock_maxspect_client, mock_gizwits_cloud,
):
    entry = type(gyre_config_entry)(
        domain=gyre_config_entry.domain, data=dict(gyre_config_entry.data),
        unique_id=gyre_config_entry.unique_id, title=gyre_config_entry.title,
        options={"local_control": True},
    )
    await setup_integration(hass, entry)
    coordinator = entry.runtime_data
    coordinator._pending_mode = MODE_OFF
    coordinator._write_lock_until = time.monotonic() + 8
    coordinator.client.state.mode = MODE_OFF

    async def confirm(mode):
        coordinator.client.state.mode = 1
        coordinator.client.state.is_on = True
        coordinator.client.last_report_attrs = {"Mode": 1}
        coordinator._on_device_push()

    coordinator.client.async_set_mode.side_effect = confirm
    await coordinator.async_set_mode(MODE_EXIT_FEED)

    assert coordinator.data.mode == 1
    assert coordinator.data.is_on is True
    mock_gizwits_cloud.async_set_mode.assert_not_awaited()


async def test_invalid_models_are_not_saved(hass, gyre_config_entry, mock_gizwits_cloud):
    coordinator = MaxspectCoordinator(hass, gyre_config_entry)
    coordinator._remember_settings({"Model_A": 0, "Model_B": 0})
    coordinator._remember_settings({"Model_A": 26, "Model_B": 10})

    assert coordinator.saved_settings["Model_A"] == 0
    assert coordinator.saved_settings["Model_B"] == 0
    await coordinator.async_shutdown()


async def test_explicit_refresh_selects_only_the_47_defined_datapoints():
    client = MaxspectClient("192.0.2.1")
    client._connected = True
    client._writer = MagicMock()
    client._writer.drain = AsyncMock()

    await client.async_request_full_status()

    frames = [call.args[0] for call in client._writer.write.call_args_list]
    assert len(frames) == 2
    assert frames[0][8:] == b"\x00\x00\x00\x03" + READ_CONFIG_DPS
    assert frames[1][8:] == READ_CONFIG_DPS
    assert READ_CONFIG_DPS[0] == 0x12


async def test_failed_explicit_refresh_is_not_success_shaped(
    hass, gyre_config_entry, mock_gizwits_cloud,
):
    coordinator = MaxspectCoordinator(hass, gyre_config_entry)
    with patch.object(
        coordinator.client, "async_request_full_status",
        new=AsyncMock(side_effect=OSError("connection lost")),
    ):
        with pytest.raises(UpdateFailed, match="connection lost"):
            await coordinator.async_refresh_program()

    assert coordinator.refresh_status == "Refresh failed; saved values retained"
    await coordinator.async_shutdown()
