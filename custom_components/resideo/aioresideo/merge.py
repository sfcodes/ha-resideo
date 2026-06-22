"""Pure merge of SignalR LiveFeed deltas into the REST device-shadow / rooms dicts.

LiveFeed pushes are **partial deltas** in a **different shape** than the REST shadow (renamed
keys, different blocks). These functions deepcopy the raw dict(s), patch at the shadow's own key
paths, and return new dict(s) — **never mutating the inputs**. Only keys present (and non-None)
in the push are written (**no-clobber**). See ``resideo-api-spec.md`` §9 + the plan's mapping table.

Merged value-types are exactly :data:`LIVE_FEED_MERGED_PROPERTIES`: ``Setpoint``, ``OperationStatus``,
``SystemSwitch``, ``FanSwitch``, ``Sensor``, ``Rooms``, and the
``Displayed{Indoor,Outdoor}{Temperature,Humidity}`` family. Every *other* LiveFeed type
(``Schedule*``, ``DrEventStatus``, ``DuctTemperature``, ``Groups``, ...) carries values we don't map
here — the coordinator re-reads REST (resync) when one arrives rather than guessing its shape — and
settings (Feels Like, Adaptive Recovery, ...) don't push values at all (they ride ``ChangeRequest``).
"""

from __future__ import annotations

from copy import deepcopy
from functools import partial
from typing import Any

from .objects.events import ResideoLiveFeed


def apply_live_feed(
    shadow_raw: dict[str, Any],
    rooms_raw: dict[str, Any],
    feed: ResideoLiveFeed,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply one LiveFeed push, returning ``(new_shadow, new_rooms)`` (inputs untouched)."""
    shadow = deepcopy(shadow_raw or {})
    rooms = deepcopy(rooms_raw or {})
    value = feed.value
    if not isinstance(value, dict):
        return shadow, rooms
    handler = _DISPATCH.get(feed.property_name)
    if handler is not None:
        handler(shadow, rooms, value)
    return shadow, rooms


# -- helpers ------------------------------------------------------------------
def _reported(shadow: dict[str, Any]) -> dict[str, Any]:
    return shadow.setdefault("Reported", {})


def _set_if_present(
    dst: dict[str, Any], dst_key: str, src: dict[str, Any], src_key: str
) -> None:
    if src.get(src_key) is not None:
        dst[dst_key] = src[src_key]


def _deep_merge_value(existing: dict[str, Any], push: dict[str, Any]) -> dict[str, Any]:
    """One-level-deep merge of an ``AccessoryValue`` subset.

    Per key: merge nested dicts (so keys the push omits survive — FIX #3), otherwise overwrite.
    For ``{Measurement, Displayed}`` measurement blocks the push carries only ``Measurement``;
    keep/default ``Displayed`` to ``True`` so CO2/TVOC don't silently vanish (FIX #4).
    """
    out = dict(existing)
    for key, new_val in push.items():
        if new_val is None:
            continue
        old_val = out.get(key)
        if isinstance(new_val, dict):
            merged = {**old_val, **new_val} if isinstance(old_val, dict) else dict(new_val)
            if "Measurement" in merged and "Displayed" not in merged:
                merged["Displayed"] = True
            out[key] = merged
        else:
            out[key] = new_val
    return out


# -- per-property appliers ----------------------------------------------------
# Every applier takes ``(shadow, rooms, value)``; each ignores the dict it doesn't need.
def _apply_setpoint(shadow: dict[str, Any], rooms: dict[str, Any], value: dict[str, Any]) -> None:
    rep = _reported(shadow)
    sp = rep.setdefault("Setpoint", {})
    _set_if_present(sp, "SetpointStatus", value, "Status")  # rename Status -> SetpointStatus
    _set_if_present(sp, "HeatSetpoint", value, "HeatSetpoint")
    _set_if_present(sp, "CoolSetpoint", value, "CoolSetpoint")
    fan = value.get("FanSwitch")
    if isinstance(fan, dict) and fan.get("Position") is not None:
        pos = fan["Position"]
        sp.setdefault("FanSwitch", {})["Position"] = pos
        rep.setdefault("FanSwitch", {})["Position"] = pos  # device.fan_position reads top-level first
    # Ignore Priority (different shape, no consumer) + DevicePreferredTemperatureUnits.


def _apply_operation_status(
    shadow: dict[str, Any], rooms: dict[str, Any], value: dict[str, Any]
) -> None:
    rep = _reported(shadow)
    op = rep.setdefault("OperationStatus", {})
    _set_if_present(op, "Mode", value, "Mode")
    _set_if_present(op, "FanRequest", value, "Fan")  # rename Fan -> FanRequest
    _set_if_present(op, "CirculationFanRequest", value, "CircFan")  # rename CircFan
    if value.get("curStg") is not None:
        rep.setdefault("HeatAndCoolDemand", {})["CurrentStage"] = value["curStg"]  # rename + block
    # Demand / StagesOn are not in the push — leave them for the next resync (don't zero).


def _apply_system_switch(
    shadow: dict[str, Any], rooms: dict[str, Any], value: dict[str, Any]
) -> None:
    rep = _reported(shadow)
    _set_if_present(rep, "SystemSwitch", value, "SystemSwitch")
    _set_if_present(rep, "HeatCoolMode", value, "HeatCoolMode")


def _apply_fan_switch(shadow: dict[str, Any], rooms: dict[str, Any], value: dict[str, Any]) -> None:
    pos = value.get("Position")
    if pos is None:
        return
    rep = _reported(shadow)
    fan = rep.setdefault("FanSwitch", {})
    fan["Position"] = pos
    if value.get("Speed") is not None:
        fan["Speed"] = value["Speed"]
    rep.setdefault("Setpoint", {}).setdefault("FanSwitch", {})["Position"] = pos


def _apply_sensor(shadow: dict[str, Any], rooms: dict[str, Any], value: dict[str, Any]) -> None:
    room_id = value.get("RoomId")
    accessory_id = value.get("AccessoryId")
    push_av = value.get("AccessoryValue")
    if not isinstance(push_av, dict):
        return
    # Route by NUMERIC ids — never by Type (the push's AccessoryAttribute.Type is "TS", not the
    # REST "Thermostat"/"IndoorAirSensor"; matching on it would misroute / break is_thermostat).
    for room in rooms.get("Rooms", []) or []:
        if room.get("Id") != room_id:
            continue
        for acc in room.get("Accessories", []) or []:
            if acc.get("AccessoryId") != accessory_id:
                continue
            acc["AccessoryValue"] = _deep_merge_value(acc.get("AccessoryValue") or {}, push_av)
            # Built-in thermostat -> mirror displayed indoor temp/humidity into the shadow.
            # Test the EXISTING accessory's Type (never overwritten), not the push's "TS".
            if (acc.get("AccessoryAttribute") or {}).get("Type") == "Thermostat":
                rep = _reported(shadow)
                _set_if_present(rep, "DisplayedIndoorTemperature", push_av, "IndoorTemperature")
                _set_if_present(rep, "DisplayedIndoorHumidity", push_av, "IndoorHumidity")
            return


def _apply_rooms(shadow: dict[str, Any], rooms: dict[str, Any], value: dict[str, Any]) -> None:
    """Room-aggregate push: ``{"PropertyName":"<roomId>","Value":{Id,Name,...,AvgTemperature,...}}``.

    Updates the matching ``rooms["Rooms"][i]`` aggregate fields (avg temp/humidity/motion/name/type);
    **never touches ``Accessories``** (the push omits them — preserve the per-sensor values merged by
    ``_apply_sensor``). No-op if the room isn't already cached (a new room arrives on the resync).
    """
    inner = value.get("Value")
    if not isinstance(inner, dict):
        return
    room_id = inner.get("Id")
    if room_id is None:  # fall back to the outer PropertyName (the room id as a string)
        pn = value.get("PropertyName")
        room_id = int(pn) if isinstance(pn, str) and pn.lstrip("-").isdigit() else None
    if room_id is None:
        return
    for room in rooms.get("Rooms", []) or []:
        if room.get("Id") != room_id:
            continue
        for key, new_val in inner.items():
            if key == "Accessories" or new_val is None:
                continue
            room[key] = new_val
        return


def _apply_displayed(
    shadow: dict[str, Any], rooms: dict[str, Any], value: dict[str, Any], *, key: str
) -> None:
    """Standalone displayed-value push ``{"Value": <n>, "Sensor": "Ok"}`` -> ``Reported.<key>``.

    Covers ``DisplayedIndoorTemperature/Humidity`` (also mirrored from the ``Sensor`` push) and
    ``DisplayedOutdoorTemperature/Humidity`` (only delivered this way). ``Sensor`` status is ignored
    (no accessor consumes it).
    """
    v = value.get("Value")
    if v is not None:
        _reported(shadow)[key] = v


# -- dispatch (the authoritative set of merged property types) -----------------
_DISPATCH = {
    "Setpoint": _apply_setpoint,
    "OperationStatus": _apply_operation_status,
    "SystemSwitch": _apply_system_switch,
    "FanSwitch": _apply_fan_switch,
    "Sensor": _apply_sensor,
    "Rooms": _apply_rooms,
    "DisplayedIndoorTemperature": partial(_apply_displayed, key="DisplayedIndoorTemperature"),
    "DisplayedIndoorHumidity": partial(_apply_displayed, key="DisplayedIndoorHumidity"),
    "DisplayedOutdoorTemperature": partial(_apply_displayed, key="DisplayedOutdoorTemperature"),
    "DisplayedOutdoorHumidity": partial(_apply_displayed, key="DisplayedOutdoorHumidity"),
}

#: LiveFeed property names merged in-memory by :func:`apply_live_feed`. The coordinator resyncs
#: (re-reads REST) for any LiveFeed whose ``PropertyName`` is **not** in this set.
LIVE_FEED_MERGED_PROPERTIES = frozenset(_DISPATCH)
