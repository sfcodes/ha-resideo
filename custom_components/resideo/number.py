"""Number platform for the Resideo integration — writable numeric device settings.

- **Freeze Protection** low-temperature floor (``freezeProtection`` → ``/configuration``
  ``Reported.FreezeProtection.LowLimitDegrees``).
- **Setpoint Limits** — per-mode heat/cool floor & ceiling (``setPointCapabilities`` →
  ``/configuration`` ``Reported.{Maximum,Minimum}{Heat,Cool}SetpointAllowed``).

Writes are **optimistic + reconciled**, mirroring the climate/switch entities.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .aioresideo import Resideo, ResideoConfiguration
from .aioresideo.exceptions import ResideoError
from .coordinator import (
    ResideoConfigEntry,
    ResideoDataUpdateCoordinator,
    ResideoDeviceData,
)
from .entity import ResideoEntity

RECONCILE_DELAY = 10  # seconds — mirror climate.py


@dataclass(frozen=True, kw_only=True)
class ResideoNumberEntityDescription(NumberEntityDescription):
    """A writable numeric device setting."""

    value_fn: Callable[[ResideoDeviceData], float | None]
    set_fn: Callable[[Resideo, str, float], Awaitable[Any]]
    exists_fn: Callable[[ResideoDeviceData], bool] = lambda _data: True


DEVICE_NUMBERS: tuple[ResideoNumberEntityDescription, ...] = (
    ResideoNumberEntityDescription(
        key="freeze_protection_low_limit",
        translation_key="freeze_protection_low_limit",
        entity_category=EntityCategory.CONFIG,
        device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        native_min_value=35,
        native_max_value=45,
        native_step=1,
        mode=NumberMode.BOX,
        value_fn=lambda d: d.configuration.freeze_protection_low_limit,
        set_fn=lambda api, mac, v: api.async_set_freeze_protection(mac, v),
        exists_fn=lambda d: d.configuration.freeze_protection_configured,
    ),
)


@dataclass(frozen=True, kw_only=True)
class ResideoSetpointLimitNumberEntityDescription(NumberEntityDescription):
    """A Setpoint-Limit floor/ceiling.

    ``setPointCapabilities`` needs the **full four-field body** (a partial body 202s but is silently
    ignored), so these route through the coordinator's shared per-MAC overrides — the same full-body
    compose the accessory excludes use — instead of a direct per-field API call.

    The device also enforces ``minHeat <= minCool`` and ``maxHeat <= maxCool`` (the heat band stays at
    or below the cool band) and silently drops a violating write. Each control names its counterpart
    limit (``bound_field``) and whether that counterpart is this value's upper bound
    (``bound_is_upper`` — ``True`` => value must be ``<=`` counterpart; ``False`` => value ``>=``).
    """

    value_fn: Callable[[ResideoDeviceData], float | None]
    field: str  # coordinator kwarg: "heat_min" / "heat_max" / "cool_min" / "cool_max"
    bound_field: str  # the counterpart limit this value is constrained against
    bound_is_upper: bool
    exists_fn: Callable[[ResideoDeviceData], bool] = lambda _data: True


def _limit_desc(
    key: str,
    field: str,
    bound_field: str,
    bound_is_upper: bool,
    value_fn: Callable[[ResideoDeviceData], float | None],
) -> ResideoSetpointLimitNumberEntityDescription:
    return ResideoSetpointLimitNumberEntityDescription(
        key=key,
        translation_key=key,
        field=field,
        bound_field=bound_field,
        bound_is_upper=bound_is_upper,
        entity_category=EntityCategory.CONFIG,
        device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        native_min_value=50,
        native_max_value=90,
        native_step=1,
        mode=NumberMode.BOX,
        value_fn=value_fn,
        exists_fn=lambda d: value_fn(d) is not None,
    )


# Heat band must stay at/below the cool band: minHeat <= minCool, maxHeat <= maxCool.
SETPOINT_LIMIT_NUMBERS: tuple[ResideoSetpointLimitNumberEntityDescription, ...] = (
    _limit_desc("heat_setpoint_min", "heat_min", "cool_min", True, lambda d: d.configuration.min_heat_setpoint),
    _limit_desc("heat_setpoint_max", "heat_max", "cool_max", True, lambda d: d.configuration.max_heat_setpoint),
    _limit_desc("cool_setpoint_min", "cool_min", "heat_min", False, lambda d: d.configuration.min_cool_setpoint),
    _limit_desc("cool_setpoint_max", "cool_max", "heat_max", False, lambda d: d.configuration.max_cool_setpoint),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ResideoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Resideo numbers."""
    coordinator = entry.runtime_data
    entities: list[NumberEntity] = []
    for mac, data in coordinator.data.items():
        entities.extend(
            ResideoNumber(coordinator, mac, desc)
            for desc in DEVICE_NUMBERS
            if desc.exists_fn(data)
        )
        entities.extend(
            ResideoSetpointLimitNumber(coordinator, mac, desc)
            for desc in SETPOINT_LIMIT_NUMBERS
            if desc.exists_fn(data)
        )
    async_add_entities(entities)


class ResideoNumber(ResideoEntity, NumberEntity):
    """A writable numeric device-setting (optimistic + reconciled like the climate entity)."""

    entity_description: ResideoNumberEntityDescription

    def __init__(
        self,
        coordinator: ResideoDataUpdateCoordinator,
        mac: str,
        description: ResideoNumberEntityDescription,
    ) -> None:
        super().__init__(coordinator, mac)
        self.entity_description = description
        self._attr_unique_id = f"{mac}_{description.key}"
        self._optimistic: float | None = None
        self._reconcile_unsub: CALLBACK_TYPE | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._optimistic is not None:
            data = self._device_data
            if data is not None and self.entity_description.value_fn(data) == self._optimistic:
                self._optimistic = None
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> float | None:
        if self._optimistic is not None:
            return self._optimistic
        data = self._device_data
        return self.entity_description.value_fn(data) if data else None

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self.entity_description.set_fn(self.coordinator.api, self._mac, value)
        except ResideoError as err:
            raise HomeAssistantError(
                f"Failed to set {self.entity_description.key}: {err}"
            ) from err
        self._optimistic = value
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


class ResideoSetpointLimitNumber(ResideoEntity, NumberEntity):
    """A Setpoint-Limit floor/ceiling (optimistic + reconciled, ~2 s read-back).

    Writes go through the coordinator, which composes the full four-field ``setPointCapabilities``
    body from shared per-MAC overrides — so a second limit edit inside the read-back window can't
    clobber this one (the same race the accessory excludes guard against).
    """

    entity_description: ResideoSetpointLimitNumberEntityDescription

    def __init__(
        self,
        coordinator: ResideoDataUpdateCoordinator,
        mac: str,
        description: ResideoSetpointLimitNumberEntityDescription,
    ) -> None:
        super().__init__(coordinator, mac)
        self.entity_description = description
        self._attr_unique_id = f"{mac}_{description.key}"
        self._optimistic: float | None = None
        self._reconcile_unsub: CALLBACK_TYPE | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._optimistic is not None:
            data = self._device_data
            if data is not None and self.entity_description.value_fn(data) == self._optimistic:
                self._clear_optimistic()
        super()._handle_coordinator_update()

    @callback
    def _clear_optimistic(self) -> None:
        self._optimistic = None
        self.coordinator.setpoint_limit_override(self._mac).pop(
            self.entity_description.field, None
        )

    @property
    def native_value(self) -> float | None:
        if self._optimistic is not None:
            return self._optimistic
        data = self._device_data
        return self.entity_description.value_fn(data) if data else None

    def _effective_limit(self, configuration: ResideoConfiguration, field: str) -> float | None:
        """The counterpart limit's pending (override) value, else its current config value.

        Override-aware so a just-issued, not-yet-confirmed edit to the counterpart is honored.
        """
        band = {
            "heat_min": configuration.min_heat_setpoint,
            "heat_max": configuration.max_heat_setpoint,
            "cool_min": configuration.min_cool_setpoint,
            "cool_max": configuration.max_cool_setpoint,
        }
        override = self.coordinator.setpoint_limit_override(self._mac)
        return override.get(field, band[field])

    async def async_set_native_value(self, value: float) -> None:
        configuration = self.configuration
        if configuration is None:
            raise HomeAssistantError("Device configuration is unavailable")
        desc = self.entity_description
        # The device silently drops a write that breaks heat<=cool ordering; reject it up front.
        bound = self._effective_limit(configuration, desc.bound_field)
        if bound is not None:
            if desc.bound_is_upper and value > bound:
                raise ServiceValidationError(
                    f"Can't set {desc.key.replace('_', ' ')} to {value:g} °F: the heat band must "
                    f"stay at or below the cool band ({bound:g} °F). Raise the cool limit first."
                )
            if not desc.bound_is_upper and value < bound:
                raise ServiceValidationError(
                    f"Can't set {desc.key.replace('_', ' ')} to {value:g} °F: the cool band must "
                    f"stay at or above the heat band ({bound:g} °F). Lower the heat limit first."
                )
        try:
            await self.coordinator.async_write_setpoint_limit(
                self._mac, configuration, field=desc.field, value=value
            )
        except ResideoError as err:
            raise HomeAssistantError(
                f"Failed to set {desc.key}: {err}"
            ) from err
        self._optimistic = value
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
