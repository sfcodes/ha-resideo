"""Switch platform for the Resideo integration — writable boolean settings.

Device switches map to consumer-API commands verified live (``resideo-api-spec.md`` §4):
- **Feels Like** (``feelsLike`` → ``Reported.FeelsLikeEnabled``)
- **Adaptive Recovery** (``adaptiveIntelligentRecovery`` ``Mode`` → ``Reported.ActiveAdaptiveRecoveryMode``)
- **Schedule** (``schedule/enabled`` → ``Reported.ScheduleEnabled``)

Per-accessory switches map to a remote room-sensor's aggregation flags (``accessoryValue`` →
``/group/0/rooms``; §10):
- **Exclude motion** (``excludeMotion`` → ``ExcludeMotion``)
- **Exclude temperature** (``excludeTemp`` → ``ExcludeTemp``)

Writes are **optimistic + reconciled**, mirroring the climate entity: the consumer API is async
(``202`` = accepted, not applied) and the shadow lags a few seconds, so a successful write shows
immediately and is reconciled against the next refresh. (The exclude flags read back in ~10 s — §10.4
— so the standard reconcile timer fits; sensitivity, which is eventually-consistent, is a ``select``.)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .aioresideo import Resideo, ResideoAccessory
from .aioresideo.const import (
    ADAPTIVE_RECOVERY_INTELLIGENT,
    ADAPTIVE_RECOVERY_NONE,
    SYSTEM_SWITCH_EMERGENCY_HEAT,
    SYSTEM_SWITCH_HEAT,
)
from .aioresideo.exceptions import ResideoError
from .coordinator import (
    ResideoConfigEntry,
    ResideoDataUpdateCoordinator,
    ResideoDeviceData,
)
from .entity import OptimisticWriteMixin, ResideoAccessoryEntity, ResideoEntity

# Writes are commands; serialize service calls per platform (reads are pure push).
PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class ResideoSwitchEntityDescription(SwitchEntityDescription):
    """A writable boolean device setting."""

    value_fn: Callable[[ResideoDeviceData], bool | None]
    set_fn: Callable[[Resideo, str, bool], Awaitable[Any]]
    exists_fn: Callable[[ResideoDeviceData], bool] = lambda _data: True


DEVICE_SWITCHES: tuple[ResideoSwitchEntityDescription, ...] = (
    ResideoSwitchEntityDescription(
        key="feels_like",
        translation_key="feels_like",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda d: d.thermostat.feels_like_enabled,
        set_fn=lambda api, mac, on: api.async_set_feels_like(mac, on),
    ),
    ResideoSwitchEntityDescription(
        key="adaptive_recovery",
        translation_key="adaptive_recovery",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda d: d.thermostat.adaptive_recovery_enabled,
        # on -> AdaptiveIntelligentRecovery, off -> None (the DelayedStartRecovery mode is not
        # expressible as a 2-state switch; a select could expose all three later).
        set_fn=lambda api, mac, on: api.async_set_adaptive_recovery(
            mac, ADAPTIVE_RECOVERY_INTELLIGENT if on else ADAPTIVE_RECOVERY_NONE
        ),
    ),
    ResideoSwitchEntityDescription(
        key="schedule",
        translation_key="schedule",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda d: d.thermostat.schedule_enabled,
        # ⚠️ Enabling makes the device follow the schedule period (setpoints jump + SetpointStatus
        # -> NoHold); disabling keeps the schedule's last setpoints. The climate setpoint reflects
        # whatever the device does after the toggle (no stateful re-assert here).
        set_fn=lambda api, mac, on: api.async_set_schedule_enabled(mac, on),
    ),
    ResideoSwitchEntityDescription(
        key="emergency_heat",
        translation_key="emergency_heat",
        # EmergencyHeat has no HA HVAC mode, so it is a switch instead. On -> systemSwitch
        # EmergencyHeat; off -> Heat (it's a heat-family mode, so "off" lands on Heat, not the
        # prior mode). The climate entity reads EmergencyHeat as Heat.
        value_fn=lambda d: d.thermostat.system_switch == SYSTEM_SWITCH_EMERGENCY_HEAT,
        set_fn=lambda api, mac, on: api.async_set_system_switch(
            mac, SYSTEM_SWITCH_EMERGENCY_HEAT if on else SYSTEM_SWITCH_HEAT
        ),
        exists_fn=lambda d: SYSTEM_SWITCH_EMERGENCY_HEAT
        in (d.configuration.system_switch_capabilities or []),
    ),
)


@dataclass(frozen=True, kw_only=True)
class ResideoAccessorySwitchEntityDescription(SwitchEntityDescription):
    """A writable per-accessory boolean (a remote room-sensor aggregation flag)."""

    value_fn: Callable[[ResideoAccessory], bool]
    # The ``accessoryValue`` write key this flag drives ("exclude_motion" | "exclude_temp"); the
    # full body is always sent (no per-field merge), changing only this one.
    field: str


ACCESSORY_SWITCHES: tuple[ResideoAccessorySwitchEntityDescription, ...] = (
    ResideoAccessorySwitchEntityDescription(
        key="exclude_motion",
        translation_key="exclude_motion",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda a: a.exclude_motion,
        field="exclude_motion",
    ),
    ResideoAccessorySwitchEntityDescription(
        key="exclude_temperature",
        translation_key="exclude_temperature",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda a: a.exclude_temperature,
        field="exclude_temp",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ResideoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Resideo switches (device settings + per remote room-sensor flags)."""
    coordinator = entry.runtime_data
    entities: list[SwitchEntity] = []
    for mac, data in coordinator.data.items():
        entities.extend(
            ResideoSwitch(coordinator, mac, desc)
            for desc in DEVICE_SWITCHES
            if desc.exists_fn(data)
        )
        for room, accessory in data.rooms.air_sensor_accessories():
            # The full accessoryValue body must echo OccupancySensitivity, so only expose these
            # where the sensor reports it (the remote air sensors).
            if accessory.occupancy_sensitivity is not None:
                entities.extend(
                    ResideoAccessorySwitch(coordinator, mac, room, accessory, desc)
                    for desc in ACCESSORY_SWITCHES
                )
    async_add_entities(entities)


class ResideoSwitch(OptimisticWriteMixin, ResideoEntity, SwitchEntity):
    """A writable device-setting switch (optimistic + reconciled; see ``OptimisticWriteMixin``)."""

    entity_description: ResideoSwitchEntityDescription

    def __init__(
        self,
        coordinator: ResideoDataUpdateCoordinator,
        mac: str,
        description: ResideoSwitchEntityDescription,
    ) -> None:
        super().__init__(coordinator, mac)
        self.entity_description = description
        self._attr_unique_id = f"{mac}_{description.key}"

    def _confirmed_values(self) -> dict[str, Any] | None:
        data = self._device_data
        if data is None:
            return None
        return {"is_on": self.entity_description.value_fn(data)}

    @property
    def is_on(self) -> bool | None:
        if "is_on" in self._optimistic:
            return self._optimistic["is_on"]
        data = self._device_data
        return self.entity_description.value_fn(data) if data else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, on: bool) -> None:
        try:
            await self.entity_description.set_fn(self.coordinator.api, self._mac, on)
        except ResideoError as err:
            raise HomeAssistantError(
                f"Failed to set {self.entity_description.key}: {err}"
            ) from err
        await self._async_post_write({"is_on": on})


class ResideoAccessorySwitch(OptimisticWriteMixin, ResideoAccessoryEntity, SwitchEntity):
    """A remote room-sensor exclude flag (optimistic + reconciled; ~10 s read-back)."""

    entity_description: ResideoAccessorySwitchEntityDescription

    def __init__(
        self,
        coordinator: ResideoDataUpdateCoordinator,
        mac: str,
        room,
        accessory: ResideoAccessory,
        description: ResideoAccessorySwitchEntityDescription,
    ) -> None:
        super().__init__(
            coordinator, mac, room.id, accessory.accessory_id, room.name, accessory.model
        )
        self.entity_description = description
        self._attr_unique_id = (
            f"{mac}_room{room.id}_acc{accessory.accessory_id}_{description.key}"
        )

    def _confirmed_values(self) -> dict[str, Any] | None:
        accessory = self.accessory
        if accessory is None:
            return None
        return {"is_on": self.entity_description.value_fn(accessory)}

    @callback
    def _on_optimistic_cleared(self, key: str) -> None:
        # Also release this flag's shared full-body override (see the coordinator helper).
        self.coordinator.accessory_override(self._mac, self._accessory_id).pop(
            self.entity_description.field, None
        )

    @property
    def is_on(self) -> bool | None:
        if "is_on" in self._optimistic:
            return self._optimistic["is_on"]
        accessory = self.accessory
        return self.entity_description.value_fn(accessory) if accessory else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, on: bool) -> None:
        accessory = self.accessory
        if accessory is None:
            raise HomeAssistantError("Accessory is unavailable")
        # Full-body write composed from the accessory's shared overrides (changes only this flag,
        # echoing the current sensitivity + other exclude; see the coordinator helper).
        try:
            await self.coordinator.async_write_accessory_value(
                self._mac,
                self._accessory_id,
                accessory,
                field=self.entity_description.field,
                value=on,
            )
        except ResideoError as err:
            raise HomeAssistantError(
                f"Failed to set {self.entity_description.key}: {err}"
            ) from err
        await self._async_post_write({"is_on": on})
