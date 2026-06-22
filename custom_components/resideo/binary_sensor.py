"""Binary sensor platform for the Resideo integration.

Device: online, fan/circulation running, adaptive recovery, vacation hold, schedule enabled,
air-filter alert, fault. Accessory (remote room sensor): motion, occupancy.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .aioresideo import ResideoAccessory
from .coordinator import (
    ResideoConfigEntry,
    ResideoDataUpdateCoordinator,
    ResideoDeviceData,
)
from .entity import ResideoAccessoryEntity, ResideoEntity

DIAG = EntityCategory.DIAGNOSTIC


# --- device-level binary sensors ---------------------------------------------
@dataclass(frozen=True, kw_only=True)
class ResideoBinarySensorEntityDescription(BinarySensorEntityDescription):
    value_fn: Callable[[ResideoDeviceData], bool | None]
    exists_fn: Callable[[ResideoDeviceData], bool] = lambda _data: True


def _ta(d: ResideoDeviceData):
    """The built-in thermostat accessory (source of the thermostat's own motion/occupancy)."""
    return d.rooms.thermostat_accessory


DEVICE_BINARY_SENSORS: tuple[ResideoBinarySensorEntityDescription, ...] = (
    ResideoBinarySensorEntityDescription(
        key="motion", device_class=BinarySensorDeviceClass.MOTION,
        value_fn=lambda d: _ta(d).motion if _ta(d) else None,
        exists_fn=lambda d: _ta(d) is not None and _ta(d).motion is not None,
    ),
    ResideoBinarySensorEntityDescription(
        key="occupancy", device_class=BinarySensorDeviceClass.OCCUPANCY,
        value_fn=lambda d: _ta(d).occupancy if _ta(d) else None,
        exists_fn=lambda d: _ta(d) is not None and _ta(d).occupancy is not None,
    ),
    ResideoBinarySensorEntityDescription(
        key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=DIAG,
        value_fn=lambda d: d.thermostat.online,
    ),
    ResideoBinarySensorEntityDescription(
        key="fan_running", translation_key="fan_running",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda d: d.thermostat.fan_request,
    ),
    ResideoBinarySensorEntityDescription(
        key="circulation_fan", translation_key="circulation_fan",
        device_class=BinarySensorDeviceClass.RUNNING, entity_category=DIAG,
        value_fn=lambda d: d.thermostat.circulation_fan_request,
    ),
    ResideoBinarySensorEntityDescription(
        key="adaptive_recovery_active", translation_key="adaptive_recovery_active",
        device_class=BinarySensorDeviceClass.RUNNING, entity_category=DIAG,
        value_fn=lambda d: d.thermostat.adaptive_recovery_active,
    ),
    ResideoBinarySensorEntityDescription(
        key="vacation_hold", translation_key="vacation_hold", entity_category=DIAG,
        value_fn=lambda d: d.thermostat.vacation_hold,
    ),
    ResideoBinarySensorEntityDescription(
        key="air_filter", translation_key="air_filter",
        device_class=BinarySensorDeviceClass.PROBLEM, entity_category=DIAG,
        value_fn=lambda d: d.thermostat.air_filter_alert,
    ),
    ResideoBinarySensorEntityDescription(
        key="fault", translation_key="fault",
        device_class=BinarySensorDeviceClass.PROBLEM, entity_category=DIAG,
        value_fn=lambda d: d.thermostat.has_fault,
    ),
    # --- from /configuration (only when the config endpoint loaded) ---
    ResideoBinarySensorEntityDescription(
        key="freeze_protection", translation_key="freeze_protection", entity_category=DIAG,
        value_fn=lambda d: d.configuration.freeze_protection_active,
        exists_fn=lambda d: bool(d.configuration.reported),
    ),
    ResideoBinarySensorEntityDescription(
        key="freeze_protection_configured", translation_key="freeze_protection_configured",
        entity_category=DIAG,
        value_fn=lambda d: d.configuration.freeze_protection_configured,
        exists_fn=lambda d: bool(d.configuration.reported),
    ),
    ResideoBinarySensorEntityDescription(
        key="away_mode", translation_key="away_mode", entity_category=DIAG,
        value_fn=lambda d: d.configuration.away_mode_override,
        exists_fn=lambda d: bool(d.configuration.reported),
    ),
    ResideoBinarySensorEntityDescription(
        key="commercial", translation_key="commercial", entity_category=DIAG,
        value_fn=lambda d: d.configuration.commercial_configuration,
        exists_fn=lambda d: bool(d.configuration.reported),
    ),
)


# --- accessory (remote room sensor) binary sensors ---------------------------
@dataclass(frozen=True, kw_only=True)
class ResideoAccessoryBinarySensorEntityDescription(BinarySensorEntityDescription):
    value_fn: Callable[[ResideoAccessory], bool | None]
    exists_fn: Callable[[ResideoAccessory], bool] = lambda _acc: True


ACCESSORY_BINARY_SENSORS: tuple[ResideoAccessoryBinarySensorEntityDescription, ...] = (
    ResideoAccessoryBinarySensorEntityDescription(
        key="motion", device_class=BinarySensorDeviceClass.MOTION,
        value_fn=lambda a: a.motion, exists_fn=lambda a: a.motion is not None,
    ),
    ResideoAccessoryBinarySensorEntityDescription(
        key="occupancy", device_class=BinarySensorDeviceClass.OCCUPANCY,
        value_fn=lambda a: a.occupancy, exists_fn=lambda a: a.occupancy is not None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ResideoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Resideo binary sensors."""
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = []
    for mac, data in coordinator.data.items():
        entities.extend(
            ResideoBinarySensor(coordinator, mac, desc)
            for desc in DEVICE_BINARY_SENSORS
            if desc.exists_fn(data)
        )
        for room, accessory in data.rooms.air_sensor_accessories():
            entities.extend(
                ResideoAccessoryBinarySensor(coordinator, mac, room, accessory, desc)
                for desc in ACCESSORY_BINARY_SENSORS
                if desc.exists_fn(accessory)
            )
    async_add_entities(entities)


class ResideoBinarySensor(ResideoEntity, BinarySensorEntity):
    """A device-level binary sensor off the thermostat shadow."""

    entity_description: ResideoBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: ResideoDataUpdateCoordinator,
        mac: str,
        description: ResideoBinarySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, mac)
        self.entity_description = description
        self._attr_unique_id = f"{mac}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        data = self._device_data
        return self.entity_description.value_fn(data) if data else None


class ResideoAccessoryBinarySensor(ResideoAccessoryEntity, BinarySensorEntity):
    """A binary sensor for a remote room-sensor accessory (a sub-device)."""

    entity_description: ResideoAccessoryBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: ResideoDataUpdateCoordinator,
        mac: str,
        room,
        accessory: ResideoAccessory,
        description: ResideoAccessoryBinarySensorEntityDescription,
    ) -> None:
        super().__init__(
            coordinator, mac, room.id, accessory.accessory_id, room.name, accessory.model
        )
        self.entity_description = description
        self._attr_unique_id = (
            f"{mac}_room{room.id}_acc{accessory.accessory_id}_{description.key}"
        )

    @property
    def is_on(self) -> bool | None:
        accessory = self.accessory
        return self.entity_description.value_fn(accessory) if accessory else None
