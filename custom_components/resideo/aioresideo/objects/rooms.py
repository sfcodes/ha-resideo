"""Rooms + accessory models (from GET /devsrv/api/v2/device/{mac}/group/0/rooms).

This is the authoritative per-accessory **value** source (live temperature/humidity/CO2/TVOC/
RSSI/battery), see ``resideo-api-spec.md`` §3. Shape::

    {DeviceId, GroupId, GroupName, Rooms:[
        {Id, Name, Type, OverallMotion, AvgTemperature, AvgHumidity, Accessories:[
            {AccessoryId, AccessoryAttribute:{Type, Name, Model, SerialNumber, ...},
             AccessoryValue:{IndoorTemperature, IndoorHumidity, RssiAverage, BatteryStatus,
                             CarbonDioxide:{Measurement, Displayed}, TotalVolatileOrganicCompounds:{...}}}]}]}
"""

from __future__ import annotations

from typing import Any

from .base import ResideoBaseObject


class ResideoAccessory(ResideoBaseObject):
    """One ``Rooms[].Accessories[]`` entry: a thermostat or a remote IndoorAirSensor."""

    @property
    def accessory_id(self) -> int | None:
        return self.attributes.get("AccessoryId")

    @property
    def _attr(self) -> dict[str, Any]:
        return self.attributes.get("AccessoryAttribute", {}) or {}

    @property
    def _value(self) -> dict[str, Any]:
        return self.attributes.get("AccessoryValue", {}) or {}

    # -- identity (from AccessoryAttribute) -----------------------------------
    @property
    def type(self) -> str | None:
        """``Thermostat`` or ``IndoorAirSensor``."""
        return self._attr.get("Type")

    @property
    def name(self) -> str | None:
        return self._attr.get("Name")

    @property
    def model(self) -> str | None:
        return self._attr.get("Model")

    @property
    def serial_number(self) -> str | None:
        return self._attr.get("SerialNumber")

    @property
    def software_revision(self) -> str | None:
        return self._attr.get("SoftwareRevision")

    @property
    def hardware_revision(self) -> str | None:
        return self._attr.get("HardwareRevision")

    @property
    def connection_method(self) -> str | None:
        return self._attr.get("ConnectionMethod")

    # -- live values (from AccessoryValue) ------------------------------------
    @property
    def indoor_temperature(self) -> float | None:
        return self._value.get("IndoorTemperature")

    @property
    def indoor_humidity(self) -> int | None:
        return self._value.get("IndoorHumidity")

    @property
    def rssi(self) -> int | None:
        return self._value.get("RssiAverage")

    @property
    def battery_status(self) -> str | None:
        return self._value.get("BatteryStatus")

    @property
    def motion(self) -> bool | None:
        return self._value.get("MotionDet")

    @property
    def occupancy(self) -> bool | None:
        return self._value.get("OccupancyDet")

    @property
    def temperature_actual(self) -> float | None:
        """Raw measured temperature (vs the displayed/rounded ``indoor_temperature``)."""
        return self._value.get("TemperatureActual")

    @property
    def status(self) -> str | None:
        return self._value.get("Status")

    @property
    def occupancy_sensitivity(self) -> str | None:
        """Motion/occupancy sensitivity, e.g. 'Low'/'Medium'/'High'."""
        return self._value.get("OccupancySensitivity")

    @property
    def occupancy_timeout(self) -> int | None:
        return self._value.get("OccupancyTimeout")

    @property
    def exclude_motion(self) -> bool:
        """Whether this accessory's motion is excluded from room aggregation."""
        return bool(self._value.get("ExcludeMotion", False))

    @property
    def exclude_temperature(self) -> bool:
        return bool(self._value.get("ExcludeTemp", False))

    def _measurement(self, key: str) -> float | None:
        """Numeric measurement from a ``{Measurement, Displayed}`` block, if displayed."""
        block = self._value.get(key)
        if isinstance(block, dict) and block.get("Displayed") and block.get("Measurement") is not None:
            return block.get("Measurement")
        return None

    @property
    def co2(self) -> float | None:
        return self._measurement("CarbonDioxide")

    @property
    def tvoc(self) -> float | None:
        return self._measurement("TotalVolatileOrganicCompounds")

    # -- classification -------------------------------------------------------
    @property
    def is_air_sensor(self) -> bool:
        return self.type == "IndoorAirSensor"

    @property
    def is_thermostat(self) -> bool:
        return self.type == "Thermostat"


class ResideoRoom(ResideoBaseObject):
    """A room with averaged values and its accessories."""

    @property
    def id(self) -> int | None:
        return self.attributes.get("Id")

    @property
    def name(self) -> str | None:
        return self.attributes.get("Name")

    @property
    def type(self) -> str | None:
        return self.attributes.get("Type")

    @property
    def avg_temperature(self) -> float | None:
        return self.attributes.get("AvgTemperature")

    @property
    def avg_humidity(self) -> float | None:
        return self.attributes.get("AvgHumidity")

    @property
    def overall_motion(self) -> bool:
        return bool(self.attributes.get("OverallMotion", False))

    @property
    def accessories(self) -> list[ResideoAccessory]:
        return [ResideoAccessory(a) for a in (self.attributes.get("Accessories") or [])]


class ResideoRooms(ResideoBaseObject):
    """Root of the ``/group/0/rooms`` response."""

    @property
    def device_id(self) -> str | None:
        return self.attributes.get("DeviceId")

    @property
    def group_id(self) -> int | None:
        return self.attributes.get("GroupId")

    @property
    def rooms(self) -> list[ResideoRoom]:
        return [ResideoRoom(r) for r in (self.attributes.get("Rooms") or [])]

    @property
    def thermostat_accessory(self) -> ResideoAccessory | None:
        """The built-in thermostat accessory (carries the device's CO2/TVOC), if any."""
        for room in self.rooms:
            for accessory in room.accessories:
                if accessory.is_thermostat:
                    return accessory
        return None

    def air_sensor_accessories(self) -> list[tuple[ResideoRoom, ResideoAccessory]]:
        """(room, accessory) pairs for each remote IndoorAirSensor — the sub-devices."""
        return [
            (room, accessory)
            for room in self.rooms
            for accessory in room.accessories
            if accessory.is_air_sensor
        ]
