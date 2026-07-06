"""Number entities: setpoint limits (full-body compose + ordering validation), freeze floor."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.resideo.const import DOMAIN

from .conftest import MAC, eid, setup_integration


async def test_setpoint_limit_entities_exist_freeze_absent(
    hass: HomeAssistant, init_integration
) -> None:
    registry = er.async_get(hass)
    for key in ("heat_setpoint_min", "heat_setpoint_max", "cool_setpoint_min", "cool_setpoint_max"):
        assert registry.async_get_entity_id("number", DOMAIN, f"{MAC}_{key}")
    # FreezeProtection.Configured is false in the fixture -> no freeze-floor entity.
    assert not registry.async_get_entity_id("number", DOMAIN, f"{MAC}_freeze_protection_low_limit")


async def test_setpoint_limit_write_sends_all_four_fields(
    hass: HomeAssistant, init_integration, mock_api
) -> None:
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": eid(hass, "number", f"{MAC}_cool_setpoint_min"), "value": 60},
        blocking=True,
    )
    # A partial setPointCapabilities body is silently ignored by the device; the
    # coordinator must compose the full four-field body from config + overrides.
    mock_api.async_set_setpoint_capabilities.assert_awaited_once_with(
        MAC, heat_min=50.0, heat_max=90.0, cool_min=60.0, cool_max=90.0
    )


async def test_heat_cool_ordering_rejected_up_front(
    hass: HomeAssistant, init_integration, mock_api
) -> None:
    """The device silently drops a write violating maxHeat <= maxCool; we reject it."""
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "number",
            "set_value",
            # cool_max 85 would fall below heat_max (90) -> heat band above cool band.
            {"entity_id": eid(hass, "number", f"{MAC}_cool_setpoint_max"), "value": 85},
            blocking=True,
        )
    mock_api.async_set_setpoint_capabilities.assert_not_awaited()


async def test_validation_honors_pending_override_and_composes(
    hass: HomeAssistant, init_integration, mock_api
) -> None:
    """A just-written counterpart limit is honored before its read-back confirms.

    cool_max 85 is invalid against the stale config (heat_max 90) but valid against the
    pending heat_max of 70 — the override-aware bound must allow it, and the second write
    must carry the pending heat_max instead of clobbering it back to 90.
    """
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": eid(hass, "number", f"{MAC}_heat_setpoint_max"), "value": 70},
        blocking=True,
    )
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": eid(hass, "number", f"{MAC}_cool_setpoint_max"), "value": 85},
        blocking=True,
    )
    second_call = mock_api.async_set_setpoint_capabilities.await_args_list[1]
    assert second_call.kwargs == {
        "heat_min": 50.0,
        "heat_max": 70.0,
        "cool_min": 50.0,
        "cool_max": 85.0,
    }


async def test_numbers_hidden_on_celsius_device(
    hass: HomeAssistant, mock_config_entry, mock_api, configuration_data
) -> None:
    """°F-integer write semantics are unverified on Celsius devices — entities are hidden."""
    configuration_data["Reported"]["TemperatureUnits"] = "C"
    configuration_data["Reported"]["FreezeProtection"]["Configured"] = True
    await setup_integration(hass, mock_config_entry, mock_api)

    registry = er.async_get(hass)
    assert not registry.async_get_entity_id("number", DOMAIN, f"{MAC}_heat_setpoint_min")
    assert not registry.async_get_entity_id(
        "number", DOMAIN, f"{MAC}_freeze_protection_low_limit"
    )


async def test_freeze_protection_write(
    hass: HomeAssistant, mock_config_entry, mock_api, configuration_data
) -> None:
    configuration_data["Reported"]["FreezeProtection"] = {
        "Configured": True,
        "Active": False,
        "LowLimitDegrees": 40,
    }
    await setup_integration(hass, mock_config_entry, mock_api)
    number_id = eid(hass, "number", f"{MAC}_freeze_protection_low_limit")
    assert float(hass.states.get(number_id).state) == 40

    await hass.services.async_call(
        "number", "set_value", {"entity_id": number_id, "value": 42}, blocking=True
    )
    mock_api.async_set_freeze_protection.assert_awaited_once_with(MAC, 42.0)
