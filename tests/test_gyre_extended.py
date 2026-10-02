"""Regressions from XF350CE hardware validation."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.maxspect.api import (
    MaxspectClient, MaxspectConnectionError, READ_CONFIG_DPS, READ_STATE_NOTIFY,
    _build_write_payload,
)
from custom_components.maxspect.const import DP_LENGTHS, GYRE_DP_NAMES
from .conftest import setup_integration


def report(values, action=0x14):
    mask = sum(1 << dp for dp in values)
    bools = [dp for dp in sorted(values) if dp < 17]
    bool_value = sum(int(values[dp]) << bit for bit, dp in enumerate(bools))
    data = bool_value.to_bytes((len(bools) + 7) // 8, "big")
    for dp in sorted(values):
        if dp >= 17:
            data += bytes([values[dp]]) if dp < 33 else values[dp]
    return bytes([action]) + mask.to_bytes(6, "big") + data


def test_reads_never_use_the_device_write_opcode():
    assert READ_STATE_NOTIFY == bytes.fromhex("12000400000000")
    assert READ_CONFIG_DPS == bytes.fromhex("127fffffffffff")
    assert _build_write_payload(18, 2) == bytes.fromhex("1100000004000002")


def test_all_47_datapoints_from_full_read_response():
    values = {dp: False for dp in range(17)}
    values.update({dp: 0 for dp in range(17, 33)})
    values.update({dp: bytes(DP_LENGTHS[dp]) for dp in range(33, 47)})
    values.update({17: 36, 18: 1, 19: 15, 20: 1, 21: 1, 6: True})
    values[34] = bytes([1, 26, 9, 15, 14, 30, 0])
    client = MaxspectClient("192.0.2.1")
    client._process_push(report(values, 0x13))
    assert set(client.state.generic_attrs) == set(GYRE_DP_NAMES)
    assert client.state.feed_duration == 15
    assert client.state.model_a == client.state.model_b == 1
    assert client.state.generic_attrs["Error_B"] is True


def test_combined_report_keeps_offsets_and_mode():
    telemetry = bytes.fromhex("050008c609590036aa050007ea094d00b3bb0b0b0000000000")
    client = MaxspectClient("192.0.2.1")
    client._process_push(report({18: 1, 34: bytes([1, 26, 9, 15, 14, 30, 0]), 44: telemetry}))
    assert client.state.mode == 1
    assert client.state.timestamp == "2026-09-15 14:30:00"
    assert client.state.ch2_rpm == 2026


def test_truncated_report_does_not_change_any_attributes():
    client = MaxspectClient("192.0.2.1")
    client._process_push(report({18: 3, 35: bytes(62)})[:-1])
    assert client.state.generic_attrs == {}


@pytest.mark.parametrize(("command", "reported"), [(2, 2), (4, 1), (5, 1), (3, 3)])
async def test_local_control_waits_for_matching_device_report(command, reported):
    client = MaxspectClient("192.0.2.1")
    client._connected = True
    writer = MagicMock()
    writer.drain = AsyncMock(side_effect=lambda: client._process_push(report({18: reported})))
    client._writer = writer
    await client.async_set_mode(command)
    assert client.state.mode == reported
    assert writer.write.call_args.args[0][-8:] == _build_write_payload(18, command)


async def test_model_and_local_control_options(hass, gyre_config_entry):
    gyre_config_entry.add_to_hass(hass)
    flow = await hass.config_entries.options.async_init(gyre_config_entry.entry_id)
    assert flow["type"] == "form"
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], user_input={"model_a": 1, "model_b": 1, "local_control": True},
    )
    assert result["type"] == "create_entry"
    assert gyre_config_entry.options["model_b"] == 1


async def test_invalid_mac_options_are_reported(hass, gyre_config_entry):
    from homeassistant.data_entry_flow import InvalidData
    gyre_config_entry.add_to_hass(hass)
    flow = await hass.config_entries.options.async_init(gyre_config_entry.entry_id)
    with pytest.raises(InvalidData, match="device_mac"):
        await hass.config_entries.options.async_configure(
            flow["flow_id"],
            user_input={"model_a": 0, "model_b": 1, "local_control": True, "device_mac": "invalid"},
        )


@pytest.mark.parametrize("models", [(0, 0), (1, 1), (0, 1), (1, 0)])
@pytest.mark.parametrize("local_control", [True, False])
async def test_feeding_and_resume_buttons(
    hass, gyre_config_entry, mock_maxspect_client, mock_gizwits_cloud,
    models, local_control,
):
    entry = type(gyre_config_entry)(
        domain=gyre_config_entry.domain, data=dict(gyre_config_entry.data),
        unique_id=gyre_config_entry.unique_id, title=gyre_config_entry.title,
        options={"model_a": models[0], "model_b": models[1], "local_control": local_control},
    )
    await setup_integration(hass, entry)
    for suffix, mode in (("start_feeding_pause", 2), ("resume_pumps", 4)):
        await hass.services.async_call("button", "press", {
            "entity_id": f"button.maxspect_my_gyre_{suffix}",
        }, blocking=True)
        if local_control:
            mock_maxspect_client.async_set_mode.assert_awaited_with(mode)
            mock_gizwits_cloud.async_set_mode.assert_not_awaited()
        else:
            mock_gizwits_cloud.async_set_mode.assert_awaited_with(mode, did="test-did-001")


async def test_local_control_uses_cloud_only_on_failure(
    hass, gyre_config_entry, mock_maxspect_client, mock_gizwits_cloud,
):
    gyre_config_entry = type(gyre_config_entry)(
        domain="maxspect", data=dict(gyre_config_entry.data),
        unique_id=gyre_config_entry.unique_id, title=gyre_config_entry.title,
        options={"local_control": True},
    )
    await setup_integration(hass, gyre_config_entry)
    coordinator = gyre_config_entry.runtime_data
    await coordinator.async_set_mode(2)
    mock_gizwits_cloud.async_set_mode.assert_not_awaited()
    mock_maxspect_client.async_set_mode.side_effect = MaxspectConnectionError("timeout")
    await coordinator.async_set_mode(4)
    mock_gizwits_cloud.async_set_mode.assert_awaited_with(4, did="test-did-001")


async def test_internal_program_data_has_no_redundant_raw_entity(
    hass, gyre_config_entry, mock_maxspect_client, mock_gizwits_cloud,
):
    mock_gizwits_cloud.async_get_device_status.return_value = {
        "attr": {"Mode": 5, "Auto": "01" * 781},
    }
    mock_maxspect_client.async_request_status.side_effect = MaxspectConnectionError("no LAN status")
    await setup_integration(hass, gyre_config_entry)
    state = hass.states.get("sensor.maxspect_my_gyre_auto_raw")
    assert state is None
    assert gyre_config_entry.runtime_data.data.generic_attrs["Auto"] == "01" * 781

@pytest.mark.parametrize(('raw', 'seconds'), [
    ('000e37', 895), ('010203', 3723), ('000000', 0),
    ('003c00', None), ('00003c', None), ('000e', None), ('zz', None), (None, None),
])
def test_countdown_matches_app_hours_minutes_seconds(raw, seconds):
    from custom_components.maxspect.sensor import decode_feed_countdown
    assert decode_feed_countdown(raw) == seconds


async def test_missing_diagnostics_hide_and_reappear_with_report(
    hass, gyre_config_entry, mock_maxspect_client, mock_gizwits_cloud,
):
    from homeassistant.helpers import entity_registry as er
    mock_gizwits_cloud.async_get_device_status.return_value = {"attr": {}}
    mock_maxspect_client.state.generic_attrs.clear()
    await setup_integration(hass, gyre_config_entry)
    coordinator = gyre_config_entry.runtime_data
    registry = er.async_get(hass)
    entity = 'sensor.maxspect_my_gyre_wash_raw'
    assert registry.async_get(entity).hidden_by == er.RegistryEntryHider.INTEGRATION
    assert registry.async_get(entity).disabled_by is None
    coordinator.data.generic_attrs['Wash'] = 10
    coordinator.async_set_updated_data(coordinator.data)
    await hass.async_block_till_done()
    assert registry.async_get(entity).hidden_by is None
    assert hass.states.get(entity).state == '10'
    registry.async_update_entity(entity, hidden_by=er.RegistryEntryHider.USER)
    coordinator.data.generic_attrs['Wash'] = 0
    coordinator.async_set_updated_data(coordinator.data)
    await hass.async_block_till_done()
    assert registry.async_get(entity).hidden_by == er.RegistryEntryHider.USER
    assert hass.states.get(entity).state == '0'


async def test_countdown_clears_after_feeding(
    hass, gyre_config_entry, mock_maxspect_client, mock_gizwits_cloud,
):
    await setup_integration(hass, gyre_config_entry)
    coordinator = gyre_config_entry.runtime_data
    coordinator.data.mode = 2
    coordinator.data.generic_attrs['Countdown_Feed'] = '000e37'
    coordinator.async_set_updated_data(coordinator.data)
    await hass.async_block_till_done()
    entity = 'sensor.maxspect_my_gyre_feeding_time_remaining'
    assert hass.states.get(entity).state == '895'
    assert hass.states.get(entity).attributes['remaining_hms'] == '00:14:55'
    coordinator.data.mode = 1
    coordinator.async_set_updated_data(coordinator.data)
    await hass.async_block_till_done()
    assert hass.states.get(entity).state == '0'

@pytest.mark.parametrize('channel', ['a', 'b'])
async def test_connection_flag_is_inverted(
    hass, gyre_config_entry, mock_maxspect_client, mock_gizwits_cloud, channel,
):
    await setup_integration(hass, gyre_config_entry)
    coordinator = gyre_config_entry.runtime_data
    for disconnected, expected in [(False, 'on'), (True, 'off')]:
        coordinator.data.generic_attrs[f'State_{channel.upper()}'] = disconnected
        coordinator.async_set_updated_data(coordinator.data)
        await hass.async_block_till_done()
        assert hass.states.get(f'binary_sensor.maxspect_my_gyre_pump_{channel}_connected').state == expected

async def test_decoded_metadata_and_feeding_minutes(
    hass, gyre_config_entry, mock_maxspect_client, mock_gizwits_cloud,
):
    from custom_components.maxspect.sensor import MaxspectDatapointSensor
    await setup_integration(hass, gyre_config_entry)
    coordinator = gyre_config_entry.runtime_data
    coordinator.data.generic_attrs.update({
        'Version_Firmware':36, 'Serial_Number':b'EXAMPLE00001\0'.hex(), 'Time_Feed':15,
    })
    assert MaxspectDatapointSensor(coordinator, 'test', 17).native_value == '3.6'
    assert MaxspectDatapointSensor(coordinator, 'test', 33).native_value == 'EXAMPLE00001'
    sensor = MaxspectDatapointSensor(coordinator, 'test', 19)
    assert sensor.native_value == 15
    assert sensor.native_unit_of_measurement == 'min'

async def test_retired_raw_entities_are_removed_from_registry(
    hass, gyre_config_entry, mock_maxspect_client, mock_gizwits_cloud,
):
    from homeassistant.helpers import entity_registry as er
    gyre_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    base = gyre_config_entry.unique_id or gyre_config_entry.data['host']
    retired = registry.async_get_or_create(
        'sensor', 'maxspect', f'{base}_dp_44',
        config_entry=gyre_config_entry, suggested_object_id='retired_bak24',
    )
    retained = registry.async_get_or_create(
        'sensor', 'maxspect', f'{base}_dp_18',
        config_entry=gyre_config_entry, suggested_object_id='retained_mode',
    )
    await hass.config_entries.async_setup(gyre_config_entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(retired.entity_id) is None
    assert registry.async_get(retained.entity_id) is not None
