"""Pure merge of SignalR LiveFeed deltas into the REST device-shadow / rooms dicts.

LiveFeed pushes are **partial deltas** in a **different shape** than the REST shadow (renamed
keys, different blocks). These functions deepcopy the raw dict(s), patch at the shadow's own key
paths, and return new dict(s) — **never mutating the inputs**. Only keys present (and non-None)
in the push are written (**no-clobber**). See ``resideo-api-spec.md`` §9 + the plan's mapping table.

Merged value-types are exactly :data:`LIVE_FEED_MERGED_PROPERTIES`: ``Setpoint``, ``OperationStatus``,
``SystemSwitch``, ``FanSwitch``, ``Sensor``, ``Rooms``, and the
``Displayed{Indoor,Outdoor}{Temperature,Humidity}`` family. ``Sensor`` needs special care: the
cloud labels each push with the identity of the *previous* accessory (see
:func:`_sensor_push_target`), so its header is decoded, not trusted. Every *other* LiveFeed type
(``Schedule*``, ``DrEventStatus``, ``DuctTemperature``, ``Groups``, ...) carries values we don't map
here — the coordinator re-reads REST (resync) when one arrives rather than guessing its shape — and
settings (Feels Like, Adaptive Recovery, ...) don't push values at all (they ride ``ChangeRequest``).
"""

from __future__ import annotations

from copy import deepcopy
from functools import partial
from itertools import pairwise
from typing import Any

from .objects.events import ResideoLiveFeed, ResideoValveStatus, ResideoWaterFlow
from .objects.valve import ResideoShutoffValve, normalize_valve_state


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


def _accessory_type(acc: dict[str, Any]) -> str | None:
    return (acc.get("AccessoryAttribute") or {}).get("Type")


def _sensor_push_target(rooms: dict[str, Any], value: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve which cached accessory a ``Sensor`` push's ``AccessoryValue`` belongs to.

    The cloud's ``Sensor`` notifications are **off by one**: each push's header (``RoomId`` /
    ``AccessoryId`` / ``AccessoryAttribute`` — type, name, serial) describes the accessory *before*
    the one whose ``AccessoryValue`` it carries. Verified live against REST with two remotes::

        header acc 0 (thermostat) + thermostat payload (CO2/VOC)  -> acc 0, the thermostat itself
        header acc 0 (thermostat) + remote payload (BatteryStatus) -> acc 1, the first remote
        header acc 1 (first remote, its real serial)               -> acc 2, the second remote

    With a single remote this is indistinguishable from "every push is labelled as the
    thermostat" (captured 9/9 in 2026-06 and again 2026-08-20). Trusting the header merged the
    remote's reading — and its ``OccupancyDet`` — into the thermostat's slot, so the thermostat's
    values and occupancy flapped with every burst, while the remote never received a push at all.

    Routing: a payload without ``BatteryStatus`` is the wall-powered thermostat's (only the
    battery remotes carry one); a remote payload belongs to the accessory *after* the header's in
    ``AccessoryId`` order. The target's cached ``Type`` must agree with the payload shape — if the
    header is unknown, has no successor, or the successor isn't a remote, the push is dropped
    rather than guessed (the REST resync covers it).
    """
    push_av = value.get("AccessoryValue")
    if not isinstance(push_av, dict):
        return None
    accessories = sorted(
        (
            acc
            for room in rooms.get("Rooms", []) or []
            for acc in room.get("Accessories", []) or []
            if acc.get("AccessoryId") is not None
        ),
        key=lambda acc: acc["AccessoryId"],
    )
    if "BatteryStatus" not in push_av:
        thermostats = [acc for acc in accessories if _accessory_type(acc) == "Thermostat"]
        return thermostats[0] if len(thermostats) == 1 else None
    header_id = value.get("AccessoryId")
    for header, successor in pairwise(accessories):
        if header["AccessoryId"] == header_id:
            return successor if _accessory_type(successor) == "IndoorAirSensor" else None
    return None


def _apply_sensor(shadow: dict[str, Any], rooms: dict[str, Any], value: dict[str, Any]) -> None:
    """Per-accessory values push -> the owning accessory (see :func:`_sensor_push_target`).

    ``DisplayedIndoorTemperature/Humidity`` are deliberately **not** derived from this push: they
    are the thermostat's *control* reading (priority-weighted, Feels-Like-adjusted — the number the
    app's header shows) and never equal any single sensor's raw value. They arrive via their own
    standalone pushes (:func:`_apply_displayed`) and the REST resync.
    """
    acc = _sensor_push_target(rooms, value)
    if acc is None:
        return
    # AccessoryValue.Id is off by one like the header — never stamp it onto the target.
    push_av = {k: v for k, v in value["AccessoryValue"].items() if k != "Id"}
    acc["AccessoryValue"] = _deep_merge_value(acc.get("AccessoryValue") or {}, push_av)


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

    The sole push-side writer of the ``Displayed{Indoor,Outdoor}{Temperature,Humidity}`` family
    (the outdoor pair is observed live every 30 min). ``Sensor`` status is ignored (no accessor
    consumes it).
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


def apply_valve_event(
    valve: ResideoShutoffValve, event: ResideoWaterFlow | ResideoValveStatus
) -> ResideoShutoffValve:
    """Apply one shutoff-valve push to a valve's ``/info`` snapshot (input untouched).

    ``WaterFlow`` overwrites the four live readings it carries (a ``None`` field is left alone —
    no-clobber). ``ActuatorValve`` sets the position, but only when it is one we recognise, so an
    unfamiliar status string can't blank the entity.
    """
    attrs = deepcopy(valve.attributes)
    if isinstance(event, ResideoWaterFlow):
        for key, value in (
            ("FlowRate", event.flow_rate),
            ("WaterTotal", event.water_total),
            ("Temperature", event.temperature),
            ("Pressure", event.pressure),
        ):
            if value is not None:
                attrs[key] = value
    elif isinstance(event, ResideoValveStatus):
        if normalize_valve_state(event.status) is not None:
            actuator = attrs.get("ActuatorValve")
            actuator = dict(actuator) if isinstance(actuator, dict) else {}
            actuator["ValveStatus"] = event.status
            if event.command_source:
                actuator["CommandSource"] = event.command_source
            attrs["ActuatorValve"] = actuator
    return ResideoShutoffValve(attrs)
