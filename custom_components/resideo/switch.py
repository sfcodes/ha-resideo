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
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later

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
from .entity import ResideoAccessoryEntity, ResideoEntity

# After a write, force a confirming refresh this many seconds later (mirrors climate.py).
RECONCILE_DELAY = 10  # seconds


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


class ResideoSwitch(ResideoEntity, SwitchEntity):
    """A writable device-setting switch (optimistic + reconciled like the climate entity)."""

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
        # Optimistic override; set on a successful write, cleared once a refresh confirms it.
        self._optimistic: bool | None = None
        self._reconcile_unsub: CALLBACK_TYPE | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._optimistic is not None:
            data = self._device_data
            if data is not None and self.entity_description.value_fn(data) == self._optimistic:
                self._optimistic = None
        super()._handle_coordinator_update()

    @property
    def is_on(self) -> bool | None:
        if self._optimistic is not None:
            return self._optimistic
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
        await self._async_post_write(on)

    # --- optimistic-write plumbing (mirrors climate.py) ----------------------
    async def _async_post_write(self, optimistic: bool) -> None:
        self._optimistic = optimistic
        self.async_write_ha_state()
        await self.coordinator.async_refresh()
        self._schedule_reconcile()

    def _schedule_reconcile(self) -> None:
        if self._reconcile_unsub is not None:
            self._reconcile_unsub()

        async def _reconcile(_now: Any) -> None:
            self._reconcile_unsub = None
            await self.coordinator.async_refresh()
            if self._optimistic is not None:
                self._optimistic = None
                self.async_write_ha_state()

        self._reconcile_unsub = async_call_later(self.hass, RECONCILE_DELAY, _reconcile)

    async def async_will_remove_from_hass(self) -> None:
        if self._reconcile_unsub is not None:
            self._reconcile_unsub()
            self._reconcile_unsub = None
        await super().async_will_remove_from_hass()


class ResideoAccessorySwitch(ResideoAccessoryEntity, SwitchEntity):
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
        self._optimistic: bool | None = None
        self._reconcile_unsub: CALLBACK_TYPE | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._optimistic is not None:
            accessory = self.accessory
            if (
                accessory is not None
                and self.entity_description.value_fn(accessory) == self._optimistic
            ):
                self._clear_optimistic()
        super()._handle_coordinator_update()

    @callback
    def _clear_optimistic(self) -> None:
        self._optimistic = None
        self.coordinator.accessory_override(self._mac, self._accessory_id).pop(
            self.entity_description.field, None
        )

    @property
    def is_on(self) -> bool | None:
        if self._optimistic is not None:
            return self._optimistic
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
        await self._async_post_write(on)

    async def _async_post_write(self, optimistic: bool) -> None:
        self._optimistic = optimistic
        self.async_write_ha_state()
        await self.coordinator.async_refresh()
        self._schedule_reconcile()

    def _schedule_reconcile(self) -> None:
        if self._reconcile_unsub is not None:
            self._reconcile_unsub()

        async def _reconcile(_now: Any) -> None:
            self._reconcile_unsub = None
            await self.coordinator.async_refresh()
            if self._optimistic is not None:
                self._clear_optimistic()
                self.async_write_ha_state()

        self._reconcile_unsub = async_call_later(self.hass, RECONCILE_DELAY, _reconcile)

    async def async_will_remove_from_hass(self) -> None:
        if self._reconcile_unsub is not None:
            self._reconcile_unsub()
            self._reconcile_unsub = None
        await super().async_will_remove_from_hass()
