"""Model-parsing tests against sanitized live captures.

These lock in the mapping the HA climate entity relies on (resideo-api-spec.md §7).
"""

from __future__ import annotations

import pytest

from custom_components.resideo.aioresideo import (
    ResideoClient,
    ResideoConfiguration,
    ResideoRooms,
    ResideoThermostat,
)
from custom_components.resideo.aioresideo.objects.account import ResideoAccountDevice


def test_thermostat_shadow(device_shadow: dict) -> None:
    t = ResideoThermostat(device_shadow)
    assert t.mac == "AABBCCDDEEFF"
    assert t.name == "Master Bedroom"
    assert t.online is True
    assert t.system_switch == "Cool"
    assert t.operation_mode == "Cool"  # current HVAC action
    assert t.indoor_temperature == 76.0
    assert t.outdoor_temperature == 82.0
    assert t.indoor_humidity == 56
    assert t.cool_setpoint == 76.0
    assert t.heat_setpoint == 62.0
    assert t.setpoint_status == "PermanentHold"
    assert t.fan_position == "Auto"
    # the captured device has a remote room sensor accessory
    accessory_types = {a.get("AccessoryType") for a in t.zone_accessories}
    assert "IndoorAirSensor" in accessory_types
    # newly surfaced shadow fields (full read-surface coverage)
    assert t.model == "DENALI_TSTAT-001"
    assert t.signal_strength == -59
    assert t.heat_cool_mode == "Cool"
    assert t.fan_request_reasons == ["HeatingOrCoolingStagesOn"]
    assert t.demand_response_state == "NotStarted"
    assert t.schedule_day == "Thursday"
    assert t.schedule_type == "Timed"
    assert t.firmware_status == "Success"
    assert t.current_language == "English"
    assert t.active_adaptive_recovery_mode == "AdaptiveIntelligentRecovery"
    assert t.feels_like_enabled is True
    assert t.ventilation_timer == 0
    assert t.serial_number == "000000000000"
    assert t.registration_date == "2026-06-12T01:23:24.129+00:00"
    assert t.firmware_last_updated == "2026-06-12T01:29:18.000+00:00"


def test_self_accessory_matched_by_type() -> None:
    """model / serial / signal_strength must resolve the Thermostat entry by AccessoryType,
    not by list position — ZoneAccessories also lists remote sensors (here, first)."""
    shadow = {
        "Reported": {
            "ZoneAccessories": {
                "ZoneAccessories": [
                    {  # a remote air sensor listed FIRST
                        "AccessoryType": "IndoorAirSensor",
                        "HardwareRevision": "4354",
                        "SerialNumber": "955140001",
                        "RssiAverage": -60,
                    },
                    {  # the thermostat itself
                        "AccessoryType": "Thermostat",
                        "HardwareRevision": "DENALI_TSTAT-001",
                        "SerialNumber": "01160123456789",
                        "RssiAverage": -59,
                    },
                ]
            }
        }
    }
    t = ResideoThermostat(shadow)
    assert t.model == "DENALI_TSTAT-001"  # NOT the air sensor's "4354"
    assert t.serial_number == "01160123456789"
    assert t.signal_strength == -59


def test_configuration(configuration: dict) -> None:
    c = ResideoConfiguration(configuration)
    assert "Cool" in c.system_switch_capabilities
    assert "Auto" in c.fan_positions
    assert c.min_cool_setpoint == 50.0
    assert c.max_cool_setpoint == 90.0
    assert c.temperature_units == "F"
    assert "Rooms" in c.supported_capabilities
    # equipment + flags (drive the diagnostic sensors)
    assert c.heating_system == "CompressorHeat"
    assert c.heating_stages == 3
    assert c.cooling_system == "CompressorCool"
    assert c.cooling_stages == 2
    assert c.freeze_protection_active is False
    assert c.freeze_protection_configured is False
    assert c.commercial_configuration is False
    assert c.away_mode_override is False
    assert c.matter_commissioning_status == "Commissioned"
    assert "TimedNorthAmerica" in c.available_schedule_types


def test_iter_devices_finds_thermostat(accounts: dict) -> None:
    devices = ResideoClient.iter_devices(accounts)
    assert len(devices) == 2
    models = [ResideoAccountDevice(d) for d in devices]
    thermostats = [m for m in models if m.is_thermostat]
    assert len(thermostats) == 1
    t = thermostats[0]
    assert t.mac == "AABBCCDDEEFF"
    assert t.name == "Master Bedroom"
    # Detection must come from the base64 global-id Type: globalDeviceType is only a model
    # name ("Denali_S1200") that contains neither "thermostat" nor "lyric". This guards the
    # real-data bug where is_thermostat compared the *encoded* id and found 0 thermostats.
    assert t.global_device_type == "Denali_S1200"
    assert t.device_kind == "LyricThermostatDevice"
    # the smoke detector must be excluded, and its kind still decodes
    others = [m for m in models if not m.is_thermostat]
    assert len(others) == 1
    assert others[0].device_kind == "SmokeDetectorDevice"


def test_write_contract_shape(cool_setpoint_write: dict) -> None:
    """Documents the proven devsrv write contract (spec §4)."""
    assert cool_setpoint_write["status"] == 202
    body = cool_setpoint_write["request_body"]
    assert body["ChannelId"] == "ds-notification-service"
    assert "SetpointValue" in body
    assert "SetpointStatus" in body
    assert "TransactionId" in cool_setpoint_write["response_body"]


def test_rooms_and_accessories(rooms: dict) -> None:
    r = ResideoRooms(rooms)
    assert r.device_id == "AABBCCDDEEFF"
    assert len(r.rooms) == 2
    # the built-in thermostat accessory carries the device's CO2/TVOC measurements
    ta = r.thermostat_accessory
    assert ta is not None and ta.is_thermostat
    assert ta.co2 == 425.0
    assert ta.tvoc == 5.0
    # exactly one remote IndoorAirSensor ("Guest Room") with live values
    air = r.air_sensor_accessories()
    assert len(air) == 1
    room, acc = air[0]
    assert room.name == "Guest Room"
    assert acc.is_air_sensor
    assert acc.indoor_temperature == 74.0
    assert acc.indoor_humidity == 58
    assert acc.battery_status == "Ok"
    assert acc.rssi == -60
    assert acc.model == "4354"
    assert acc.co2 is None  # the remote sensor doesn't report CO2
    # newly surfaced accessory fields
    assert acc.occupancy_sensitivity == "Medium"
    assert acc.occupancy_timeout == 0
    assert acc.exclude_motion is False
    assert acc.software_revision == "2.1.5.0"
    assert acc.serial_number == "000000000000"


@pytest.mark.skip(reason="TODO: client transport tests need aioresponses mocking (see plan)")
def test_client_request_roundtrip_TODO() -> None:
    """Placeholder: mock api.resideo.com and assert headers (bearer + Ocp-Apim key),
    202 handling, and the retry-once-on-401 path in ResideoClient._request."""
