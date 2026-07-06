"""Switch entities: device settings, emergency heat, and the accessory full-body compose."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from .conftest import MAC, eid

# The Guest Room remote air sensor in rooms.json.
ACCESSORY_SWITCH_UID = f"{MAC}_room1_acc1_exclude_motion"
SENSITIVITY_UID = f"{MAC}_room1_acc1_occupancy_sensitivity"


async def test_device_switch_states(hass: HomeAssistant, init_integration) -> None:
    # FeelsLikeEnabled=true / ScheduleEnabled=false / ActiveAdaptiveRecoveryMode set (fixture).
    assert hass.states.get(eid(hass, "switch", f"{MAC}_feels_like")).state == "on"
    assert hass.states.get(eid(hass, "switch", f"{MAC}_schedule")).state == "off"
    assert hass.states.get(eid(hass, "switch", f"{MAC}_adaptive_recovery")).state == "on"
    assert hass.states.get(eid(hass, "switch", f"{MAC}_emergency_heat")).state == "off"


async def test_feels_like_toggle_is_optimistic(
    hass: HomeAssistant, init_integration, mock_api
) -> None:
    switch_id = eid(hass, "switch", f"{MAC}_feels_like")
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": switch_id}, blocking=True
    )
    mock_api.async_set_feels_like.assert_awaited_once_with(MAC, False)
    assert hass.states.get(switch_id).state == "off"  # optimistic (shadow still true)


async def test_emergency_heat_maps_to_system_switch(
    hass: HomeAssistant, init_integration, mock_api
) -> None:
    switch_id = eid(hass, "switch", f"{MAC}_emergency_heat")
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": switch_id}, blocking=True
    )
    mock_api.async_set_system_switch.assert_awaited_with(MAC, "EmergencyHeat")
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": switch_id}, blocking=True
    )
    # Off lands on Heat (a heat-family mode), not the prior mode.
    mock_api.async_set_system_switch.assert_awaited_with(MAC, "Heat")


async def test_accessory_exclude_sends_full_body(
    hass: HomeAssistant, init_integration, mock_api
) -> None:
    switch_id = eid(hass, "switch", ACCESSORY_SWITCH_UID)
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": switch_id}, blocking=True
    )
    # The full accessoryValue body: the changed flag plus the echoed current values.
    mock_api.async_set_accessory_value.assert_awaited_once_with(
        MAC, 1, sensitivity="Medium", exclude_motion=True, exclude_temp=False
    )
    assert hass.states.get(switch_id).state == "on"


async def test_pending_writes_compose_not_clobber(
    hass: HomeAssistant, init_integration, mock_api
) -> None:
    """Two writes inside the ~2 s read-back window must compose via the shared overrides.

    Without them, the sensitivity write would echo exclude_motion=False from the stale
    shadow and silently revert the first write.
    """
    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": eid(hass, "switch", ACCESSORY_SWITCH_UID)},
        blocking=True,
    )
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": eid(hass, "select", SENSITIVITY_UID), "option": "High"},
        blocking=True,
    )
    assert mock_api.async_set_accessory_value.await_count == 2
    second_call = mock_api.async_set_accessory_value.await_args_list[1]
    assert second_call.kwargs == {
        "sensitivity": "High",
        "exclude_motion": True,  # the not-yet-confirmed first write is carried, not clobbered
        "exclude_temp": False,
    }


async def test_sensitivity_select_holds_until_reported(
    hass: HomeAssistant, init_integration, mock_api, rooms_data
) -> None:
    """Sensitivity is eventually-consistent: no reconcile snap-back, cleared on report."""
    select_id = eid(hass, "select", SENSITIVITY_UID)
    assert hass.states.get(select_id).state == "Medium"

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": select_id, "option": "High"},
        blocking=True,
    )
    assert hass.states.get(select_id).state == "High"  # optimistic

    # A refresh that still reports Medium must NOT snap the UI back.
    coordinator = init_integration.runtime_data
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(select_id).state == "High"

    # Once the sensor checks in and the cloud reports High, the override is released.
    rooms_data["Rooms"][1]["Accessories"][0]["AccessoryValue"]["OccupancySensitivity"] = "High"
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(select_id).state == "High"
    assert coordinator.accessory_override(MAC, 1) == {}  # shared override cleared
