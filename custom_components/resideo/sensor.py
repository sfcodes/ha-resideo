"""Sensor platform for the Resideo integration.

Exposes the full readable surface:
  - **device** sensors on the thermostat: temps/humidity, CO2/TVOC, setpoints, HVAC demand/
    stage, IAQ qualities, hold status, schedule, backlight, air filter, firmware, faults.
  - **accessory** sensors for each remote IndoorAirSensor (a sub-device): temperature,
    humidity, actual temperature, battery, signal strength, status.
Secondary readouts use the diagnostic category so they can be pruned later.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    CONCENTRATION_PARTS_PER_MILLION,
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from .aioresideo import ResideoAccessory
from .coordinator import (
    ResideoConfigEntry,
    ResideoDataUpdateCoordinator,
    ResideoDeviceData,
)
from .entity import ResideoAccessoryEntity, ResideoEntity

DIAG = EntityCategory.DIAGNOSTIC
TEMP_F = UnitOfTemperature.FAHRENHEIT


# --- device-level sensors (the thermostat) -----------------------------------
@dataclass(frozen=True, kw_only=True)
class ResideoSensorEntityDescription(SensorEntityDescription):
    """Thermostat sensor description with a getter over the per-device data."""

    value_fn: Callable[[ResideoDeviceData], StateType]
    exists_fn: Callable[[ResideoDeviceData], bool] = lambda _data: True


def _co2(d: ResideoDeviceData) -> StateType:
    acc = d.rooms.thermostat_accessory
    return acc.co2 if acc else None


def _tvoc(d: ResideoDeviceData) -> StateType:
    acc = d.rooms.thermostat_accessory
    return acc.tvoc if acc else None


DEVICE_SENSORS: tuple[ResideoSensorEntityDescription, ...] = (
    # --- primary environment ---
    ResideoSensorEntityDescription(
        key="indoor_temperature", translation_key="indoor_temperature",
        device_class=SensorDeviceClass.TEMPERATURE, native_unit_of_measurement=TEMP_F,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.thermostat.indoor_temperature,
    ),
    ResideoSensorEntityDescription(
        key="outdoor_temperature", translation_key="outdoor_temperature",
        device_class=SensorDeviceClass.TEMPERATURE, native_unit_of_measurement=TEMP_F,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.thermostat.outdoor_temperature,
    ),
    ResideoSensorEntityDescription(
        key="indoor_humidity", translation_key="indoor_humidity",
        device_class=SensorDeviceClass.HUMIDITY, native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.thermostat.indoor_humidity,
    ),
    ResideoSensorEntityDescription(
        key="outdoor_humidity", translation_key="outdoor_humidity",
        device_class=SensorDeviceClass.HUMIDITY, native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.thermostat.outdoor_humidity,
    ),
    ResideoSensorEntityDescription(
        key="co2", device_class=SensorDeviceClass.CO2,
        native_unit_of_measurement=CONCENTRATION_PARTS_PER_MILLION,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_co2, exists_fn=lambda d: _co2(d) is not None,
    ),
    ResideoSensorEntityDescription(
        key="tvoc", translation_key="tvoc", state_class=SensorStateClass.MEASUREMENT,
        value_fn=_tvoc, exists_fn=lambda d: _tvoc(d) is not None,
    ),
    # --- setpoints / HVAC activity (diagnostic) ---
    ResideoSensorEntityDescription(
        key="heat_setpoint", translation_key="heat_setpoint", entity_category=DIAG,
        device_class=SensorDeviceClass.TEMPERATURE, native_unit_of_measurement=TEMP_F,
        value_fn=lambda d: d.thermostat.heat_setpoint,
    ),
    ResideoSensorEntityDescription(
        key="cool_setpoint", translation_key="cool_setpoint", entity_category=DIAG,
        device_class=SensorDeviceClass.TEMPERATURE, native_unit_of_measurement=TEMP_F,
        value_fn=lambda d: d.thermostat.cool_setpoint,
    ),
    ResideoSensorEntityDescription(
        key="setpoint_status", translation_key="setpoint_status", entity_category=DIAG,
        value_fn=lambda d: d.thermostat.setpoint_status,
    ),
    ResideoSensorEntityDescription(
        key="equipment_status", translation_key="equipment_status", entity_category=DIAG,
        value_fn=lambda d: d.thermostat.operation_mode,
    ),
    ResideoSensorEntityDescription(
        key="demand", translation_key="demand", entity_category=DIAG,
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.thermostat.demand,
    ),
    ResideoSensorEntityDescription(
        key="current_stage", translation_key="current_stage", entity_category=DIAG,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.thermostat.current_stage,
    ),
    # --- indoor air quality (qualitative, diagnostic) ---
    ResideoSensorEntityDescription(
        key="air_quality", translation_key="air_quality", entity_category=DIAG,
        value_fn=lambda d: d.thermostat.air_quality,
    ),
    ResideoSensorEntityDescription(
        key="co2_quality", translation_key="co2_quality", entity_category=DIAG,
        value_fn=lambda d: d.thermostat.co2_quality,
    ),
    ResideoSensorEntityDescription(
        key="voc_quality", translation_key="voc_quality", entity_category=DIAG,
        value_fn=lambda d: d.thermostat.voc_quality,
    ),
    ResideoSensorEntityDescription(
        key="humidity_quality", translation_key="humidity_quality", entity_category=DIAG,
        value_fn=lambda d: d.thermostat.humidity_quality,
    ),
    # --- schedule / display / priority (diagnostic) ---
    ResideoSensorEntityDescription(
        key="schedule_period", translation_key="schedule_period", entity_category=DIAG,
        value_fn=lambda d: d.thermostat.schedule_period,
    ),
    ResideoSensorEntityDescription(
        key="backlight", translation_key="backlight", entity_category=DIAG,
        value_fn=lambda d: d.thermostat.backlight_state,
    ),
    ResideoSensorEntityDescription(
        key="priority_type", translation_key="priority_type", entity_category=DIAG,
        value_fn=lambda d: d.thermostat.current_priority_type,
    ),
    # --- maintenance / firmware / faults (diagnostic) ---
    ResideoSensorEntityDescription(
        key="air_filter_remaining", translation_key="air_filter_remaining", entity_category=DIAG,
        native_unit_of_measurement=UnitOfTime.DAYS, state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.thermostat.air_filter_remaining_days,
    ),
    ResideoSensorEntityDescription(
        key="firmware_version", translation_key="firmware_version", entity_category=DIAG,
        value_fn=lambda d: d.thermostat.firmware_version,
    ),
    ResideoSensorEntityDescription(
        key="fault_count", translation_key="fault_count", entity_category=DIAG,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.thermostat.fault_count,
    ),
    # --- connectivity / mode detail / staging (diagnostic) ---
    ResideoSensorEntityDescription(
        key="signal_strength", device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT, entity_category=DIAG,
        value_fn=lambda d: d.thermostat.signal_strength,
        exists_fn=lambda d: d.thermostat.signal_strength is not None,
    ),
    ResideoSensorEntityDescription(
        key="heat_cool_mode", translation_key="heat_cool_mode", entity_category=DIAG,
        value_fn=lambda d: d.thermostat.heat_cool_mode,
        exists_fn=lambda d: d.thermostat.heat_cool_mode is not None,
    ),
    ResideoSensorEntityDescription(
        # Always present (deterministic across restarts); empty -> None when the fan
        # isn't being requested for a specific reason.
        key="fan_reason", translation_key="fan_reason", entity_category=DIAG,
        value_fn=lambda d: ", ".join(d.thermostat.fan_request_reasons) or None,
    ),
    ResideoSensorEntityDescription(
        key="demand_response", translation_key="demand_response", entity_category=DIAG,
        value_fn=lambda d: d.thermostat.demand_response_state,
        exists_fn=lambda d: d.thermostat.demand_response_state is not None,
    ),
    # --- schedule / recovery / ventilation / display (diagnostic) ---
    ResideoSensorEntityDescription(
        key="schedule_day", translation_key="schedule_day", entity_category=DIAG,
        value_fn=lambda d: d.thermostat.schedule_day,
        exists_fn=lambda d: d.thermostat.schedule_day is not None,
    ),
    ResideoSensorEntityDescription(
        key="schedule_type", translation_key="schedule_type", entity_category=DIAG,
        value_fn=lambda d: d.thermostat.schedule_type,
        exists_fn=lambda d: d.thermostat.schedule_type is not None,
    ),
    ResideoSensorEntityDescription(
        key="adaptive_recovery_mode", translation_key="adaptive_recovery_mode", entity_category=DIAG,
        value_fn=lambda d: d.thermostat.active_adaptive_recovery_mode,
        exists_fn=lambda d: d.thermostat.active_adaptive_recovery_mode is not None,
    ),
    ResideoSensorEntityDescription(
        key="ventilation_timer", translation_key="ventilation_timer", entity_category=DIAG,
        native_unit_of_measurement=UnitOfTime.MINUTES, state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.thermostat.ventilation_timer,
        exists_fn=lambda d: d.thermostat.ventilation_timer is not None,
    ),
    ResideoSensorEntityDescription(
        key="ventilation_boost_timer", translation_key="ventilation_boost_timer", entity_category=DIAG,
        native_unit_of_measurement=UnitOfTime.MINUTES, state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.thermostat.ventilation_boost_timer,
        exists_fn=lambda d: d.thermostat.ventilation_boost_timer is not None,
    ),
    ResideoSensorEntityDescription(
        key="language", translation_key="language", entity_category=DIAG,
        value_fn=lambda d: d.thermostat.current_language,
        exists_fn=lambda d: d.thermostat.current_language is not None,
    ),
    ResideoSensorEntityDescription(
        key="firmware_status", translation_key="firmware_status", entity_category=DIAG,
        value_fn=lambda d: d.thermostat.firmware_status,
        exists_fn=lambda d: d.thermostat.firmware_status is not None,
    ),
    ResideoSensorEntityDescription(
        key="registered", translation_key="registered", entity_category=DIAG,
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d: dt_util.parse_datetime(d.thermostat.registration_date or ""),
        exists_fn=lambda d: dt_util.parse_datetime(d.thermostat.registration_date or "") is not None,
    ),
    ResideoSensorEntityDescription(
        key="firmware_updated", translation_key="firmware_updated", entity_category=DIAG,
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d: dt_util.parse_datetime(d.thermostat.firmware_last_updated or ""),
        exists_fn=lambda d: dt_util.parse_datetime(d.thermostat.firmware_last_updated or "") is not None,
    ),
    # --- equipment configuration (diagnostic; from /configuration) ---
    ResideoSensorEntityDescription(
        key="heating_system", translation_key="heating_system", entity_category=DIAG,
        value_fn=lambda d: d.configuration.heating_system,
        exists_fn=lambda d: d.configuration.heating_system is not None,
    ),
    ResideoSensorEntityDescription(
        key="heating_stages", translation_key="heating_stages", entity_category=DIAG,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.configuration.heating_stages,
        exists_fn=lambda d: d.configuration.heating_stages is not None,
    ),
    ResideoSensorEntityDescription(
        key="cooling_system", translation_key="cooling_system", entity_category=DIAG,
        value_fn=lambda d: d.configuration.cooling_system,
        exists_fn=lambda d: d.configuration.cooling_system is not None,
    ),
    ResideoSensorEntityDescription(
        key="cooling_stages", translation_key="cooling_stages", entity_category=DIAG,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.configuration.cooling_stages,
        exists_fn=lambda d: d.configuration.cooling_stages is not None,
    ),
    ResideoSensorEntityDescription(
        key="temperature_units", translation_key="temperature_units", entity_category=DIAG,
        value_fn=lambda d: d.configuration.temperature_units,
        exists_fn=lambda d: d.configuration.temperature_units is not None,
    ),
    ResideoSensorEntityDescription(
        key="matter_status", translation_key="matter_status", entity_category=DIAG,
        value_fn=lambda d: d.configuration.matter_commissioning_status,
        exists_fn=lambda d: d.configuration.matter_commissioning_status is not None,
    ),
    # --- room priority (diagnostic; from /priority) ---
    ResideoSensorEntityDescription(
        key="priority_status", translation_key="priority_status", entity_category=DIAG,
        value_fn=lambda d: d.priority.priority_status,
        exists_fn=lambda d: d.priority.priority_status is not None,
    ),
)


# --- accessory (remote room sensor) sensors ----------------------------------
@dataclass(frozen=True, kw_only=True)
class ResideoAccessorySensorEntityDescription(SensorEntityDescription):
    """Remote-accessory sensor description with a getter over a single accessory."""

    value_fn: Callable[[ResideoAccessory], StateType]
    exists_fn: Callable[[ResideoAccessory], bool] = lambda _acc: True


ACCESSORY_SENSORS: tuple[ResideoAccessorySensorEntityDescription, ...] = (
    ResideoAccessorySensorEntityDescription(
        key="temperature", device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=TEMP_F, state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda a: a.indoor_temperature,
        exists_fn=lambda a: a.indoor_temperature is not None,
    ),
    ResideoAccessorySensorEntityDescription(
        key="humidity", device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda a: a.indoor_humidity,
        exists_fn=lambda a: a.indoor_humidity is not None,
    ),
    ResideoAccessorySensorEntityDescription(
        key="temperature_actual", translation_key="temperature_actual", entity_category=DIAG,
        device_class=SensorDeviceClass.TEMPERATURE, native_unit_of_measurement=TEMP_F,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda a: a.temperature_actual,
        exists_fn=lambda a: a.temperature_actual is not None,
    ),
    ResideoAccessorySensorEntityDescription(
        key="co2", device_class=SensorDeviceClass.CO2,
        native_unit_of_measurement=CONCENTRATION_PARTS_PER_MILLION,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda a: a.co2, exists_fn=lambda a: a.co2 is not None,
    ),
    ResideoAccessorySensorEntityDescription(
        key="voc", translation_key="tvoc", state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda a: a.tvoc, exists_fn=lambda a: a.tvoc is not None,
    ),
    ResideoAccessorySensorEntityDescription(
        key="battery_status", translation_key="battery_status", entity_category=DIAG,
        value_fn=lambda a: a.battery_status, exists_fn=lambda a: a.battery_status is not None,
    ),
    ResideoAccessorySensorEntityDescription(
        key="signal_strength", device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT, entity_category=DIAG,
        value_fn=lambda a: a.rssi, exists_fn=lambda a: a.rssi is not None,
    ),
    ResideoAccessorySensorEntityDescription(
        key="status", translation_key="status", entity_category=DIAG,
        value_fn=lambda a: a.status, exists_fn=lambda a: a.status is not None,
    ),
    # occupancy_sensitivity is now a writable `select` (see select.py), not a read-only sensor.
    ResideoAccessorySensorEntityDescription(
        key="occupancy_timeout", translation_key="occupancy_timeout", entity_category=DIAG,
        native_unit_of_measurement=UnitOfTime.SECONDS, state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda a: a.occupancy_timeout,
        exists_fn=lambda a: a.occupancy_timeout is not None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ResideoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Resideo sensor entities (device sensors + remote-accessory sub-devices)."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for mac, data in coordinator.data.items():
        entities.extend(
            ResideoSensor(coordinator, mac, desc)
            for desc in DEVICE_SENSORS
            if desc.exists_fn(data)
        )
        for room, accessory in data.rooms.air_sensor_accessories():
            entities.extend(
                ResideoAccessorySensor(coordinator, mac, room, accessory, desc)
                for desc in ACCESSORY_SENSORS
                if desc.exists_fn(accessory)
            )
    async_add_entities(entities)


class ResideoSensor(ResideoEntity, SensorEntity):
    """A sensor reading a value off the thermostat's per-device data."""

    entity_description: ResideoSensorEntityDescription

    def __init__(
        self,
        coordinator: ResideoDataUpdateCoordinator,
        mac: str,
        description: ResideoSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, mac)
        self.entity_description = description
        self._attr_unique_id = f"{mac}_{description.key}"

    @property
    def native_value(self) -> StateType:
        data = self._device_data
        return self.entity_description.value_fn(data) if data else None


class ResideoAccessorySensor(ResideoAccessoryEntity, SensorEntity):
    """A sensor for a remote room-sensor accessory (a sub-device)."""

    entity_description: ResideoAccessorySensorEntityDescription

    def __init__(
        self,
        coordinator: ResideoDataUpdateCoordinator,
        mac: str,
        room,
        accessory: ResideoAccessory,
        description: ResideoAccessorySensorEntityDescription,
    ) -> None:
        super().__init__(
            coordinator, mac, room.id, accessory.accessory_id, room.name, accessory.model
        )
        self.entity_description = description
        self._attr_unique_id = (
            f"{mac}_room{room.id}_acc{accessory.accessory_id}_{description.key}"
        )

    @property
    def native_value(self) -> StateType:
        accessory = self.accessory
        return self.entity_description.value_fn(accessory) if accessory else None
