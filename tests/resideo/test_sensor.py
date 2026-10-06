"""Sensor entities: values, and the temperature-unit handling for F and C devices."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from .conftest import MAC, eid, setup_integration


async def test_sensor_values(hass: HomeAssistant, init_integration) -> None:
    assert hass.states.get(eid(hass, "sensor", f"{MAC}_indoor_temperature")).state == "76.0"
    assert hass.states.get(eid(hass, "sensor", f"{MAC}_outdoor_temperature")).state == "82.0"
    assert hass.states.get(eid(hass, "sensor", f"{MAC}_indoor_humidity")).state == "56"
    # CO2 / TVOC come off the built-in thermostat accessory in /group/0/rooms.
    assert hass.states.get(eid(hass, "sensor", f"{MAC}_co2")).state == "425.0"
    assert hass.states.get(eid(hass, "sensor", f"{MAC}_tvoc")).state == "5.0"
    # Remote room sensor (sub-device).
    assert hass.states.get(eid(hass, "sensor", f"{MAC}_room1_acc1_temperature")).state == "74.0"
    assert (
        hass.states.get(eid(hass, "sensor", f"{MAC}_room1_acc1_battery_status")).state == "Ok"
    )


async def test_fahrenheit_device_units(hass: HomeAssistant, init_integration) -> None:
    state = hass.states.get(eid(hass, "sensor", f"{MAC}_indoor_temperature"))
    # US-customary HA + °F native -> shown as-is.
    assert state.attributes["unit_of_measurement"] == "°F"
    assert state.state == "76.0"
    climate = hass.states.get(eid(hass, "climate", f"{MAC}_climate"))
    assert climate.attributes["current_temperature"] == 76.0


async def test_celsius_faceplate_still_reports_fahrenheit(
    hass: HomeAssistant, mock_config_entry, mock_api, configuration_data
) -> None:
    """API payloads are °F even when ``TemperatureUnits`` says "C" (issue #2).

    ``TemperatureUnits`` is display-unit metadata only (verified live — see
    ``ResideoConfiguration.temperature_units``); declaring °C native here double-converts:
    a 26.7 °C room reported as 80 would show 176 °F.
    """
    configuration_data["Reported"]["TemperatureUnits"] = "C"
    await setup_integration(hass, mock_config_entry, mock_api)

    state = hass.states.get(eid(hass, "sensor", f"{MAC}_indoor_temperature"))
    assert state.attributes["unit_of_measurement"] == "°F"
    assert float(state.state) == 76.0  # native °F, shown as-is (US system)

    remote = hass.states.get(eid(hass, "sensor", f"{MAC}_room1_acc1_temperature"))
    assert float(remote.state) == 74.0

    climate = hass.states.get(eid(hass, "climate", f"{MAC}_climate"))
    assert climate.attributes["current_temperature"] == 76.0


async def test_outdoor_temperature_follows_the_account_country(
    hass: HomeAssistant, mock_config_entry, mock_api, accounts_data, device_shadow
) -> None:
    """Outdoor temperature arrives in the account country's unit, unlike everything else (#7).

    A Canadian account reports 15 °C outdoors as ``15.0``. Declaring it °F showed -9.4 °C.
    """
    accounts_data["data"]["countryCode"] = "CA"
    device_shadow["Reported"]["DisplayedOutdoorTemperature"] = 15.0
    await setup_integration(hass, mock_config_entry, mock_api)

    outdoor = hass.states.get(eid(hass, "sensor", f"{MAC}_outdoor_temperature"))
    assert outdoor.attributes["unit_of_measurement"] == "°F"  # US-customary HA display
    assert float(outdoor.state) == 59.0  # 15 °C, converted once
    # Indoor is still °F on the wire, whatever the country.
    indoor = hass.states.get(eid(hass, "sensor", f"{MAC}_indoor_temperature"))
    assert float(indoor.state) == 76.0


async def test_outdoor_temperature_is_fahrenheit_for_us_accounts(
    hass: HomeAssistant, init_integration
) -> None:
    outdoor = hass.states.get(eid(hass, "sensor", f"{MAC}_outdoor_temperature"))
    assert float(outdoor.state) == 82.0  # native °F, shown as-is


async def test_diagnostic_sensors(hass: HomeAssistant, init_integration) -> None:
    assert hass.states.get(eid(hass, "sensor", f"{MAC}_firmware_version")).state == "01.3605.740"
    assert hass.states.get(eid(hass, "sensor", f"{MAC}_equipment_status")).state == "Cool"
    assert hass.states.get(eid(hass, "sensor", f"{MAC}_setpoint_status")).state == "PermanentHold"
    assert hass.states.get(eid(hass, "sensor", f"{MAC}_fault_count")).state == "0"
