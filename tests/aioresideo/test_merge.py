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
    # built-in accessory -> shadow displayed indoor temp/humidity mirrored (C2)
    t = ResideoThermostat(shadow)
    assert t.indoor_temperature == 78
    assert t.indoor_humidity == 52


def test_sensor_remote_merge(device_shadow, rooms, fixture_loader) -> None:
    shadow, new_rooms = apply_live_feed(
        device_shadow, rooms, _feed(fixture_loader, "live_sensor_remote.json")
    )
    air = ResideoRooms(new_rooms).air_sensor_accessories()
    assert len(air) == 1
    _room, acc = air[0]
    assert acc.indoor_temperature == 70
    assert acc.indoor_humidity == 60
    assert acc.rssi == -55
    assert acc.battery_status == "Ok"  # survives (FIX #3)
    assert acc.occupancy_sensitivity == "Medium"  # survives
    assert acc.temperature_actual == 72.461  # survives
    # remote sensor -> the shadow's displayed indoor temp is NOT touched
    assert ResideoThermostat(shadow).indoor_temperature == 76.0


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
    # a Sensor push for a non-existent room/accessory -> no crash, no change
    ghost = ResideoLiveFeed(
        "AABBCCDDEEFF",
        "Sensor",
        {"RoomId": 9, "AccessoryId": 9, "AccessoryValue": {"IndoorTemperature": 1}},
        None,
    )
    _s, r2 = apply_live_feed(device_shadow, rooms, ghost)
    assert ResideoRooms(r2).rooms[0].accessories[0].indoor_temperature == 76.0


def test_merge_is_pure(device_shadow, rooms, fixture_loader) -> None:
    before_shadow = copy.deepcopy(device_shadow)
    before_rooms = copy.deepcopy(rooms)
    apply_live_feed(device_shadow, rooms, _feed(fixture_loader, "live_setpoint.json"))
    apply_live_feed(device_shadow, rooms, _feed(fixture_loader, "live_sensor_builtin.json"))
    assert device_shadow == before_shadow  # inputs untouched
    assert rooms == before_rooms
