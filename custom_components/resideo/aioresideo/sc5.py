"""Read-only SC5 discovery and snapshot interpretation.

Source evidence: Zack Wagner's RESIDEO_API.md and rmarr's Homebridge PR #4.
This module is a prototype for the upstream Resideo client, without push handling.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from .client import ResideoClient

SC5_TYPE = "Citadel_SC5"
_SMOKE_ACTIVE = frozenset({
    "alarm",
    "smokeAlarm",
    "smokeEarlyWarning",
    "smokeInterconnectAlarm",
    "smokeEarlyWarningInterconnectAlarm",
})
_CO_ACTIVE = frozenset({
    "alarm",
    "coAlarm",
    "coEarlyWarning",
    "carbonMonoxideAlarm",
    "carbonMonoxideEarlyWarning",
    "carbonMonoxideInterconnectAlarm",
    "carbonMonoxideEarlyWarningInterconnectAlarm",
})


@dataclass(frozen=True, slots=True)
class SC5Device:
    """An SC5 identified in the existing account graph."""

    device_id: str
    name: str
    location: str | None


@dataclass(frozen=True, slots=True)
class SC5Snapshot:
    """An individual snapshot; None indicates unknown or untrusted data."""

    online: bool | None
    smoke: bool | None
    carbon_monoxide: bool | None
    battery_condition: str | None
    power_source: str | None


def discover_sc5(accounts: dict[str, Any]) -> tuple[SC5Device, ...]:
    """Filter the shared account discovery results across all locations."""
    devices: dict[str, SC5Device] = {}
    for device in ResideoClient.iter_devices(accounts):
        device_id = device.get("deviceId")
        if device.get("globalDeviceType") != SC5_TYPE or not isinstance(device_id, str):
            continue
        if not device_id:
            continue
        name, location = device.get("name"), device.get("location")
        devices.setdefault(
            device_id,
            SC5Device(
                device_id,
                name if isinstance(name, str) and name else device_id,
                location if isinstance(location, str) else None,
            ),
        )
    return tuple(devices.values())


def _child(parent: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = parent.get(key)
    return value if isinstance(value, Mapping) else {}


def _state(alarm: Mapping[str, Any], kind: str) -> str | None:
    value = _child(alarm, kind).get("deviceState")
    return value if isinstance(value, str) else None


def _alarm_value(state: str | None, active: frozenset[str], online: bool | None) -> bool | None:
    if online is not True:
        return None
    if state in active:
        return True
    if state == "idle":
        return False
    return None


def parse_sc5_snapshot(payload: Mapping[str, Any]) -> SC5Snapshot:
    """Read the cloud snapshot without treating absent values as clear."""
    reported = _child(_child(payload, "deviceState"), "reported")
    alarm = _child(reported, "alarmState")
    connectivity = (payload.get("isOnlineComputed"), payload.get("isOnline"))
    online = False if any(value is False for value in connectivity) else (
        True if any(value is True for value in connectivity) else None
    )
    battery = _state(alarm, "battery")
    power = _state(alarm, "power")
    return SC5Snapshot(
        online=online,
        smoke=_alarm_value(_state(alarm, "smoke"), _SMOKE_ACTIVE, online),
        carbon_monoxide=_alarm_value(_state(alarm, "co"), _CO_ACTIVE, online),
        battery_condition=battery if battery in {"good", "low", "replace", "critical"} else None,
        power_source=power if power in {"ac", "dc"} else None,
    )


async def async_read_sc5(client: ResideoClient, device: SC5Device) -> SC5Snapshot:
    """Read state through upstream's authenticated client."""
    device_id = quote(device.device_id, safe="")
    payload = await client.get(f"/ris-public-api/api/v2/devices/smokeDetectors/{device_id}/state")
    if not isinstance(payload, Mapping):
        raise ValueError("Unexpected Resideo SC5 state response")
    return parse_sc5_snapshot(payload)

