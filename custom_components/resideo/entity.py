"""Base entities for the Resideo integration."""

from __future__ import annotations

from typing import Any

from homeassistant.const import UnitOfTemperature
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_call_later
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

# After a write, force a confirming refresh this many seconds later — long enough for the
# device to apply the command and the shadow to reflect it (~8s observed live).
RECONCILE_DELAY = 10  # seconds


class ResideoEntity(CoordinatorEntity[ResideoDataUpdateCoordinator]):
    """Base entity bound to one Resideo thermostat (keyed by MAC).

    Subclasses must set their own ``_attr_unique_id`` (suffixed per entity).
    """

    _attr_has_entity_name = True
    # When False the entity stays available while the thermostat is offline — used by the
    # connectivity sensor, whose whole job is to report that offline state as "off".
    _requires_device_online: bool = True

    def __init__(self, coordinator: ResideoDataUpdateCoordinator, mac: str) -> None:
        super().__init__(coordinator)
        self._mac = mac

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
        if not super().available or device is None:
            return False
        return device.online or not self._requires_device_online

    @property
    def device_temperature_unit(self) -> str:
        """The device's configured display unit — the unit its temperatures are reported in."""
        config = self.configuration
        if config is not None and config.temperature_units == "C":
            return UnitOfTemperature.CELSIUS
        return UnitOfTemperature.FAHRENHEIT

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


class OptimisticWriteMixin:
    """Optimistic-write plumbing shared by every writable Resideo entity.

    Mix in **before** ``ResideoEntity``/``ResideoAccessoryEntity`` in the class bases.

    The consumer API is asynchronous (``202`` = accepted, not applied) and the shadow lags a
    write by a few seconds, so a successful write is shown immediately from ``_optimistic``
    and reconciled against later data:

      - every coordinator update drops the optimistic values the fresh data now confirms
        (compared via :meth:`_confirmed_values`), so the UI never flickers on a stale read;
      - a forced reconcile refresh ``_reconcile_delay`` seconds after the last write clears
        whatever remains, so the UI always converges to the device's reported truth. Entities
        whose writes are eventually-consistent (no timely read-back, e.g. remote-sensor
        sensitivity) set ``_reconcile_delay = None`` and rely on confirmation alone.
    """

    _reconcile_delay: float | None = RECONCILE_DELAY

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._optimistic: dict[str, Any] = {}
        self._reconcile_unsub: CALLBACK_TYPE | None = None

    # -- subclass hooks --------------------------------------------------------
    def _confirmed_values(self) -> dict[str, Any] | None:
        """Optimistic key -> the value the device currently reports (``None`` = no data)."""
        raise NotImplementedError

    @callback
    def _on_optimistic_cleared(self, key: str) -> None:
        """A key was confirmed/cleared — pop shared coordinator overrides here if any."""

    # -- plumbing ---------------------------------------------------------------
    @callback
    def _handle_coordinator_update(self) -> None:
        if self._optimistic:
            confirmed = self._confirmed_values()
            if confirmed is not None:
                for key, value in confirmed.items():
                    if key in self._optimistic and self._optimistic[key] == value:
                        del self._optimistic[key]
                        self._on_optimistic_cleared(key)
        super()._handle_coordinator_update()

    async def _async_post_write(self, values: dict[str, Any]) -> None:
        """Reflect a just-accepted write immediately and schedule its reconciliation.

        Schedules the coordinator's debounced resync rather than forcing an immediate
        refresh: the optimistic value already covers the UI, the push stream confirms
        merged facets in ~1-3 s, and the debounce lets the write settle on the cloud
        before the re-read.
        """
        self._optimistic.update(values)
        self.async_write_ha_state()
        self.coordinator.schedule_resync()
        self._schedule_reconcile()

    def _schedule_reconcile(self) -> None:
        """Force one refresh after the device has had time to apply, then drop any remaining
        optimistic values so the UI converges to the device's reported truth."""
        if self._reconcile_delay is None:
            return
        if self._reconcile_unsub is not None:
            self._reconcile_unsub()

        async def _reconcile(_now: Any) -> None:
            self._reconcile_unsub = None
            await self.coordinator.async_refresh()
            if self._optimistic:
                for key in list(self._optimistic):
                    del self._optimistic[key]
                    self._on_optimistic_cleared(key)
                self.async_write_ha_state()

        self._reconcile_unsub = async_call_later(self.hass, self._reconcile_delay, _reconcile)

    async def async_will_remove_from_hass(self) -> None:
        """Cancel a pending reconcile so it can't fire after the entity is gone."""
        if self._reconcile_unsub is not None:
            self._reconcile_unsub()
            self._reconcile_unsub = None
        await super().async_will_remove_from_hass()
