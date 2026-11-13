"""Merge tests — lock the SignalR LiveFeed → shadow/rooms field mapping (plan Part A6).

Each LiveFeed fixture encodes a *change* from the baseline ``device.json`` / ``rooms.json`` so a
no-op merge (or a wrong key/rename) fails the assertion. Also covers the 4 fixes + purity.
"""

from __future__ import annotations

import copy

from custom_components.resideo.aioresideo import (
    ResideoRooms,
    ResideoThermostat,
    apply_live_feed,
    parse_event,
)
from custom_components.resideo.aioresideo.objects.events import ResideoLiveFeed


def _feed(loader, name: str) -> ResideoLiveFeed:
    ev = parse_event(loader(name))
    assert isinstance(ev, ResideoLiveFeed)
    return ev


def test_setpoint_merge(device_shadow, rooms, fixture_loader) -> None:
    shadow, _ = apply_live_feed(device_shadow, rooms, _feed(fixture_loader, "live_setpoint.json"))
    t = ResideoThermostat(shadow)
    assert t.cool_setpoint == 77
    assert t.heat_setpoint == 64
    assert t.setpoint_status == "PermanentHold"  # Status -> SetpointStatus rename
    assert t.fan_position == "On"  # written to top-level FanSwitch (read first)
    assert shadow["Reported"]["Setpoint"]["FanSwitch"]["Position"] == "On"  # ...and Setpoint.FanSwitch
    # untouched fields survive
    assert t.name == "Master Bedroom"
    assert t.outdoor_temperature == 82.0
    assert t.firmware_version == "01.3605.740"


def test_operation_status_merge(device_shadow, rooms, fixture_loader) -> None:
    shadow, _ = apply_live_feed(
        device_shadow, rooms, _feed(fixture_loader, "live_operation_status.json")
    )
    t = ResideoThermostat(shadow)
    assert t.operation_mode == "Heat"
    assert t.fan_request is False  # Fan -> FanRequest rename
    assert t.circulation_fan_request is True  # CircFan -> CirculationFanRequest rename
    assert t.current_stage == 2  # curStg -> HeatAndCoolDemand.CurrentStage
    assert t.demand == 68  # not in the push -> unchanged (not zeroed)


def test_system_switch_merge(device_shadow, rooms, fixture_loader) -> None:
    shadow, _ = apply_live_feed(
        device_shadow, rooms, _feed(fixture_loader, "live_system_switch.json")
    )
    t = ResideoThermostat(shadow)
    assert t.system_switch == "Heat"  # was Cool -> drives hvac_mode
    assert t.heat_cool_mode == "Heat"


def test_fan_switch_merge(device_shadow, rooms, fixture_loader) -> None:
    shadow, _ = apply_live_feed(device_shadow, rooms, _feed(fixture_loader, "live_fan_switch.json"))
    t = ResideoThermostat(shadow)
    assert t.fan_position == "On"  # was Auto
    assert shadow["Reported"]["FanSwitch"]["Speed"] == 1


def test_sensor_builtin_merge(device_shadow, rooms, fixture_loader) -> None:
    shadow, new_rooms = apply_live_feed(
        device_shadow, rooms, _feed(fixture_loader, "live_sensor_builtin.json")
    )
    ta = ResideoRooms(new_rooms).thermostat_accessory
    assert ta is not None and ta.is_thermostat  # Type still "Thermostat", not the push's "TS" (FIX #1)
    assert ta.indoor_temperature == 78
    assert ta.indoor_humidity == 52
    assert ta.rssi == -62
    assert ta.co2 == 430  # updated + Displayed preserved (FIX #4)
    assert ta.tvoc == 2
    assert ta.temperature_actual == 75.133  # not in the push -> survives (FIX #3)
    # The remote sensor is untouched by a thermostat-shaped push.
    _room, remote = ResideoRooms(new_rooms).air_sensor_accessories()[0]
    assert remote.indoor_temperature == 74.0
    # The thermostat's *control* reading (Displayed*) is NOT the thermostat sensor's raw value —
    # it is priority-weighted / Feels-Like-adjusted and only arrives via its own push or REST.
    t = ResideoThermostat(shadow)
    assert t.indoor_temperature == 76.0
    assert t.indoor_humidity == 56


def test_sensor_remote_merge(device_shadow, rooms, fixture_loader) -> None:
    """A remote-sensor push is labelled with the THERMOSTAT's ids (RoomId 0 / AccessoryId 0 /
    Type "TS") — the cloud does this for every Sensor push. It must be routed by payload shape
    (``BatteryStatus`` => remote) to the remote accessory, never into the thermostat's slot."""
    shadow, new_rooms = apply_live_feed(
        device_shadow, rooms, _feed(fixture_loader, "live_sensor_remote.json")
    )
    rr = ResideoRooms(new_rooms)
    air = rr.air_sensor_accessories()
    assert len(air) == 1
    _room, acc = air[0]
    assert acc.indoor_temperature == 70
    assert acc.indoor_humidity == 60
    assert acc.rssi == -55
    assert acc.occupancy is True  # was False
    assert acc.battery_status == "Ok"
    assert acc.occupancy_sensitivity == "Medium"
    assert acc.temperature_actual == 72.461  # not in the push -> survives
    assert acc.accessory_id == 1  # identity untouched by the push's ids
    assert acc.attributes["AccessoryValue"].get("Id") is None  # push's Id (thermostat's 0) dropped
    # The thermostat's slot is untouched: its reading, occupancy and CO2 don't flap.
    ta = rr.thermostat_accessory
    assert ta.indoor_temperature == 76.0
    assert ta.occupancy is True
    assert ta.co2 == 425.0
    assert "BatteryStatus" not in ta.attributes["AccessoryValue"]
    # ...and neither is the shadow's displayed indoor temp/humidity.
    assert ResideoThermostat(shadow).indoor_temperature == 76.0
    assert ResideoThermostat(shadow).indoor_humidity == 56


def _with_second_remote(rooms: dict) -> dict:
    """rooms.json + a second remote (room 2 / accessory 2), as REST reports two room sensors."""
    two = copy.deepcopy(rooms)
    second = copy.deepcopy(two["Rooms"][1])
    second["Id"], second["Name"] = 2, "Micah Room"
    second["Accessories"][0]["AccessoryId"] = 2
    second["Accessories"][0]["AccessoryAttribute"]["Name"] = "Micah Room"
    second["Accessories"][0]["AccessoryValue"].update(
        {"IndoorTemperature": 79.0, "IndoorHumidity": 74, "OccupancyDet": False}
    )
    two["Rooms"].append(second)
    return two


def test_sensor_pushes_are_off_by_one_with_two_remotes(
    device_shadow, rooms, fixture_loader
) -> None:
    """With two remotes the cloud's off-by-one becomes visible: the second remote's payload
    arrives under the FIRST remote's header (its room, id, type and real serial). Route each
    payload to the accessory after the header's; the header's own slot must not be touched."""
    two = _with_second_remote(rooms)
    # header = first remote (acc 1, "RL"/"IS" type, its serial) ; payload = second remote's reading
    second_remote_push = ResideoLiveFeed(
        "AABBCCDDEEFF",
        "Sensor",
        {
            "RoomId": 1,
            "AccessoryId": 1,
            "AccessoryAttribute": {"Id": 0, "Type": "IS", "Name": "Guest Room",
                                   "SerialNumber": "000000000000"},
            "AccessoryValue": {"Id": 1, "IndoorTemperature": 80, "IndoorHumidity": 74,
                               "OccupancyDet": True, "MotionDet": True, "BatteryStatus": "Ok",
                               "RssiAverage": -55, "Status": "Ok"},
        },
        None,
    )
    _s, r2 = apply_live_feed(device_shadow, two, second_remote_push)
    rr = ResideoRooms(r2)
    (_, first), (_, second) = rr.air_sensor_accessories()
    assert second.accessory_id == 2
    assert second.indoor_temperature == 80 and second.occupancy is True  # routed to acc 2
    assert first.indoor_temperature == 74.0 and first.occupancy is False  # acc 1 untouched
    assert rr.thermostat_accessory.indoor_temperature == 76.0
    # header = thermostat (acc 0) + remote payload -> the FIRST remote (acc 1), as with one remote
    _s, r3 = apply_live_feed(device_shadow, two, _feed(fixture_loader, "live_sensor_remote.json"))
    (_, first), (_, second) = ResideoRooms(r3).air_sensor_accessories()
    assert first.indoor_temperature == 70 and first.occupancy is True
    assert second.indoor_temperature == 79.0 and second.occupancy is False


def test_sensor_remote_push_is_dropped_when_unattributable(
    device_shadow, rooms, fixture_loader
) -> None:
    """A remote payload whose header has no remote successor can't be attributed — drop it
    rather than guess (or dump it into the thermostat's slot)."""
    feed = _feed(fixture_loader, "live_sensor_remote.json")
    # no remotes at all -> header acc 0 has no successor
    none = copy.deepcopy(rooms)
    del none["Rooms"][1]
    _s, r1 = apply_live_feed(device_shadow, none, feed)
    assert r1 == none
    # header names the LAST accessory -> no successor
    two = _with_second_remote(rooms)
    last = ResideoLiveFeed("AABBCCDDEEFF", "Sensor", {**feed.value, "AccessoryId": 2}, None)
    _s, r2 = apply_live_feed(device_shadow, two, last)
    assert r2 == two
    # header names an accessory we don't know -> dropped
    ghost = ResideoLiveFeed("AABBCCDDEEFF", "Sensor", {**feed.value, "AccessoryId": 9}, None)
    _s, r3 = apply_live_feed(device_shadow, two, ghost)
    assert r3 == two


def test_co2_first_time_via_push(device_shadow, rooms, fixture_loader) -> None:
    """CO2 with no prior REST block (push carries only {Measurement}) still surfaces."""
    stripped = copy.deepcopy(rooms)
    del stripped["Rooms"][0]["Accessories"][0]["AccessoryValue"]["CarbonDioxide"]
    _shadow, new_rooms = apply_live_feed(
        device_shadow, stripped, _feed(fixture_loader, "live_sensor_builtin.json")
    )
    assert ResideoRooms(new_rooms).thermostat_accessory.co2 == 430  # Displayed defaulted True


def test_rooms_merge(device_shadow, rooms, fixture_loader) -> None:
    _shadow, new_rooms = apply_live_feed(
        device_shadow, rooms, _feed(fixture_loader, "live_rooms.json")
    )
    rr = ResideoRooms(new_rooms)
    room0 = rr.rooms[0]
    assert room0.avg_temperature == 79  # was 76.0
    assert room0.avg_humidity == 49  # was 55.0
    assert room0.overall_motion is False  # was True
    # the push omits Accessories -> per-sensor values survive (not wiped)
    assert room0.accessories[0].indoor_temperature == 76.0
    assert room0.accessories[0].co2 == 425.0
    # other rooms untouched
    assert rr.rooms[1].avg_temperature == 72.0


def test_displayed_indoor_humidity_merge(device_shadow, rooms, fixture_loader) -> None:
    shadow, _ = apply_live_feed(
        device_shadow, rooms, _feed(fixture_loader, "live_displayed_indoor_humidity.json")
    )
    assert ResideoThermostat(shadow).indoor_humidity == 54  # standalone push; was 56


def test_displayed_outdoor_temperature_merge(device_shadow, rooms, fixture_loader) -> None:
    shadow, _ = apply_live_feed(
        device_shadow, rooms, _feed(fixture_loader, "live_displayed_outdoor_temperature.json")
    )
    assert ResideoThermostat(shadow).outdoor_temperature == 90  # only delivered via push; was 82.0


def test_merged_properties_set_matches_dispatch() -> None:
    """The exported set the coordinator routes on must equal the actual dispatch table."""
    from custom_components.resideo.aioresideo import LIVE_FEED_MERGED_PROPERTIES
    from custom_components.resideo.aioresideo.merge import _DISPATCH

    assert LIVE_FEED_MERGED_PROPERTIES == set(_DISPATCH)
    assert {"Rooms", "DisplayedOutdoorHumidity", "DisplayedIndoorTemperature"} <= (
        LIVE_FEED_MERGED_PROPERTIES
    )
    # a settings/slow type stays OUT (coordinator resyncs for these instead)
    assert "ScheduleEnabled" not in LIVE_FEED_MERGED_PROPERTIES


def test_unmapped_and_ghost_are_noops(device_shadow, rooms) -> None:
    # an unmapped property type (coordinator resyncs for these) -> no in-memory change
    noop = ResideoLiveFeed("AABBCCDDEEFF", "ScheduleEnabled", {"Value": True}, None)
    shadow, _ = apply_live_feed(device_shadow, rooms, noop)
    assert ResideoThermostat(shadow).cool_setpoint == 76.0
    # a missing Body.Value (value is None) -> no change, no crash
    empty = ResideoLiveFeed("AABBCCDDEEFF", "Setpoint", None, None)
    shadow2, _ = apply_live_feed(device_shadow, rooms, empty)
    assert ResideoThermostat(shadow2).cool_setpoint == 76.0
    # a Sensor push without a usable AccessoryValue -> no crash, no change
    ghost = ResideoLiveFeed(
        "AABBCCDDEEFF", "Sensor", {"RoomId": 0, "AccessoryId": 0, "AccessoryValue": None}, None
    )
    _s, r2 = apply_live_feed(device_shadow, rooms, ghost)
    assert r2 == rooms
    # ...and one arriving before any rooms are known -> no crash
    _s, r3 = apply_live_feed(device_shadow, {}, _live_sensor())
    assert r3 == {}


def _live_sensor() -> ResideoLiveFeed:
    return ResideoLiveFeed(
        "AABBCCDDEEFF",
        "Sensor",
        {"RoomId": 0, "AccessoryId": 0, "AccessoryValue": {"IndoorTemperature": 1}},
        None,
    )


def test_merge_is_pure(device_shadow, rooms, fixture_loader) -> None:
    before_shadow = copy.deepcopy(device_shadow)
    before_rooms = copy.deepcopy(rooms)
    apply_live_feed(device_shadow, rooms, _feed(fixture_loader, "live_setpoint.json"))
    apply_live_feed(device_shadow, rooms, _feed(fixture_loader, "live_sensor_builtin.json"))
    assert device_shadow == before_shadow  # inputs untouched
    assert rooms == before_rooms


def test_humidification_merge(device_shadow, rooms) -> None:
    feed = ResideoLiveFeed(
        "AABBCCDDEEFF",
        "Humidification",
        {
            "HumidifierStatus": "On",
            "DehumidifierStatus": "Off",
            "HumidifierSetpoint": 42,
            "DehumidifierSetpoint": 58,
            "WindowsProtection": 5,
            "WindowsProtectionPercentage": 85.0,
        },
        None,
    )
    shadow, _ = apply_live_feed(device_shadow, rooms, feed)
    t = ResideoThermostat(shadow)
    assert t.humidifier_running is True
    assert t.dehumidifier_running is False
    assert t.humidifier_setpoint == 42
    assert t.dehumidifier_setpoint == 58
    assert t.windows_protection == 5
    assert t.windows_protection_percentage == 85.0
