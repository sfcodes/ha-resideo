"""Thermostat capabilities model (from GET /devsrv/api/v2/device/{mac}/configuration).

See ``resideo-api-spec.md`` §6. Fixture: ``tests/fixtures/003_configuration.json``.
"""

from __future__ import annotations

from typing import Any

from .base import ResideoBaseObject


class ResideoConfiguration(ResideoBaseObject):
    """Device capabilities and allowed value sets (drives HA min/max, hvac_modes, fan_modes)."""

    @property
    def reported(self) -> dict[str, Any]:
        return self.attributes.get("Reported", {}) or {}

    @property
    def system_switch_capabilities(self) -> list[str]:
        """Allowed modes, e.g. [EmergencyHeat, Heat, Off, Cool, Auto]."""
        return self.reported.get("SystemSwitchCapabilities", []) or []

    @property
    def fan_positions(self) -> list[str]:
        """Allowed fan positions, e.g. [On, Auto, Circulate]."""
        return (self.reported.get("FanSwitchCapabilities") or {}).get("Positions") or []

    @property
    def min_cool_setpoint(self) -> float | None:
        return self.reported.get("MinimumCoolSetpointAllowed")

    @property
    def max_cool_setpoint(self) -> float | None:
        return self.reported.get("MaximumCoolSetpointAllowed")

    @property
    def min_heat_setpoint(self) -> float | None:
        return self.reported.get("MinimumHeatSetpointAllowed")

    @property
    def max_heat_setpoint(self) -> float | None:
        return self.reported.get("MaximumHeatSetpointAllowed")

    @property
    def setpoint_deadband(self) -> float | None:
        return self.reported.get("SetpointDeadband")

    @property
    def temperature_units(self) -> str | None:
        """"F" or "C"."""
        return self.reported.get("TemperatureUnits")

    @property
    def supported_capabilities(self) -> list[str]:
        return self.reported.get("SupportedCapabilities", []) or []

    # -- equipment (SystemConfiguration) --------------------------------------
    @property
    def _system_config(self) -> dict[str, Any]:
        return self.reported.get("SystemConfiguration", {}) or {}

    @property
    def heating_system(self) -> str | None:
        """e.g. 'CompressorHeat', 'Conventional'."""
        return self._system_config.get("HeatingSystem")

    @property
    def heating_stages(self) -> int | None:
        return self._system_config.get("HeatingStages")

    @property
    def cooling_system(self) -> str | None:
        """e.g. 'CompressorCool'."""
        return self._system_config.get("CoolingSystem")

    @property
    def cooling_stages(self) -> int | None:
        return self._system_config.get("CoolingStages")

    # -- freeze protection ----------------------------------------------------
    @property
    def _freeze(self) -> dict[str, Any]:
        return self.reported.get("FreezeProtection", {}) or {}

    @property
    def freeze_protection_configured(self) -> bool:
        return bool(self._freeze.get("Configured", False))

    @property
    def freeze_protection_active(self) -> bool:
        return bool(self._freeze.get("Active", False))

    @property
    def freeze_protection_low_limit(self) -> int | None:
        return self._freeze.get("LowLimitDegrees")

    # -- misc flags / display -------------------------------------------------
    @property
    def commercial_configuration(self) -> bool:
        return bool(self.reported.get("CommercialConfiguration", False))

    @property
    def away_mode_override(self) -> bool:
        return bool(self.reported.get("AwayModeOverride", False))

    @property
    def backlight_intensity(self) -> int | None:
        return self.reported.get("BackLightIntensity")

    @property
    def max_backlight_intensity(self) -> int | None:
        return self.reported.get("MaximumBackLightIntensity")

    @property
    def min_backlight_intensity(self) -> int | None:
        return self.reported.get("MinimumBackLightIntensity")

    @property
    def matter_commissioning_status(self) -> str | None:
        return (self.reported.get("Matter") or {}).get("CommissioningStatus")

    @property
    def allowed_adaptive_recovery_modes(self) -> list[str]:
        return self.reported.get("AllowedAdaptiveRecoveryModes", []) or []

    @property
    def available_schedule_types(self) -> list[str]:
        return (self.reported.get("ScheduleCapabilities") or {}).get("AvailableScheduleTypes") or []
