"""Select platform for the Resideo integration — per-accessory occupancy controls.

Currently: a remote room-sensor's **Occupancy sensitivity** (``accessoryValue`` ``sensitivity`` →
read ``OccupancySensitivity``; ``resideo-api-spec.md`` §10).

Unlike the device switches, this setting is **eventually-consistent**: the API accepts the write
(``202``) but the battery-powered wireless sensor only applies it on its next check-in, so there is
no immediate read-back (§10.4). The entity therefore holds the chosen value optimistically and clears
it only once a later refresh actually reports it — there is **no reconcile timer** (a fixed delay
would wrongly snap the UI back before the sensor has checked in).
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .aioresideo.const import OCCUPANCY_SENSITIVITY_OPTIONS
from .aioresideo.exceptions import ResideoError
from .coordinator import ResideoConfigEntry, ResideoDataUpdateCoordinator
from .entity import ResideoAccessoryEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ResideoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Resideo selects (per remote room-sensor accessory)."""
    coordinator = entry.runtime_data
    entities: list[SelectEntity] = []
    for mac, data in coordinator.data.items():
        for room, accessory in data.rooms.air_sensor_accessories():
            if accessory.occupancy_sensitivity is not None:
                entities.append(
                    ResideoOccupancySensitivitySelect(coordinator, mac, room, accessory)
                )
    async_add_entities(entities)


class ResideoOccupancySensitivitySelect(ResideoAccessoryEntity, SelectEntity):
    """A remote sensor's occupancy-sensitivity select (optimistic, reconcile-on-report)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "occupancy_sensitivity"
    _attr_options = list(OCCUPANCY_SENSITIVITY_OPTIONS)

    def __init__(
        self,
        coordinator: ResideoDataUpdateCoordinator,
        mac: str,
        room,
        accessory,
    ) -> None:
        super().__init__(
            coordinator, mac, room.id, accessory.accessory_id, room.name, accessory.model
        )
        self._attr_unique_id = (
            f"{mac}_room{room.id}_acc{accessory.accessory_id}_occupancy_sensitivity"
        )
        # Held until a refresh reports the chosen value (sensitivity is eventually-consistent).
        self._optimistic: str | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._optimistic is not None:
            accessory = self.accessory
            if accessory is not None and accessory.occupancy_sensitivity == self._optimistic:
                self._optimistic = None
                self.coordinator.accessory_override(self._mac, self._accessory_id).pop(
                    "sensitivity", None
                )
        super()._handle_coordinator_update()

    @property
    def current_option(self) -> str | None:
        if self._optimistic is not None:
            return self._optimistic
        accessory = self.accessory
        return accessory.occupancy_sensitivity if accessory else None

    async def async_select_option(self, option: str) -> None:
        accessory = self.accessory
        if accessory is None:
            raise HomeAssistantError("Accessory is unavailable")
        # Full-body write composed from the accessory's shared overrides (carries the current
        # excludes; see ResideoDataUpdateCoordinator.async_write_accessory_value).
        try:
            await self.coordinator.async_write_accessory_value(
                self._mac, self._accessory_id, accessory, field="sensitivity", value=option
            )
        except ResideoError as err:
            raise HomeAssistantError(f"Failed to set occupancy sensitivity: {err}") from err
        self._optimistic = option
        self.async_write_ha_state()
        # A refresh now will not reflect the change (deferred to the sensor's next check-in); the
        # optimistic value is cleared by _handle_coordinator_update whenever a later refresh reports
        # it (e.g. the periodic reconnect resync, or a Sensor push that carries it).
        await self.coordinator.async_refresh()
