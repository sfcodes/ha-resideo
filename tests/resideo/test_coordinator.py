"""Coordinator behavior: push merges, resync triggers, error surfacing, failure isolation."""

from __future__ import annotations

import base64
from copy import deepcopy

from homeassistant.core import HomeAssistant

from custom_components.resideo.aioresideo import ResideoChangeConfirm, ResideoLiveFeed
from custom_components.resideo.aioresideo.exceptions import (
    ResideoAuthError,
    ResideoConnectionError,
)
from custom_components.resideo.const import DOMAIN

from .conftest import MAC, advance_time, eid, setup_integration

MAC2 = "112233445566"


def _live(property_name: str, value) -> ResideoLiveFeed:
    return ResideoLiveFeed(MAC, property_name, value, None)


async def test_live_feed_updates_entity_state(
    hass: HomeAssistant, init_integration, mock_api
) -> None:
    temp = eid(hass, "sensor", f"{MAC}_indoor_temperature")
    assert hass.states.get(temp).state == "76.0"

    stream = mock_api.streams[0]
    stream.on_event(_live("DisplayedIndoorTemperature", {"Value": 80.0, "Sensor": "Ok"}))
    await hass.async_block_till_done()

    assert hass.states.get(temp).state == "80.0"


async def test_setpoint_push_updates_climate(
    hass: HomeAssistant, init_integration, mock_api
) -> None:
    climate = eid(hass, "climate", f"{MAC}_climate")
    assert hass.states.get(climate).attributes["temperature"] == 76.0

    mock_api.streams[0].on_event(
        _live("Setpoint", {"CoolSetpoint": 71.0, "HeatSetpoint": 62.0, "Status": "PermanentHold"})
    )
    await hass.async_block_till_done()

    assert hass.states.get(climate).attributes["temperature"] == 71.0


async def test_change_confirm_success_schedules_debounced_resync(
    hass: HomeAssistant, init_integration, mock_api
) -> None:
    mock_api.async_get_device.reset_mock()
    stream = mock_api.streams[0]

    # A burst of confirmations coalesces into ONE resync after the debounce window.
    for _ in range(3):
        stream.on_event(ResideoChangeConfirm(MAC, "tx", "changeSetpoint", "AppInitiated", True))
    await hass.async_block_till_done()
    assert mock_api.async_get_device.call_count == 0  # debounced, not immediate

    await advance_time(hass, 4)
    assert mock_api.async_get_device.call_count == 1


async def test_change_confirm_failure_does_not_resync(
    hass: HomeAssistant, init_integration, mock_api
) -> None:
    mock_api.async_get_device.reset_mock()
    mock_api.streams[0].on_event(
        ResideoChangeConfirm(MAC, "tx", "changeSetpoint", "AppInitiated", False)
    )
    await advance_time(hass, 4)
    assert mock_api.async_get_device.call_count == 0


async def test_unmerged_live_feed_schedules_resync(
    hass: HomeAssistant, init_integration, mock_api
) -> None:
    mock_api.async_get_device.reset_mock()
    mock_api.streams[0].on_event(_live("ScheduleStatus", {"Value": "Running"}))
    await advance_time(hass, 4)
    assert mock_api.async_get_device.call_count == 1


async def test_stream_error_marks_unavailable_then_push_recovers(
    hass: HomeAssistant, init_integration, mock_api
) -> None:
    temp = eid(hass, "sensor", f"{MAC}_indoor_temperature")
    stream = mock_api.streams[0]

    stream.on_error(ResideoConnectionError("socket dropped"))
    await hass.async_block_till_done()
    assert hass.states.get(temp).state == "unavailable"

    stream.on_event(_live("DisplayedIndoorTemperature", {"Value": 79.0}))
    await hass.async_block_till_done()
    assert hass.states.get(temp).state == "79.0"


async def test_stream_auth_error_starts_reauth(
    hass: HomeAssistant, init_integration, mock_api
) -> None:
    mock_api.streams[0].on_error(ResideoAuthError("token revoked"))
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(flow["context"]["source"] == "reauth" for flow in flows)


async def test_reconnect_resync_refreshes_data(
    hass: HomeAssistant, init_integration, mock_api, device_shadow
) -> None:
    """The stream's on_connected hook re-reads REST so missed changes land."""
    temp = eid(hass, "sensor", f"{MAC}_indoor_temperature")
    device_shadow["Reported"]["DisplayedIndoorTemperature"] = 68.0  # changed while offline

    await mock_api.streams[0].on_connected()
    await hass.async_block_till_done()

    assert hass.states.get(temp).state == "68.0"


async def test_one_failing_device_keeps_others_and_its_own_previous_data(
    hass: HomeAssistant, mock_config_entry, mock_api, accounts_data, shadows, device_shadow
) -> None:
    # Add a second thermostat to the account graph before setup.
    gid = base64.b64encode(f"LyricThermostatDevice:{MAC2}".encode()).decode()
    location = accounts_data["data"]["consumerUsers"][0]["consumerAccount"]["locations"][0]
    location["consumerDevices"].append(
        {
            "id": "cd2",
            "name": "Upstairs",
            "device": {"deviceId": MAC2, "globalDeviceType": "Denali_S1200", "id": gid},
        }
    )
    shadow2 = deepcopy(device_shadow)
    shadow2["DeviceId"] = MAC2
    shadow2["Reported"]["DeviceName"] = "Upstairs"
    shadows[MAC2] = shadow2

    await setup_integration(hass, mock_config_entry, mock_api)
    temp1 = eid(hass, "sensor", f"{MAC}_indoor_temperature")
    temp2 = eid(hass, "sensor", f"{MAC2}_indoor_temperature")
    assert hass.states.get(temp2).state == "76.0"

    # Device 2 now fails its shadow read; device 1 changes.
    shadows[MAC2] = ResideoConnectionError("device 2 unreachable")
    device_shadow["Reported"]["DisplayedIndoorTemperature"] = 74.0
    coordinator = mock_config_entry.runtime_data
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success  # partial failure is not a global failure
    assert hass.states.get(temp1).state == "74.0"  # fresh
    assert hass.states.get(temp2).state == "76.0"  # previous snapshot retained


async def test_all_devices_failing_marks_update_failed(
    hass: HomeAssistant, init_integration, mock_api, shadows
) -> None:
    temp = eid(hass, "sensor", f"{MAC}_indoor_temperature")
    shadows[MAC] = ResideoConnectionError("api down")

    coordinator = init_integration.runtime_data
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert not coordinator.last_update_success
    assert hass.states.get(temp).state == "unavailable"
