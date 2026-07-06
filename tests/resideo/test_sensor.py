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


async def test_celsius_device_values_convert_correctly(
    hass: HomeAssistant, mock_config_entry, mock_api, configuration_data
) -> None:
    """A °C-configured device reports °C values; treating them as °F mis-converts.

    With HA displaying US-customary units, a native 76 °C must convert to 168.8 °F —
    before the fix the sensor declared °F and would show 76.0.
    """
    configuration_data["Reported"]["TemperatureUnits"] = "C"
    await setup_integration(hass, mock_config_entry, mock_api)

    state = hass.states.get(eid(hass, "sensor", f"{MAC}_indoor_temperature"))
    assert state.attributes["unit_of_measurement"] == "°F"  # display unit (US system)
    assert float(state.state) == 168.8  # converted FROM native °C

    # The remote accessory temperature follows the same device unit.
    remote = hass.states.get(eid(hass, "sensor", f"{MAC}_room1_acc1_temperature"))
    assert float(remote.state) == 165.2  # 74 °C -> °F


async def test_diagnostic_sensors(hass: HomeAssistant, init_integration) -> None:
    assert hass.states.get(eid(hass, "sensor", f"{MAC}_firmware_version")).state == "01.3605.740"
    assert hass.states.get(eid(hass, "sensor", f"{MAC}_equipment_status")).state == "Cool"
    assert hass.states.get(eid(hass, "sensor", f"{MAC}_setpoint_status")).state == "PermanentHold"
    assert hass.states.get(eid(hass, "sensor", f"{MAC}_fault_count")).state == "0"
