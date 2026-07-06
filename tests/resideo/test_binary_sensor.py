"""Binary sensors — most notably the connectivity sensor's offline behavior."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from .conftest import MAC, eid, setup_integration


async def test_binary_sensor_states(hass: HomeAssistant, init_integration) -> None:
    assert hass.states.get(eid(hass, "binary_sensor", f"{MAC}_online")).state == "on"
    assert hass.states.get(eid(hass, "binary_sensor", f"{MAC}_fault")).state == "off"
    assert hass.states.get(eid(hass, "binary_sensor", f"{MAC}_vacation_hold")).state == "off"
    # Remote room sensor motion/occupancy (sub-device).
    assert hass.states.get(eid(hass, "binary_sensor", f"{MAC}_room1_acc1_motion")).state == "off"


async def test_online_sensor_reports_off_when_device_offline(
    hass: HomeAssistant, mock_config_entry, mock_api, device_shadow
) -> None:
    """The connectivity sensor's whole job is to report the offline state — it must show
    'off', not become unavailable with everything else."""
    device_shadow["Reported"]["Online"] = False
    await setup_integration(hass, mock_config_entry, mock_api)

    assert hass.states.get(eid(hass, "binary_sensor", f"{MAC}_online")).state == "off"
    # Everything else on the device is rightly unavailable.
    assert hass.states.get(eid(hass, "climate", f"{MAC}_climate")).state == "unavailable"
    assert (
        hass.states.get(eid(hass, "sensor", f"{MAC}_indoor_temperature")).state
        == "unavailable"
    )
