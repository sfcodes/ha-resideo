"""Tests for the diagnostics dump."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant.typing import ClientSessionGenerator

from custom_components.resideo.aioresideo import (
    ResideoApiError,
    ResideoChangeConfirm,
    ResideoCommand,
    ResideoLiveFeed,
    ResideoPriority,
    parse_event,
)
from custom_components.resideo.aioresideo.exceptions import ResideoConnectionError

from .conftest import MAC, advance_time, load_fixture_json

REDACTED = "**REDACTED**"


async def test_diagnostics(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, init_integration, mock_api: AsyncMock
) -> None:
    diag = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)

    assert diag["entry"]["data"]["refresh_token"] == REDACTED

    # Regional metadata survives — it's what unit questions (issue #7) hinge on.
    account = diag["account"]["data"]
    assert account["countryCode"] == "US"
    assert account["locale"] == "en-US"
    location = account["consumerUsers"][0]["consumerAccount"]["locations"][0]
    assert location["consumerDevices"][0]["device"]["globalDeviceType"] == "Denali_S1200"
    # ...while anything identifying the person or the household does not.
    assert account["firstName"] == REDACTED
    assert account["contactEmail"] == REDACTED
    assert location["name"] == REDACTED
    assert location["id"] == REDACTED
    assert location["consumerDevices"][0]["device"]["deviceId"] == REDACTED
    assert location["consumerAccountId"] == REDACTED
    assert location["consumerDevices"][0]["consumerDeviceLocationId"] == REDACTED

    (device,) = diag["devices"]
    assert device["shadow"]["DeviceId"] == REDACTED
    assert device["shadow"]["Reported"]["DisplayedOutdoorTemperature"] == 82.0
    assert device["configuration"]["Reported"]["TemperatureUnits"] == "F"
    assert device["pushes"] == {}
    assert device["writes"] == []
    assert device["drift"] == {"pushes_merged_since_read": 0, "shadow": [], "rooms": []}
    # What the account holds survives by model and kind; the id that spells the kind does not.
    assert diag["account_devices"] == [
        {"model": "Denali_S1200", "kind": "LyricThermostatDevice", "supported": True},
        {"model": "OneLink_SC", "kind": "SmokeDetectorDevice", "supported": False},
    ]
    assert MAC not in str(diag)
    # The entry's title is "Resideo (<e-mail>)", so it stays out.
    assert "test@example.com" not in str(diag)


async def test_diagnostics_blank_what_the_household_typed_in(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration,
    mock_api: AsyncMock,
    accounts_data: dict,
    device_shadow: dict,
) -> None:
    """Room, sensor and thermostat names are the family's words (a child's name, a street
    address); the ids next to them are what a bug report needs."""
    accounts_data["data"]["photoUrl"] = "https://example.com/me.jpg"
    accounts_data["data"]["__typename"] = "ConsumerProfile"
    accounts_data["data"]["createdAt"] = "2024-03-09T14:21:07.123Z"
    location = accounts_data["data"]["consumerUsers"][0]["consumerAccount"]["locations"][0]
    location["weatherLocationId"] = "349727"
    graph_device = location["consumerDevices"][0]["device"]
    graph_device["product"] = {"productName": "ElitePro S1200 Thermostat"}
    device_shadow["Reported"]["CurrentSchedulePeriod"] = {"PeriodName": "Wake"}
    mock_api.async_get_priority.side_effect = lambda mac: ResideoPriority(
        {"Priority": {"PriorityType": "PickARoom", "Rooms": [{"Id": 1, "Name": "Guest Room"}]}}
    )
    await init_integration.runtime_data.async_refresh()
    mock_api.streams[0].on_event(parse_event(load_fixture_json("live_rooms.json")))

    diag = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)

    (device,) = diag["devices"]
    reported = device["shadow"]["Reported"]
    assert reported["DeviceName"] == REDACTED
    assert reported["InbuiltSensorState"] == {"RoomId": 0, "RoomName": REDACTED}
    assert reported["RegistrationDate"] == REDACTED
    assert {a["Name"] for a in reported["ZoneAccessories"]["ZoneAccessories"]} == {REDACTED}

    room = device["rooms"]["Rooms"][1]
    assert room["Id"] == 1
    assert room["Name"] == REDACTED
    assert room["Accessories"][0]["AccessoryAttribute"]["Name"] == REDACTED
    assert room["Accessories"][0]["AccessoryAttribute"]["Type"] == "IndoorAirSensor"
    assert device["priority"]["Priority"]["Rooms"] == [{"Id": 1, "Name": REDACTED}]

    configuration = device["configuration"]["Reported"]
    assert configuration["DealerInformation"] == REDACTED
    assert configuration["Matter"]["DiscriminatorValue"] == REDACTED
    assert configuration["Matter"]["Fabrics"][0] == {"VendorId": 4937, "Label": REDACTED}

    # A push sample is redacted like a snapshot, and keeps the room id it is addressed to.
    pushed = device["pushes"]["Rooms"]["shapes"][0]["samples"][0]["value"]
    assert pushed["PropertyName"] == "0"
    assert pushed["Value"]["Name"] == REDACTED
    assert pushed["Value"]["AvgTemperature"] == 79

    assert diag["account"]["data"]["photoUrl"] == REDACTED
    assert diag["account"]["data"]["createdAt"] == REDACTED
    assert '"weatherLocationId": "349727"' not in json.dumps(diag["account"])
    # Names that are the API's own words, not the household's, are what a report needs.
    assert diag["account"]["data"]["__typename"] == "ConsumerProfile"
    assert '"productName": "ElitePro S1200 Thermostat"' in json.dumps(diag["account"])
    assert reported["CurrentSchedulePeriod"] == {"PeriodName": "Wake"}
    for typed_in in ("Master Bedroom", "Guest Room", "My Home", "Hallway Smoke"):
        assert typed_in not in str(diag)


async def test_diagnostics_without_account_graph(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, init_integration, mock_api: AsyncMock
) -> None:
    """A failed /accounts fetch is reported inline and doesn't cost the device snapshots."""
    mock_api.async_get_accounts.side_effect = ResideoConnectionError("cloud unreachable")

    diag = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)

    assert diag["account"] == {"error": "cloud unreachable"}
    assert diag["account_devices"] == []
    assert diag["devices"][0]["shadow"]["Reported"]["DisplayedIndoorTemperature"] == 76.0


async def test_diagnostics_carry_the_pushes_received_and_the_commands_sent(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, init_integration, mock_api: AsyncMock
) -> None:
    """One download is the evidence for a new push type or a new write: what arrived, what was
    sent, whether the device confirmed it — with nothing identifying the home left in."""
    stream = mock_api.streams[0]
    stream.on_event(parse_event(load_fixture_json("live_sensor_remote.json")))
    stream.on_event(ResideoLiveFeed(MAC, "ScheduleStatus", {"Value": "Running"}, None))

    write = load_fixture_json("cool_setpoint_write.json")
    transaction_id = write["response_body"]["TransactionId"]
    mock_api.command_cb(
        ResideoCommand(MAC, "coolSetpoint", write["request_body"], result=write["response_body"])
    )
    stream.on_event(
        ResideoChangeConfirm(MAC, transaction_id, "changeSetpoint", "AppInitiated", True)
    )
    mock_api.command_cb(
        ResideoCommand(
            MAC,
            "freezeProtection",
            {"LowLimitDegrees": 41.5},
            error=ResideoApiError(
                f"PUT https://api.ha.resideo.com/device/{MAC}/freezeProtection -> 400",
                status=400,
                body={"error": f"invalid value for device {MAC}"},
            ),
        )
    )
    await advance_time(hass, 4)  # let the resync those events scheduled run

    diag = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)

    assert diag["capture_since"]
    (device,) = diag["devices"]

    sensor = device["pushes"]["Sensor"]
    assert sensor["merged_in_memory"] is True
    assert sensor["count"] == 1
    value = sensor["shapes"][0]["samples"][0]["value"]
    assert value["AccessoryValue"]["BatteryStatus"] == "Ok"
    assert value["AccessoryAttribute"]["SerialNumber"] == REDACTED
    assert device["pushes"]["ScheduleStatus"]["merged_in_memory"] is False

    accepted, refused = device["writes"]
    assert accepted["command"] == "coolSetpoint"
    assert accepted["body"] == write["request_body"]
    assert accepted["result"] == {"TransactionId": transaction_id}
    assert accepted["confirmation"]["success"] is True
    assert refused["command"] == "freezeProtection"
    assert refused["error"]["status"] == 400
    assert refused["error"]["message"].endswith(f"{REDACTED}/freezeProtection -> 400")
    assert refused["error"]["response"] == {"error": f"invalid value for device {REDACTED}"}
    assert MAC not in str(diag)


async def test_diagnostics_list_where_memory_drifted_from_the_cloud(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration,
    mock_api: AsyncMock,
    device_shadow: dict,
) -> None:
    """The proof a merge is right: after a push, what is held equals what the cloud reports."""
    push = load_fixture_json("live_setpoint.json")
    mock_api.streams[0].on_event(parse_event(push))
    pushed = push["Body"]["Value"]["CoolSetpoint"]
    assert pushed != device_shadow["Reported"]["Setpoint"]["CoolSetpoint"]

    diag = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)

    drift = diag["devices"][0]["drift"]
    assert drift["pushes_merged_since_read"] == 1
    assert drift["rooms"] == []
    assert {
        "path": "Reported.Setpoint.CoolSetpoint",
        "held": pushed,
        "cloud": device_shadow["Reported"]["Setpoint"]["CoolSetpoint"],
    } in drift["shadow"]

    # Once the cloud reports what the push said, nothing is left to list.
    device_shadow["Reported"]["Setpoint"]["CoolSetpoint"] = pushed
    diag = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)
    assert not any(
        d["path"] == "Reported.Setpoint.CoolSetpoint" for d in diag["devices"][0]["drift"]["shadow"]
    )


async def test_diagnostics_say_when_the_state_held_is_just_a_read(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, init_integration, mock_api: AsyncMock
) -> None:
    """After a resync nothing was built from pushes, so "no drift" would prove nothing."""
    mock_api.streams[0].on_event(parse_event(load_fixture_json("live_setpoint.json")))
    await init_integration.runtime_data.async_refresh()

    diag = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)

    assert diag["devices"][0]["drift"] == {
        "pushes_merged_since_read": 0,
        "shadow": [],
        "rooms": [],
    }


async def test_diagnostics_drift_survives_a_failed_read(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, init_integration, mock_api: AsyncMock
) -> None:
    mock_api.async_get_device.side_effect = ResideoConnectionError(f"GET /device/{MAC} failed")

    diag = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)

    (device,) = diag["devices"]
    assert device["drift"] == {"error": f"GET /device/{REDACTED} failed"}
    assert device["shadow"]["Reported"]["DisplayedIndoorTemperature"] == 76.0
