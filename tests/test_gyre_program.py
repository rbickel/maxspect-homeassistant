"""Gyre schedule decoding, midnight selection and saved configuration."""
import pytest
from custom_components.maxspect.gyre_program import decode_program, active_entry
from custom_components.maxspect.api import MaxspectClient
from custom_components.maxspect.coordinator import MaxspectCoordinator

AUTO = '54080100000101050606010502020004010506010503080001010606060106040a00040108060108050c00030108545400068181060108000606120004010806010807140001010606060106081600040106060106'


def test_full_schedule_and_alternating_power():
    entries = decode_program(AUTO, scheduled=True)
    assert len(entries) == 8
    entry = active_entry(entries, 15 * 60)
    assert entry['time'] == '12:00'
    assert entry['a']['pattern'] == 'Alternating'
    assert entry['b']['pattern'] == 'Anti-synchronized'
    for channel in ('a', 'b'):
        assert entry[channel]['power_percent'] == 80
        assert entry[channel]['alternate_power_percent'] == -60
    assert active_entry(entries, 18 * 60)['a']['pattern'] == 'Random'
    assert active_entry(entries, 23 * 60)['time'] == '22:00'
    assert active_entry(entries[1:], 0)['time'] == '22:00'


@pytest.mark.parametrize('raw', [None, '', 'zz', 'ff01', AUTO[:-2], AUTO.replace('5408', '5409', 1)])
def test_malformed_program_is_not_partially_accepted(raw):
    assert decode_program(raw, scheduled=True) == []


def test_manual_and_invalid_speed():
    entries = decode_program('06000105040108', scheduled=False)
    assert entries[0]['a']['pattern'] == 'Constant speed'
    assert entries[0]['b']['power_percent'] == 80
    assert decode_program('060001ff040108', scheduled=False) == []


async def test_saved_schedule_survives_reload_without_stale_live_state(hass, gyre_config_entry):
    first = MaxspectCoordinator(hass, gyre_config_entry)
    first._remember_settings({'Auto': AUTO, 'Time_Feed': 15, 'Mode': 3, 'Error_A': True})
    received = dict(first.settings_received)
    await first._program_store.async_save({'settings': first.saved_settings, 'received': received})
    second = MaxspectCoordinator(hass, gyre_config_entry)
    await second.async_load_settings()
    assert second.saved_settings['Auto'] == AUTO
    assert second.settings_received == received
    assert second.client.state.feed_duration == 15
    assert 'Error_A' not in second.client.state.generic_attrs
    assert 'Mode' not in second.client.state.generic_attrs
    second._remember_settings({'Auto': 'broken'})
    assert second.saved_settings['Auto'] == AUTO
    second._remember_settings({'Auto': AUTO}, fresh=False)
    assert second.settings_received == received
    await first.async_shutdown()
    await second.async_shutdown()

async def test_refresh_ack_alone_never_replaces_schedule(hass, gyre_config_entry):
    from unittest.mock import AsyncMock, patch
    coordinator = MaxspectCoordinator(hass, gyre_config_entry)
    coordinator._remember_settings({'Auto': AUTO})
    before = dict(coordinator.settings_received)
    with patch.object(coordinator.client, 'async_request_full_status', new=AsyncMock()), patch.object(coordinator, 'async_seed_from_cloud', new=AsyncMock()), patch('custom_components.maxspect.coordinator.asyncio.sleep', new=AsyncMock()):
        await coordinator.async_refresh_program()
    assert coordinator.saved_settings['Auto'] == AUTO
    assert coordinator.settings_received == before
    assert coordinator.refresh_status == 'No fresh schedule received; saved values retained'
    await coordinator.async_shutdown()


async def test_schedule_sensors_show_programmed_not_watt_values(hass, gyre_config_entry):
    from custom_components.maxspect.sensor import MaxspectProgramSensor
    from datetime import datetime
    from unittest.mock import patch
    coordinator = MaxspectCoordinator(hass, gyre_config_entry)
    coordinator._remember_settings({'Auto': AUTO})
    coordinator.client.state.mode = 1
    coordinator.async_set_updated_data(coordinator.client.state)
    with patch.object(coordinator.client, 'controller_time_now', return_value=datetime(2026, 9, 15, 15, 0)):
        pattern = MaxspectProgramSensor(coordinator, 'test', 'pattern', 'b')
        power = MaxspectProgramSensor(coordinator, 'test', 'power', 'b')
        assert pattern.native_value == 'Anti-synchronized'
        assert power.native_value == 80
        assert power.native_unit_of_measurement == '%'
        assert power.extra_state_attributes['alternate_power_percent'] == -60
        coordinator.client.state.mode = 2
        assert pattern.native_value == 'Feeding pause'
        assert power.native_value is None
    await coordinator.async_shutdown()


def test_duplicate_or_invalid_clock_does_not_reset_elapsed_time():
    from unittest.mock import patch
    from datetime import datetime
    client = MaxspectClient('192.0.2.1')
    with patch('custom_components.maxspect.api.time.monotonic', return_value=100):
        client.apply_attributes({'Time': '011a090f0c0000'})
    with patch('custom_components.maxspect.api.time.monotonic', return_value=160):
        client.apply_attributes({'Time': '011a090f0c0000'})
        assert client.controller_time_now() == datetime(2026, 9, 15, 12, 1)
        client.apply_attributes({'Time': '011a0dff0c0000'})
        assert client.controller_time_now() == datetime(2026, 9, 15, 12, 1)
