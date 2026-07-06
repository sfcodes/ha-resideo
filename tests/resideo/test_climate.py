"""Climate entity: state mapping, services, hold-status parity, optimistic writes."""

from __future__ import annotations

import pytest
from homeassistant.components.climate import (
    ATTR_HVAC_ACTION,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from .conftest import MAC, advance_time, eid, setup_integration


@pytest.fixture
def climate_id(hass: HomeAssistant, init_integration) -> str:
    return eid(hass, "climate", f"{MAC}_climate")


async def test_state_mapping(hass: HomeAssistant, climate_id: str) -> None:
    state = hass.states.get(climate_id)
    assert state.state == HVACMode.COOL
    attrs = state.attributes
    assert attrs["current_temperature"] == 76.0
    assert attrs["current_humidity"] == 56
    assert attrs["temperature"] == 76.0  # cool setpoint in COOL mode
    assert attrs["fan_mode"] == "auto"
    assert attrs["fan_modes"] == ["auto", "Circulate", "on"]  # app order
    assert attrs[ATTR_HVAC_ACTION] == HVACAction.COOLING
    assert attrs["min_temp"] == 50.0
    assert attrs["max_temp"] == 90.0
    # Schedule is OFF -> only one reachable preset -> no preset selector offered
    # (and therefore no preset_mode attribute in the state).
    features = ClimateEntityFeature(attrs["supported_features"])
    assert not features & ClimateEntityFeature.PRESET_MODE
    assert "preset_mode" not in attrs
    assert features & ClimateEntityFeature.TARGET_TEMPERATURE
    assert features & ClimateEntityFeature.FAN_MODE
    assert features & ClimateEntityFeature.TURN_OFF
    assert features & ClimateEntityFeature.TURN_ON
    # EmergencyHeat maps to no HA mode; the rest come from SystemSwitchCapabilities.
    assert state.attributes["hvac_modes"] == [
        HVACMode.OFF,
        HVACMode.HEAT,
        HVACMode.COOL,
        HVACMode.HEAT_COOL,
    ]


async def test_turn_off_and_on_services(
    hass: HomeAssistant, climate_id: str, mock_api
) -> None:
    await hass.services.async_call(
        "climate", "turn_off", {"entity_id": climate_id}, blocking=True
    )
    mock_api.async_set_system_switch.assert_awaited_with(MAC, "Off")
    assert hass.states.get(climate_id).state == HVACMode.OFF  # optimistic

    await hass.services.async_call(
        "climate", "turn_on", {"entity_id": climate_id}, blocking=True
    )
    # The ClimateEntity default picks the first of (heat_cool, heat, cool) available.
    mock_api.async_set_system_switch.assert_awaited_with(MAC, "Auto")


async def test_set_temperature_with_schedule_off_uses_permanent_hold(
    hass: HomeAssistant, climate_id: str, mock_api
) -> None:
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": climate_id, "temperature": 74},
        blocking=True,
    )
    mock_api.async_set_cool_setpoint.assert_awaited_once_with(
        MAC, 74.0, status="PermanentHold"
    )
    assert hass.states.get(climate_id).attributes["temperature"] == 74.0  # optimistic


async def test_set_temperature_following_schedule_uses_temporary_hold(
    hass: HomeAssistant, mock_config_entry, mock_api, device_shadow
) -> None:
    device_shadow["Reported"]["ScheduleEnabled"] = True
    device_shadow["Reported"]["Setpoint"]["SetpointStatus"] = "NoHold"
    await setup_integration(hass, mock_config_entry, mock_api)
    climate_id = eid(hass, "climate", f"{MAC}_climate")

    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": climate_id, "temperature": 74},
        blocking=True,
    )
    # App parity: while following the schedule, a setpoint change is a temporary hold.
    mock_api.async_set_cool_setpoint.assert_awaited_once_with(
        MAC, 74.0, status="TemporaryHold"
    )
    assert hass.states.get(climate_id).attributes["preset_mode"] == "temporary_hold"


async def test_set_temperature_range_in_auto(
    hass: HomeAssistant, climate_id: str, mock_api
) -> None:
    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": climate_id, "hvac_mode": HVACMode.HEAT_COOL},
        blocking=True,
    )
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": climate_id, "target_temp_low": 64, "target_temp_high": 78},
        blocking=True,
    )
    mock_api.async_set_cool_setpoint.assert_awaited_once_with(
        MAC, 78.0, status="PermanentHold"
    )
    mock_api.async_set_heat_setpoint.assert_awaited_once_with(
        MAC, 64.0, status="PermanentHold"
    )
    attrs = hass.states.get(climate_id).attributes
    assert attrs["target_temp_low"] == 64.0
    assert attrs["target_temp_high"] == 78.0


async def test_set_temperature_while_off_is_rejected(
    hass: HomeAssistant, climate_id: str, mock_api
) -> None:
    await hass.services.async_call(
        "climate", "turn_off", {"entity_id": climate_id}, blocking=True
    )
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": climate_id, "temperature": 74},
            blocking=True,
        )
    mock_api.async_set_cool_setpoint.assert_not_awaited()
    mock_api.async_set_heat_setpoint.assert_not_awaited()


async def test_unsettable_preset_is_rejected(
    hass: HomeAssistant, mock_config_entry, mock_api, device_shadow
) -> None:
    # Schedule ON so the preset selector exists and OUR validation (not HA's feature
    # gate) is what rejects the read-only vacation preset.
    device_shadow["Reported"]["ScheduleEnabled"] = True
    device_shadow["Reported"]["Setpoint"]["SetpointStatus"] = "NoHold"
    await setup_integration(hass, mock_config_entry, mock_api)
    climate_id = eid(hass, "climate", f"{MAC}_climate")
    assert hass.states.get(climate_id).attributes["preset_mode"] == "none"

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "climate",
            "set_preset_mode",
            {"entity_id": climate_id, "preset_mode": "vacation"},
            blocking=True,
        )
    mock_api.async_set_hold.assert_not_awaited()

    # The settable holds go through (PUT hold).
    await hass.services.async_call(
        "climate",
        "set_preset_mode",
        {"entity_id": climate_id, "preset_mode": "permanent_hold"},
        blocking=True,
    )
    mock_api.async_set_hold.assert_awaited_once_with(MAC, "PermanentHold")


async def test_optimistic_fan_mode_held_until_confirmed(
    hass: HomeAssistant, climate_id: str, mock_api, device_shadow
) -> None:
    await hass.services.async_call(
        "climate",
        "set_fan_mode",
        {"entity_id": climate_id, "fan_mode": "on"},
        blocking=True,
    )
    mock_api.async_set_fan.assert_awaited_once_with(MAC, "On")
    assert hass.states.get(climate_id).attributes["fan_mode"] == "on"

    # The debounced resync still reads the OLD shadow -> the optimistic value must hold.
    await advance_time(hass, 4)
    assert hass.states.get(climate_id).attributes["fan_mode"] == "on"

    # The device applies; the reconcile refresh confirms and the UI stays converged.
    device_shadow["Reported"]["FanSwitch"] = {"Position": "On"}
    await advance_time(hass, 11)
    assert hass.states.get(climate_id).attributes["fan_mode"] == "on"


async def test_hvac_action_off_and_fan(
    hass: HomeAssistant, mock_config_entry, mock_api, device_shadow
) -> None:
    # System off, but the blower is running (fan set to On) -> report FAN, not OFF.
    device_shadow["Reported"]["SystemSwitch"] = "Off"
    device_shadow["Reported"]["OperationStatus"]["Mode"] = "EquipmentOff"
    device_shadow["Reported"]["OperationStatus"]["FanRequest"] = True
    await setup_integration(hass, mock_config_entry, mock_api)
    climate_id = eid(hass, "climate", f"{MAC}_climate")

    assert hass.states.get(climate_id).attributes[ATTR_HVAC_ACTION] == HVACAction.FAN


async def test_emergency_heat_reads_as_heat(
    hass: HomeAssistant, mock_config_entry, mock_api, device_shadow
) -> None:
    device_shadow["Reported"]["SystemSwitch"] = "EmergencyHeat"
    await setup_integration(hass, mock_config_entry, mock_api)
    climate_id = eid(hass, "climate", f"{MAC}_climate")

    assert hass.states.get(climate_id).state == HVACMode.HEAT
    # ... and the emergency-heat switch carries the distinction.
    switch_id = eid(hass, "switch", f"{MAC}_emergency_heat")
    assert hass.states.get(switch_id).state == "on"
