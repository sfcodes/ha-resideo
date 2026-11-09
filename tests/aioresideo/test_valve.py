"""Water shutoff valve: model, events, merge, device classification, client calls.

Fixtures are hand-written (``shutoff_valve_info.json`` mirrors the shape of a live ``/info``
response with invented identifiers and round numbers; ``valve_events.json`` mirrors the
``WaterFlow`` / ``ActuatorValve`` / ``ChangeRequestSuccess`` pushes). No live API is touched.
"""

from __future__ import annotations

import json
import time
from copy import deepcopy
from datetime import datetime

import aiohttp
import pytest
from aioresponses import CallbackResult, aioresponses

from custom_components.resideo.aioresideo import (
    ResideoChangeConfirm,
    ResideoClient,
    ResideoShutoffValve,
    ResideoValveStatus,
    ResideoWaterFlow,
    apply_valve_event,
    normalize_valve_state,
    parse_event,
)
from custom_components.resideo.aioresideo.const import API_BASE_URL, SHUTOFF_VALVE_V1
from custom_components.resideo.aioresideo.exceptions import ResideoApiError
from custom_components.resideo.aioresideo.objects.account import ResideoAccountDevice

from .conftest import load_fixture

VALVE_MAC = "0A1B2C3D4E5F"
INFO_URL = f"{API_BASE_URL}{SHUTOFF_VALVE_V1.format(mac=VALVE_MAC)}/info"
VALVE_URL = f"{API_BASE_URL}{SHUTOFF_VALVE_V1.format(mac=VALVE_MAC)}/valve"


@pytest.fixture
def valve_info() -> dict:
    return load_fixture("shutoff_valve_info.json")


@pytest.fixture
def valve_events() -> dict:
    return load_fixture("valve_events.json")


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as s:
        yield s


def _client(session: aiohttp.ClientSession) -> ResideoClient:
    return ResideoClient(
        session, refresh_token="rt", access_token="at", expires_at=time.time() + 3600
    )


# -- model --------------------------------------------------------------------
def test_valve_info_model(valve_info: dict) -> None:
    v = ResideoShutoffValve(valve_info)
    assert v.name == "Main"
    assert v.model == "VWS03Y-3/4"
    assert v.firmware_version == "0.1.4.12"
    assert v.hardware_version == "HUKA0000000-00-A"
    assert v.serial_number == "S0000000000001"
    assert v.valve_state == "open"  # /info says "Open"
    assert v.command_source == "app"
    assert v.leak_status == "Ok"
    assert v.online is True
    assert v.last_check_in == datetime.fromisoformat("2026-02-03T04:05:06+00:00")
    assert v.flow_rate == 0.0
    assert v.water_total == 135000.5
    assert v.temperature == 60.0
    assert v.pressure == 62.5
    assert v.battery == 99.0
    assert v.on_battery_power is False
    assert v.consumed_this_period == 2200.5
    assert v.micro_leak_test_in_progress is False
    assert v.buzzer_active is False


def test_only_active_faults_count(valve_info: dict) -> None:
    v = ResideoShutoffValve(valve_info)
    assert [f["Code"] for f in v.active_faults] == [821]  # the Resolved one is excluded
    assert v.has_fault is True
    assert ResideoShutoffValve({**valve_info, "Faults": []}).has_fault is False


def test_empty_valve_is_all_none() -> None:
    v = ResideoShutoffValve({})
    assert v.valve_state is None
    assert v.online is False
    assert v.flow_rate is None
    assert v.last_check_in is None
    assert v.active_faults == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Open", "open"),
        ("open", "open"),
        ("close", "closed"),  # events say "close"
        ("Closed", "closed"),
        ("opening", "opening"),
        ("closing", "closing"),
        ("Bogus", None),
        (None, None),
        (3, None),
    ],
)
def test_normalize_valve_state(raw, expected) -> None:
    assert normalize_valve_state(raw) == expected


# -- events -------------------------------------------------------------------
def test_parse_water_flow(valve_events: dict) -> None:
    ev = parse_event(json.dumps(valve_events["water_flow"]))  # the stream sends a JSON string
    assert ev == ResideoWaterFlow(VALVE_MAC, 13.5, 135010.25, 61.5, 60.25)


def test_parse_valve_status(valve_events: dict) -> None:
    ev = parse_event(valve_events["valve_closed"])  # already-decoded dicts work too
    assert ev == ResideoValveStatus(VALVE_MAC, "close", "app")


def test_parse_valve_change_confirm(valve_events: dict) -> None:
    ev = parse_event(valve_events["change_ok"])
    assert isinstance(ev, ResideoChangeConfirm)
    assert ev.success is True
    assert ev.change_name == "actuatorValve"


def test_parse_water_flow_ignores_non_numeric(valve_events: dict) -> None:
    bad = deepcopy(valve_events["water_flow"])
    bad["Body"]["FlowRate"] = "n/a"
    bad["Body"]["Pressure"] = True  # a bool is not a reading
    ev = parse_event(bad)
    assert ev.flow_rate is None
    assert ev.pressure is None
    assert ev.water_total == 135010.25


# -- merge --------------------------------------------------------------------
def test_water_flow_merge_updates_readings_without_mutating(valve_info: dict) -> None:
    before = deepcopy(valve_info)
    valve = ResideoShutoffValve(valve_info)
    new = apply_valve_event(valve, ResideoWaterFlow(VALVE_MAC, 13.5, 135010.25, 61.5, 60.25))
    assert (new.flow_rate, new.water_total, new.temperature, new.pressure) == (
        13.5,
        135010.25,
        61.5,
        60.25,
    )
    assert new.valve_state == "open"  # untouched
    assert new.battery == 99.0  # everything else preserved
    assert valve.attributes == before  # input untouched


def test_water_flow_merge_does_not_clobber_with_none(valve_info: dict) -> None:
    valve = ResideoShutoffValve(valve_info)
    new = apply_valve_event(valve, ResideoWaterFlow(VALVE_MAC, 5.0, None, None, None))
    assert new.flow_rate == 5.0
    assert new.water_total == 135000.5
    assert new.pressure == 62.5


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("closing", "closing"), ("close", "closed"), ("opening", "opening"), ("open", "open")],
)
def test_valve_status_merge(valve_info: dict, raw: str, expected: str) -> None:
    valve = ResideoShutoffValve(valve_info)
    new = apply_valve_event(valve, ResideoValveStatus(VALVE_MAC, raw, "geofence"))
    assert new.valve_state == expected
    assert new.command_source == "geofence"
    assert valve.valve_state == "open"  # input untouched
    assert new.leak_status == "Ok"  # the rest of ActuatorValve survives


def test_unrecognised_valve_status_is_ignored(valve_info: dict) -> None:
    valve = ResideoShutoffValve(valve_info)
    new = apply_valve_event(valve, ResideoValveStatus(VALVE_MAC, "wobbling", "app"))
    assert new.valve_state == "open"


# -- device classification ----------------------------------------------------
def test_valve_is_not_a_thermostat_even_though_its_global_id_says_so() -> None:
    accounts = load_fixture("accounts_with_valve.json")
    devices = {d.mac: d for d in map(ResideoAccountDevice, ResideoClient.iter_devices(accounts))}
    valve = devices[VALVE_MAC]
    # The backend gives every device a LyricThermostatDevice node type...
    assert valve.device_kind == "LyricThermostatDevice"
    # ...so the model type, not the kind, has to decide.
    assert valve.is_shutoff_valve is True
    assert valve.is_thermostat is False
    assert devices["AABBCCDDEEFF"].is_thermostat is True
    assert devices["AABBCCDDEEFF"].is_shutoff_valve is False
    assert devices["DDEEFFAABBCC"].is_thermostat is False  # the smoke detector, still excluded
    assert devices["DDEEFFAABBCC"].is_shutoff_valve is False


@pytest.mark.parametrize("gdt", ["ShutoffValve_L7_T", "ShutoffValve_L5_T"])
def test_both_valve_models_are_recognised(gdt: str) -> None:
    assert ResideoAccountDevice({"globalDeviceType": gdt}).is_shutoff_valve is True


# -- client -------------------------------------------------------------------
async def test_get_shutoff_valve_info_uses_v1_info(session, valve_info: dict) -> None:
    with aioresponses() as m:
        m.get(INFO_URL, payload=valve_info)
        data = await _client(session).get_shutoff_valve_info(VALVE_MAC)
    assert data == valve_info


@pytest.mark.parametrize(("open_valve", "expected"), [(True, "Open"), (False, "Close")])
async def test_set_shutoff_valve_body(session, open_valve: bool, expected: str) -> None:
    sent: dict = {}

    def capture(url, **kwargs):
        sent.update(kwargs["json"])
        return CallbackResult(status=202, payload={"TransactionId": "tx-1"})

    with aioresponses() as m:
        m.put(VALVE_URL, callback=capture)
        result = await _client(session).set_shutoff_valve(VALVE_MAC, open_valve)

    assert result == {"TransactionId": "tx-1"}
    # CommandSource is mandatory (400 ParameterIsMissing without it); ValveStatus is explicit.
    assert sent == {
        "CommandSource": "app",
        "ValveStatus": expected,
        "ChannelId": "ds-notification-service",
    }


async def test_set_shutoff_valve_surfaces_a_rejection(session) -> None:
    body = [{"ErrorCode": "ParameterIsMissing", "ParamName": "CommandSource"}]
    with aioresponses() as m:
        m.put(VALVE_URL, status=400, payload=body)
        with pytest.raises(ResideoApiError) as err:
            await _client(session).set_shutoff_valve(VALVE_MAC, False)
    assert err.value.status == 400
    assert err.value.body == body
