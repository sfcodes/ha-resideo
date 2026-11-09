"""Shutoff valve end to end through Home Assistant: entities, push, commands, setup shapes."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.resideo.aioresideo import (
    ResideoChangeConfirm,
    ResideoValveStatus,
    ResideoWaterFlow,
)
from custom_components.resideo.aioresideo.exceptions import (
    ResideoApiError,
    ResideoConnectionError,
)
from custom_components.resideo.const import DOMAIN
from custom_components.resideo.diagnostics import async_get_config_entry_diagnostics

from .conftest import (
    MAC,
    VALVE_MAC,
    advance_time,
    eid,
    load_fixture_json,
    setup_integration,
)

# 1 US gallon in litres — the test harness runs in US units, so HA shows litres as gallons.
L_PER_GAL = 3.785411784


@pytest.fixture
def accounts_data() -> dict:
    """The thermostat account plus an L7 shutoff valve (overrides the conftest default)."""
    return load_fixture_json("accounts_with_valve.json")


def _valve_only(accounts: dict) -> None:
    """Strip every device but the valve out of the (lazily-read) accounts fixture."""
    devices = accounts["data"]["consumerUsers"][0]["consumerAccount"]["locations"][0][
        "consumerDevices"
    ]
    devices[:] = [d for d in devices if d["device"]["deviceId"] == VALVE_MAC]


def _state(hass: HomeAssistant, platform: str, key: str):
    return hass.states.get(eid(hass, platform, f"{VALVE_MAC}_{key}"))


async def test_valve_entity_reports_initial_position(
    hass: HomeAssistant, init_integration
) -> None:
    """The initial position comes from the ``/info`` read, since the stream only pushes changes."""
    state = _state(hass, "valve", "valve")
    assert state.state == "open"
    assert state.attributes["device_class"] == "water"
    assert state.attributes["last_command_source"] == "app"
    # The thermostat is untouched by the valve's presence.
    assert hass.states.get(eid(hass, "sensor", f"{MAC}_indoor_temperature")).state == "76.0"


async def test_valve_initially_closed(hass: HomeAssistant, mock_config_entry, mock_api, valve_info) -> None:
    valve_info["ActuatorValve"]["ValveStatus"] = "Closed"
    await setup_integration(hass, mock_config_entry, mock_api)
    assert _state(hass, "valve", "valve").state == "closed"


async def test_valve_sensors(hass: HomeAssistant, init_integration) -> None:
    # Flow stays in L/min (HA only converts the water-volume sensors for US units) — as the app shows.
    flow = _state(hass, "sensor", "flow_rate")
    assert flow.attributes["device_class"] == "volume_flow_rate"
    assert (flow.state, flow.attributes["unit_of_measurement"]) == ("0.0", "L/min")

    total = _state(hass, "sensor", "water_total")
    assert total.attributes["unit_of_measurement"] == "gal"
    assert float(total.state) == pytest.approx(135000.5 / L_PER_GAL, rel=1e-3)
    assert total.attributes["state_class"] == "total_increasing"

    period = _state(hass, "sensor", "water_usage_period")
    assert float(period.state) == pytest.approx(2200.5 / L_PER_GAL, rel=1e-3)

    # °F and psi are already the US unit system's units.
    temp = _state(hass, "sensor", "water_temperature")
    assert (temp.state, temp.attributes["unit_of_measurement"]) == ("60.0", "°F")
    pressure = _state(hass, "sensor", "water_pressure")
    assert (pressure.state, pressure.attributes["unit_of_measurement"]) == ("62.5", "psi")

    assert _state(hass, "sensor", "battery").state == "99.0"
    assert _state(hass, "sensor", "leak_status").state == "Ok"
    assert _state(hass, "sensor", "fault_count").state == "1"
    assert _state(hass, "sensor", "last_check_in").state.startswith("2026-02-03T04:05:06")


async def test_valve_binary_sensors(hass: HomeAssistant, init_integration) -> None:
    assert _state(hass, "binary_sensor", "online").state == "on"
    assert _state(hass, "binary_sensor", "fault").state == "on"  # the active code-821 fault
    assert _state(hass, "binary_sensor", "on_battery_power").state == "off"
    assert _state(hass, "binary_sensor", "micro_leak_test_running").state == "off"
    assert _state(hass, "binary_sensor", "buzzer").state == "off"


async def test_valve_device_info(hass: HomeAssistant, init_integration) -> None:
    device = dr.async_get(hass).async_get_device(identifiers={("resideo", VALVE_MAC)})
    assert device is not None
    assert device.name == "Main"
    assert device.model == "VWS03Y-3/4"
    assert device.manufacturer == "Resideo"
    assert device.sw_version == "0.1.4.12"
    assert device.hw_version == "HUKA0000000-00-A"
    assert device.serial_number == "S0000000000001"


# -- push ----------------------------------------------------------------------
async def test_actuator_events_drive_the_valve_state(
    hass: HomeAssistant, init_integration, mock_api
) -> None:
    stream = mock_api.streams[0]
    for raw, expected in [
        ("closing", "closing"),
        ("close", "closed"),
        ("opening", "opening"),
        ("open", "open"),
    ]:
        stream.on_event(ResideoValveStatus(VALVE_MAC, raw, "app"))
        await hass.async_block_till_done()
        assert _state(hass, "valve", "valve").state == expected


async def test_water_flow_event_updates_sensors(
    hass: HomeAssistant, init_integration, mock_api
) -> None:
    mock_api.streams[0].on_event(ResideoWaterFlow(VALVE_MAC, 13.5, 135010.25, 61.5, 60.25))
    await hass.async_block_till_done()

    assert _state(hass, "sensor", "water_temperature").state == "61.5"
    assert _state(hass, "sensor", "water_pressure").state == "60.25"
    flow = _state(hass, "sensor", "flow_rate")
    assert (flow.state, flow.attributes["unit_of_measurement"]) == ("13.5", "L/min")
    assert float(_state(hass, "sensor", "water_total").state) == pytest.approx(
        135010.25 / L_PER_GAL, rel=1e-3
    )


async def test_event_for_unknown_valve_is_ignored(
    hass: HomeAssistant, init_integration, mock_api
) -> None:
    mock_api.streams[0].on_event(ResideoValveStatus("FFFFFFFFFFFF", "close", "app"))
    await hass.async_block_till_done()
    assert _state(hass, "valve", "valve").state == "open"


async def test_valve_change_confirm_does_not_resync(
    hass: HomeAssistant, init_integration, mock_api
) -> None:
    """A ChangeRequestSuccess carries no value; re-reading ``/info`` would race the position push."""
    mock_api.streams[0].on_event(ResideoValveStatus(VALVE_MAC, "close", "app"))
    await hass.async_block_till_done()
    calls = mock_api.async_get_shutoff_valve.await_count

    mock_api.streams[0].on_event(
        ResideoChangeConfirm(VALVE_MAC, "tx", "actuatorValve", "AppInitiated", True)
    )
    await advance_time(hass, 10)

    assert mock_api.async_get_shutoff_valve.await_count == calls
    assert _state(hass, "valve", "valve").state == "closed"  # not clobbered by a stale read


async def test_rejected_valve_change_resyncs(
    hass: HomeAssistant, init_integration, mock_api, valve_info
) -> None:
    """A rejected command must roll the optimistic ``closing`` back to the device's truth."""
    await hass.services.async_call(
        "valve", "close_valve", {"entity_id": eid(hass, "valve", f"{VALVE_MAC}_valve")}, blocking=True
    )
    assert _state(hass, "valve", "valve").state == "closing"

    mock_api.streams[0].on_event(
        ResideoChangeConfirm(VALVE_MAC, "tx", "actuatorValve", "AppInitiated", False)
    )
    await advance_time(hass, 10)

    assert _state(hass, "valve", "valve").state == "open"  # /info still says Open


# -- commands ------------------------------------------------------------------
async def test_close_valve_service(hass: HomeAssistant, init_integration, mock_api) -> None:
    valve_id = eid(hass, "valve", f"{VALVE_MAC}_valve")
    await hass.services.async_call("valve", "close_valve", {"entity_id": valve_id}, blocking=True)

    mock_api.async_set_shutoff_valve.assert_awaited_once_with(VALVE_MAC, False)
    assert hass.states.get(valve_id).state == "closing"  # shown at once, not after the push


async def test_open_valve_service(
    hass: HomeAssistant, mock_config_entry, mock_api, valve_info
) -> None:
    valve_info["ActuatorValve"]["ValveStatus"] = "Closed"
    await setup_integration(hass, mock_config_entry, mock_api)
    valve_id = eid(hass, "valve", f"{VALVE_MAC}_valve")

    await hass.services.async_call("valve", "open_valve", {"entity_id": valve_id}, blocking=True)

    mock_api.async_set_shutoff_valve.assert_awaited_once_with(VALVE_MAC, True)
    assert hass.states.get(valve_id).state == "opening"


async def test_command_failure_raises_and_leaves_state(
    hass: HomeAssistant, init_integration, mock_api
) -> None:
    mock_api.async_set_shutoff_valve.side_effect = ResideoApiError(
        "400", status=400, body=[{"ErrorCode": "ParameterIsMissing"}]
    )
    valve_id = eid(hass, "valve", f"{VALVE_MAC}_valve")
    with pytest.raises(HomeAssistantError, match="Could not close the valve"):
        await hass.services.async_call(
            "valve", "close_valve", {"entity_id": valve_id}, blocking=True
        )
    assert hass.states.get(valve_id).state == "open"  # no optimistic move on a failed command


async def test_fallback_reread_converges_if_the_stream_stays_silent(
    hass: HomeAssistant, init_integration, mock_api, valve_info
) -> None:
    """Closing is shown as ``closing``; if no ``closed`` push arrives, a re-read settles it."""
    valve_id = eid(hass, "valve", f"{VALVE_MAC}_valve")
    await hass.services.async_call("valve", "close_valve", {"entity_id": valve_id}, blocking=True)
    assert hass.states.get(valve_id).state == "closing"

    valve_info["ActuatorValve"]["ValveStatus"] = "Closed"  # what the device now reports
    await advance_time(hass, 31)

    assert hass.states.get(valve_id).state == "closed"


# -- setup shapes --------------------------------------------------------------
async def test_stream_only_activates_thermostats(
    hass: HomeAssistant, init_integration, mock_api
) -> None:
    """The ``/priority`` activation read is a thermostat concept; a valve pushes on its own."""
    stream = mock_api.streams[0]
    assert set(stream.device_ids) == {MAC, VALVE_MAC}  # subscribed for both
    assert stream.activate_ids == [MAC]  # activated for the thermostat only


async def test_valve_only_account_sets_up(
    hass: HomeAssistant, mock_config_entry, mock_api, accounts_data
) -> None:
    """No thermostat at all: previously a hard setup error, now a working valve integration."""
    _valve_only(accounts_data)
    await setup_integration(hass, mock_config_entry, mock_api)

    assert _state(hass, "valve", "valve").state == "open"
    mock_api.async_get_device.assert_not_called()
    stream = mock_api.streams[0]
    assert stream.device_ids == [VALVE_MAC]
    assert stream.activate_ids == []  # nothing to activate

    stream.on_event(ResideoValveStatus(VALVE_MAC, "close", "app"))
    await hass.async_block_till_done()
    assert _state(hass, "valve", "valve").state == "closed"  # data is empty; push must still work


async def test_valve_read_failure_at_setup_isolated_from_thermostat(
    hass: HomeAssistant, mock_config_entry, mock_api, valve_infos
) -> None:
    """One failing device must not take down the others (same rule as multi-thermostat)."""
    valve_infos[VALVE_MAC] = ResideoConnectionError("valve cloud down")
    await setup_integration(hass, mock_config_entry, mock_api)

    assert hass.states.get(eid(hass, "sensor", f"{MAC}_indoor_temperature")).state == "76.0"
    # No snapshot, so no entity (and no crash).
    assert er.async_get(hass).async_get_entity_id("valve", DOMAIN, f"{VALVE_MAC}_valve") is None


async def test_offline_valve_is_unavailable_but_reports_connectivity(
    hass: HomeAssistant, mock_config_entry, mock_api, valve_info
) -> None:
    valve_info["Online"] = False
    await setup_integration(hass, mock_config_entry, mock_api)

    assert _state(hass, "valve", "valve").state == "unavailable"
    assert _state(hass, "sensor", "water_pressure").state == "unavailable"
    assert _state(hass, "binary_sensor", "online").state == "off"  # reports it, stays available


async def test_diagnostics_include_a_redacted_valve(
    hass: HomeAssistant, init_integration
) -> None:
    diag = await async_get_config_entry_diagnostics(hass, init_integration)

    assert len(diag["valves"]) == 1
    valve = diag["valves"][0]
    assert valve["DeviceSerialNumber"] == "**REDACTED**"
    assert valve["DevModel"] == "VWS03Y-3/4"  # the model is not an identifier
    assert valve["ActuatorValve"]["ValveStatus"] == "Open"


# -- Energy dashboard ----------------------------------------------------------
def _validate_water(hass: HomeAssistant, source: dict, stats: set[str]) -> dict:
    """Run Home Assistant's own Energy-dashboard water validation against live entities.

    Statistics metadata and the recorder's entity filter are stubbed (they are about the
    database, not our entities); every check on the entity's state, device class, unit and
    state class is Home Assistant's own.
    """
    from homeassistant.components.energy import validate

    issues = validate.ValidationIssues()
    calls: list = []
    metadata = {stat: (1, {}) for stat in stats}
    with patch.object(validate.recorder, "is_entity_recorded", return_value=True):
        validate._validate_water_source(hass, source, metadata, set(), issues, calls)
        for call in calls:
            call()
    return issues.issues


def _water_source(total: str, rate: str | None = None) -> dict:
    return {
        "type": "water",
        "stat_energy_from": total,
        "stat_cost": None,
        "entity_energy_price": None,
        "number_energy_price": None,
        "stat_rate": rate,
    }


async def test_energy_dashboard_accepts_water_total_and_flow_rate(
    hass: HomeAssistant, init_integration
) -> None:
    """The total is a valid water source and the flow rate its live-flow sensor — by HA's rules."""
    total = eid(hass, "sensor", f"{VALVE_MAC}_water_total")
    flow = eid(hass, "sensor", f"{VALVE_MAC}_flow_rate")

    assert _validate_water(hass, _water_source(total, flow), {total, flow}) == {}


async def test_energy_dashboard_accepts_period_usage_as_a_water_device(
    hass: HomeAssistant, init_integration
) -> None:
    """Billing-period usage resets monthly; ``total_increasing`` is what lets that reset through."""
    period = eid(hass, "sensor", f"{VALVE_MAC}_water_usage_period")

    assert _validate_water(hass, _water_source(period), {period}) == {}


async def test_energy_water_validation_is_not_vacuous(
    hass: HomeAssistant, init_integration
) -> None:
    """Negative control: sensors that are *not* water/flow must be flagged by the same check."""
    temp = eid(hass, "sensor", f"{VALVE_MAC}_water_temperature")
    pressure = eid(hass, "sensor", f"{VALVE_MAC}_water_pressure")

    issues = _validate_water(hass, _water_source(temp, pressure), {temp, pressure})

    assert "entity_unexpected_device_class" in issues
    assert {e for e, _ in issues["entity_unexpected_device_class"].affected_entities} == {
        temp,
        pressure,
    }


async def test_water_sensors_stay_valid_after_push_updates(
    hass: HomeAssistant, init_integration, mock_api
) -> None:
    """A live WaterFlow push must not change the attributes the dashboard validates."""
    total = eid(hass, "sensor", f"{VALVE_MAC}_water_total")
    flow = eid(hass, "sensor", f"{VALVE_MAC}_flow_rate")

    mock_api.streams[0].on_event(ResideoWaterFlow(VALVE_MAC, 13.5, 135010.25, 61.5, 60.25))
    await hass.async_block_till_done()

    assert float(hass.states.get(flow).state) == 13.5
    assert _validate_water(hass, _water_source(total, flow), {total, flow}) == {}
