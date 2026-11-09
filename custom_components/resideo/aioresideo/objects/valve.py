"""Water shutoff valve model (L5 / L7), wrapping ``GET .../shutOffValves/{mac}/info``.

The body is flat (no ``Reported``/``Desired`` wrapper). Live state arrives separately over
SignalR as ``ActuatorValve`` and ``WaterFlow`` notifications, merged in by
:func:`~aioresideo.merge.apply_valve_event`. Units are fixed by the device, whatever the app is
set to display: volume in **litres**, flow in **L/min**, temperature in **°F**, pressure in
**psi**.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..const import (
    VALVE_STATE_CLOSED,
    VALVE_STATE_CLOSING,
    VALVE_STATE_OPEN,
    VALVE_STATE_OPENING,
)
from .base import ResideoBaseObject

# ``/info`` reports ``Open``; events report ``open`` / ``close`` / ``opening`` / ``closing``.
_STATE_MAP = {
    "open": VALVE_STATE_OPEN,
    "opened": VALVE_STATE_OPEN,
    "close": VALVE_STATE_CLOSED,
    "closed": VALVE_STATE_CLOSED,
    "opening": VALVE_STATE_OPENING,
    "closing": VALVE_STATE_CLOSING,
}


def normalize_valve_state(raw: Any) -> str | None:
    """Map any spelling of the valve position to one of the ``VALVE_STATE_*`` constants."""
    if not isinstance(raw, str):
        return None
    return _STATE_MAP.get(raw.strip().lower())


class ResideoShutoffValve(ResideoBaseObject):
    """A shutoff valve's full state (``/info``) with typed accessors."""

    # -- identity -------------------------------------------------------------
    @property
    def name(self) -> str | None:
        return self._get("DeviceName")

    @property
    def model(self) -> str | None:
        return self._get("DevModel") or self._get("OsNumber")

    @property
    def firmware_version(self) -> str | None:
        return self._get("SoftwareRevision")

    @property
    def hardware_version(self) -> str | None:
        return self._get("HardwareRevision")

    @property
    def serial_number(self) -> str | None:
        return self._get("DeviceSerialNumber")

    # -- valve ----------------------------------------------------------------
    @property
    def valve_state(self) -> str | None:
        """``open`` / ``closed`` / ``opening`` / ``closing`` (``None`` if unknown)."""
        return normalize_valve_state(self._get("ActuatorValve", "ValveStatus"))

    @property
    def command_source(self) -> str | None:
        """Who last moved the valve (``app``, ``geofence``, ...)."""
        return self._get("ActuatorValve", "CommandSource")

    @property
    def leak_status(self) -> str | None:
        """The valve's leak status string (``Ok`` when dry)."""
        return self._get("ActuatorValve", "LeakStatus")

    @property
    def device_temperature(self) -> float | None:
        """The actuator's own temperature (°F), distinct from the water temperature."""
        return self._as_float(self._get("ActuatorValve", "DeviceTemperature"))

    # -- connectivity / power -------------------------------------------------
    @property
    def online(self) -> bool:
        return bool(self._get("Online", default=False))

    @property
    def last_check_in(self) -> datetime | None:
        raw = self._get("LastCheckInTime")
        if not isinstance(raw, str):
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    @property
    def battery(self) -> float | None:
        """Backup battery charge, percent."""
        return self._as_float(self._get("BatteryCapacity"))

    @property
    def on_battery_power(self) -> bool | None:
        value = self._get("IsOnBatteryPower")
        return None if value is None else bool(value)

    # -- live water readings --------------------------------------------------
    @property
    def flow_rate(self) -> float | None:
        """Current flow, litres per minute."""
        return self._as_float(self._get("FlowRate"))

    @property
    def water_total(self) -> float | None:
        """Lifetime volume through the valve, litres."""
        return self._as_float(self._get("WaterTotal"))

    @property
    def temperature(self) -> float | None:
        """Water temperature, °F."""
        return self._as_float(self._get("Temperature"))

    @property
    def pressure(self) -> float | None:
        """Water pressure, psi."""
        return self._as_float(self._get("Pressure"))

    @property
    def consumed_this_period(self) -> float | None:
        """Volume used so far in the current billing period, litres."""
        return self._as_float(self._get("TargetUse", "ConsumedThisPeriod"))

    # -- faults / tests -------------------------------------------------------
    @property
    def active_faults(self) -> list[dict[str, Any]]:
        faults = self._get("Faults", default=[])
        if not isinstance(faults, list):
            return []
        return [
            f for f in faults if isinstance(f, dict) and str(f.get("Status")).lower() == "active"
        ]

    @property
    def has_fault(self) -> bool:
        return bool(self.active_faults)

    @property
    def micro_leak_test_in_progress(self) -> bool | None:
        value = self._get("IsMicroLeakTestInProgress")
        return None if value is None else bool(value)

    @property
    def buzzer_active(self) -> bool | None:
        value = self._get("IsBuzzerActive")
        return None if value is None else bool(value)
