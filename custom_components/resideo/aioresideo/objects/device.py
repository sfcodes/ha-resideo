"""Thermostat device-shadow model (from GET /devsrv/api/v2/device/{mac}).

Wraps ``{DeviceId, Reported{...}, Desired{...}}``. The ``Reported`` block is the live device
state; see ``resideo-api-spec.md`` §5. Fixture: ``tests/fixtures/device.json``.
"""

from __future__ import annotations

from typing import Any

from .base import ResideoBaseObject


class ResideoThermostat(ResideoBaseObject):
    """The thermostat device shadow + convenience accessors."""

    @property
    def device_id(self) -> str | None:
        return self.attributes.get("DeviceId")

    mac = device_id

    @property
    def reported(self) -> dict[str, Any]:
        return self.attributes.get("Reported", {}) or {}

    @property
    def desired(self) -> dict[str, Any]:
        return self.attributes.get("Desired", {}) or {}

    def _r(self, *path: str, default: Any = None) -> Any:
        """Walk a nested key path inside ``Reported``."""
        cur: Any = self.reported
        for key in path:
            if not isinstance(cur, dict):
                return default
            cur = cur.get(key)
            if cur is None:
                return default
        return cur

    # -- identity / status ----------------------------------------------------
    @property
    def name(self) -> str | None:
        return self.reported.get("DeviceName")

    @property
    def online(self) -> bool:
        return bool(self.reported.get("Online", False))

    @property
    def current_language(self) -> str | None:
        return self.reported.get("CurrentLanguage")

    @property
    def registration_date(self) -> str | None:
        return self.reported.get("RegistrationDate")

    # -- mode / action --------------------------------------------------------
    @property
    def system_switch(self) -> str | None:
        """Configured mode: Heat/Cool/Off/Auto/EmergencyHeat."""
        return self.reported.get("SystemSwitch")

    @property
    def heat_cool_mode(self) -> str | None:
        return self.reported.get("HeatCoolMode")

    @property
    def operation_mode(self) -> str | None:
        """Current HVAC action: EquipmentOff/Heat/Cool/... (OperationStatus.Mode)."""
        return self._r("OperationStatus", "Mode")

    @property
    def fan_request(self) -> bool | None:
        return self._r("OperationStatus", "FanRequest")

    @property
    def circulation_fan_request(self) -> bool | None:
        return self._r("OperationStatus", "CirculationFanRequest")

    @property
    def demand(self) -> int | None:
        """Heat/cool demand percentage (HeatAndCoolDemand.Demand)."""
        return self._r("HeatAndCoolDemand", "Demand")

    @property
    def current_stage(self) -> int | None:
        return self._r("HeatAndCoolDemand", "CurrentStage")

    # -- temperature / humidity ----------------------------------------------
    @property
    def indoor_temperature(self) -> float | None:
        return self.reported.get("DisplayedIndoorTemperature")

    @property
    def outdoor_temperature(self) -> float | None:
        return self.reported.get("DisplayedOutdoorTemperature")

    @property
    def indoor_humidity(self) -> int | None:
        return self.reported.get("DisplayedIndoorHumidity")

    @property
    def outdoor_humidity(self) -> int | None:
        return self.reported.get("DisplayedOutdoorHumidity")

    # -- setpoints / fan / hold ----------------------------------------------
    @property
    def setpoint(self) -> dict[str, Any]:
        return self.reported.get("Setpoint", {}) or {}

    @property
    def setpoint_status(self) -> str | None:
        """Hold state: NoHold/TemporaryHold/PermanentHold/HoldUntil/VacationHold."""
        return self.setpoint.get("SetpointStatus")

    @property
    def heat_setpoint(self) -> float | None:
        return self.setpoint.get("HeatSetpoint")

    @property
    def cool_setpoint(self) -> float | None:
        return self.setpoint.get("CoolSetpoint")

    @property
    def fan_position(self) -> str | None:
        fan = self.reported.get("FanSwitch") or self.setpoint.get("FanSwitch") or {}
        return fan.get("Position")

    # -- schedule -------------------------------------------------------------
    @property
    def schedule_enabled(self) -> bool:
        return bool(self.reported.get("ScheduleEnabled", False))

    @property
    def schedule_period(self) -> str | None:
        return self._r("CurrentSchedulePeriod", "Period")

    @property
    def schedule_day(self) -> str | None:
        return self._r("CurrentSchedulePeriod", "Day")

    @property
    def schedule_type(self) -> str | None:
        return self._r("ScheduleType", "ScheduleType")

    @property
    def current_priority_type(self) -> str | None:
        return self.reported.get("CurrentPriorityType")

    # -- holds / recovery / ventilation --------------------------------------
    @property
    def vacation_hold(self) -> bool:
        return bool(self._r("VacationHold", "Enabled", default=False))

    @property
    def adaptive_recovery_active(self) -> bool:
        return bool(self.reported.get("IsAdaptiveRecoveryActive", False))

    @property
    def active_adaptive_recovery_mode(self) -> str | None:
        return self.reported.get("ActiveAdaptiveRecoveryMode")

    @property
    def adaptive_recovery_enabled(self) -> bool:
        """Whether adaptive recovery is enabled (a mode is selected) — distinct from the
        transient ``adaptive_recovery_active`` (currently pre-conditioning)."""
        mode = self.active_adaptive_recovery_mode
        return bool(mode) and str(mode).lower() not in ("none", "off")

    @property
    def ventilation_timer(self) -> int | None:
        return self.reported.get("VentilationTimer")

    @property
    def ventilation_boost_timer(self) -> int | None:
        return self.reported.get("VentilationBoostTimer")

    @property
    def feels_like_enabled(self) -> bool:
        return bool(self.reported.get("FeelsLikeEnabled", False))

    @property
    def backlight_state(self) -> str | None:
        return self.reported.get("BackLightState")

    # -- indoor air quality (qualitative; numeric CO2/TVOC come from /group/0/rooms) --
    @property
    def air_quality(self) -> str | None:
        return self._r("IndoorAirQuality", "TotalIndoorAirQuality", "Quality")

    @property
    def co2_quality(self) -> str | None:
        return self._r("IndoorAirQuality", "EstimatedCarbonDioxide", "Quality")

    @property
    def voc_quality(self) -> str | None:
        return self._r("IndoorAirQuality", "TotalVolatileOrganicCompounds", "Quality")

    @property
    def humidity_quality(self) -> str | None:
        return self._r("IndoorAirQuality", "HumidityQuality")

    # -- maintenance / firmware / faults -------------------------------------
    @property
    def air_filter_remaining_days(self) -> int | None:
        return self._r("AirFilterReminders", "1", "RemainingDays")

    @property
    def air_filter_alert(self) -> bool:
        return bool(self._r("AirFilterReminders", "1", "IsAlertRaised", default=False))

    @property
    def firmware_version(self) -> str | None:
        return self._r("FirmwareSystem", "SwPackage") or self._r("FirmwareUpdate", "SwPackage")

    @property
    def firmware_status(self) -> str | None:
        return self._r("FirmwareUpdate", "Status")

    @property
    def firmware_last_updated(self) -> str | None:
        """ISO timestamp of the last firmware update (FirmwareSystem.LastUpdated)."""
        return self._r("FirmwareSystem", "LastUpdated")

    @property
    def thermostat_faults(self) -> list[Any]:
        return self.reported.get("ThermostatFaults", []) or []

    @property
    def fault_count(self) -> int:
        return len(self.thermostat_faults)

    @property
    def has_fault(self) -> bool:
        return self.fault_count > 0

    # -- fan reasons / staging ------------------------------------------------
    @property
    def fan_request_reasons(self) -> list[str]:
        """Why the fan is being requested, e.g. ['HeatingOrCoolingStagesOn']."""
        return self.reported.get("FanReqReasons", []) or []

    @property
    def stages_on(self) -> list[bool]:
        """Per-stage on/off flags (HeatAndCoolDemand.StagesOn)."""
        return self._r("HeatAndCoolDemand", "StagesOn", default=[]) or []

    @property
    def active_demand_mode(self) -> str | None:
        """HeatAndCoolDemand.Mode (the mode the equipment is actively demanding)."""
        return self._r("HeatAndCoolDemand", "Mode")

    # -- demand response (utility events) -------------------------------------
    @property
    def demand_response_state(self) -> str | None:
        """Utility demand-response event state, e.g. 'NotStarted' / 'Active'."""
        return self._r("DemandResponseState", "CurrentDrEventState")

    @property
    def demand_response_opted_out(self) -> bool:
        return bool(self._r("DemandResponseState", "OptedOut", default=False))

    # -- inbuilt sensor / room ------------------------------------------------
    @property
    def inbuilt_sensor_room(self) -> str | None:
        return self._r("InbuiltSensorState", "RoomName")

    # -- accessories (remote room sensors metadata) --------------------------
    @property
    def zone_accessories(self) -> list[dict[str, Any]]:
        return (self.reported.get("ZoneAccessories") or {}).get("ZoneAccessories") or []

    @property
    def _self_accessory(self) -> dict[str, Any]:
        """The thermostat's own entry in ZoneAccessories (its model/firmware/RSSI).

        Matched by ``AccessoryType`` — ``ZoneAccessories`` also lists remote sensors, so
        indexing by position would risk reading a remote sensor's model/serial/RSSI.
        """
        accs = self.zone_accessories
        for acc in accs:
            if acc.get("AccessoryType") == "Thermostat":
                return acc
        return accs[0] if accs else {}

    @property
    def model(self) -> str | None:
        """Hardware model, e.g. 'DENALI_TSTAT-001'."""
        return self._self_accessory.get("HardwareRevision") or self._self_accessory.get("Model")

    @property
    def signal_strength(self) -> int | None:
        """The thermostat's own Wi-Fi RSSI (dBm)."""
        return self._self_accessory.get("RssiAverage")

    @property
    def serial_number(self) -> str | None:
        return self._self_accessory.get("SerialNumber")
