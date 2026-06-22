"""Base entities for the Resideo integration."""

from __future__ import annotations

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .aioresideo import (
    ResideoAccessory,
    ResideoConfiguration,
    ResideoPriority,
    ResideoRooms,
    ResideoThermostat,
)
from .const import DOMAIN, MANUFACTURER
from .coordinator import ResideoDataUpdateCoordinator, ResideoDeviceData


class ResideoEntity(CoordinatorEntity[ResideoDataUpdateCoordinator]):
    """Base entity bound to one Resideo thermostat (keyed by MAC)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ResideoDataUpdateCoordinator, mac: str) -> None:
        super().__init__(coordinator)
        self._mac = mac
        self._attr_unique_id = mac

    @property
    def _device_data(self) -> ResideoDeviceData | None:
        return self.coordinator.data.get(self._mac)

    @property
    def device(self) -> ResideoThermostat | None:
        """The thermostat shadow for this entity."""
        data = self._device_data
        return data.thermostat if data else None

    @property
    def rooms(self) -> ResideoRooms | None:
        """The room/accessory data for this thermostat (may be empty)."""
        data = self._device_data
        return data.rooms if data else None

    @property
    def configuration(self) -> ResideoConfiguration | None:
        """The device capabilities/equipment config (may be empty)."""
        data = self._device_data
        return data.configuration if data else None

    @property
    def priority(self) -> ResideoPriority | None:
        """The room-priority/selection state (may be empty)."""
        data = self._device_data
        return data.priority if data else None

    @property
    def available(self) -> bool:
        device = self.device
        return super().available and device is not None and device.online

    @property
    def device_info(self) -> DeviceInfo:
        device = self.device
        return DeviceInfo(
            identifiers={(DOMAIN, self._mac)},
            connections={(dr.CONNECTION_NETWORK_MAC, dr.format_mac(self._mac))},
            manufacturer=MANUFACTURER,
            name=(device.name if device else None) or f"Resideo {self._mac}",
            model=device.model if device else None,
            sw_version=device.firmware_version if device else None,
            serial_number=device.serial_number if device else None,
        )


class ResideoAccessoryEntity(ResideoEntity):
    """Base for a remote room-sensor accessory, modeled as a sub-device of the thermostat."""

    def __init__(
        self,
        coordinator: ResideoDataUpdateCoordinator,
        mac: str,
        room_id: int,
        accessory_id: int,
        room_name: str | None,
        model: str | None,
    ) -> None:
        super().__init__(coordinator, mac)
        self._room_id = room_id
        self._accessory_id = accessory_id
        self._room_name = room_name
        self._accessory_model = model
        self._attr_unique_id = f"{mac}_room{room_id}_acc{accessory_id}"

    @property
    def accessory(self) -> ResideoAccessory | None:
        """Resolve this accessory in the latest coordinator data."""
        rooms = self.rooms
        if rooms is None:
            return None
        for room in rooms.rooms:
            if room.id == self._room_id:
                for accessory in room.accessories:
                    if accessory.accessory_id == self._accessory_id:
                        return accessory
        return None

    @property
    def available(self) -> bool:
        return super().available and self.accessory is not None

    @property
    def device_info(self) -> DeviceInfo:
        accessory = self.accessory
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._mac}_room{self._room_id}_acc{self._accessory_id}")},
            name=self._room_name or f"Resideo room {self._room_id}",
            manufacturer=MANUFACTURER,
            model=self._accessory_model,
            sw_version=accessory.software_revision if accessory else None,
            serial_number=accessory.serial_number if accessory else None,
            via_device=(DOMAIN, self._mac),
        )
