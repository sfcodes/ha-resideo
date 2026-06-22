"""Climate platform for the Resideo integration — the thermostat entity.

Maps the consumer-API device shadow to a Home Assistant ``climate`` entity per
``resideo-api-spec.md`` §7: capability-driven modes/limits, single-setpoint Heat/Cool, an Auto
heat/cool **range** (``target_temperature_low``/``high``), fan modes, and **hold presets**
(``SetpointStatus`` via ``PUT hold``). EmergencyHeat has no HA HVAC mode, so it reads as Heat and
is toggled by a separate ``switch`` (see ``switch.py``).

Writes are optimistic: the consumer API is asynchronous (``202`` means *accepted*, not
*applied*) and the device shadow lags a few seconds, so a successful write is reflected in
the UI immediately and reconciled against the next poll (see ``_async_post_write``).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.climate.const import FAN_AUTO, FAN_ON, PRESET_NONE
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .aioresideo.const import (
    FAN_AUTO as RES_FAN_AUTO,
)
from .aioresideo.const import (
    FAN_CIRCULATE as RES_FAN_CIRCULATE,
)
from .aioresideo.const import (
    FAN_ON as RES_FAN_ON,
)
from .aioresideo.const import (
    SETPOINT_HOLD_UNTIL,
    SETPOINT_NO_HOLD,
    SETPOINT_PERMANENT_HOLD,
    SETPOINT_TEMPORARY_HOLD,
    SETPOINT_VACATION_HOLD,
    SYSTEM_SWITCH_AUTO,
    SYSTEM_SWITCH_COOL,
    SYSTEM_SWITCH_EMERGENCY_HEAT,
    SYSTEM_SWITCH_HEAT,
    SYSTEM_SWITCH_OFF,
)
from .aioresideo.exceptions import ResideoError
from .coordinator import ResideoConfigEntry, ResideoDataUpdateCoordinator
from .entity import ResideoEntity

# Resideo SystemSwitch <-> HA HVACMode. TODO: EmergencyHeat has no direct HA mode.
RESIDEO_TO_HVAC: dict[str, HVACMode] = {
    SYSTEM_SWITCH_OFF: HVACMode.OFF,
    SYSTEM_SWITCH_HEAT: HVACMode.HEAT,
    SYSTEM_SWITCH_COOL: HVACMode.COOL,
    SYSTEM_SWITCH_AUTO: HVACMode.HEAT_COOL,
}
HVAC_TO_RESIDEO: dict[HVACMode, str] = {v: k for k, v in RESIDEO_TO_HVAC.items()}

# Reported.OperationStatus.Mode -> HA HVACAction (current activity).
RESIDEO_TO_ACTION: dict[str, HVACAction] = {
    "EquipmentOff": HVACAction.IDLE,
    "Off": HVACAction.OFF,
    "Heat": HVACAction.HEATING,
    "Cool": HVACAction.COOLING,
}

# Hold presets <-> Reported.Setpoint.SetpointStatus (spec §4/§7). Reads cover every status;
# only NoHold/PermanentHold/TemporaryHold are settable via `PUT hold {Status}` (all 202-verified).
# VacationHold needs start/end dates + setpoints and HoldUntil needs an until-time, neither of which
# a plain preset can supply — they are shown read-only and rejected on set.
PRESET_PERMANENT_HOLD = "permanent_hold"
PRESET_TEMPORARY_HOLD = "temporary_hold"
PRESET_HOLD_UNTIL = "hold_until"
PRESET_VACATION = "vacation"

RESIDEO_STATUS_TO_PRESET: dict[str, str] = {
    SETPOINT_NO_HOLD: PRESET_NONE,
    SETPOINT_PERMANENT_HOLD: PRESET_PERMANENT_HOLD,
    SETPOINT_TEMPORARY_HOLD: PRESET_TEMPORARY_HOLD,
    SETPOINT_HOLD_UNTIL: PRESET_HOLD_UNTIL,
    SETPOINT_VACATION_HOLD: PRESET_VACATION,
}
PRESET_TO_HOLD_STATUS: dict[str, str] = {
    PRESET_NONE: SETPOINT_NO_HOLD,
    PRESET_PERMANENT_HOLD: SETPOINT_PERMANENT_HOLD,
    PRESET_TEMPORARY_HOLD: SETPOINT_TEMPORARY_HOLD,
}
_SETTABLE_PRESETS: tuple[str, ...] = (PRESET_NONE, PRESET_PERMANENT_HOLD, PRESET_TEMPORARY_HOLD)

# HA has a fixed vocabulary of fan-mode constants (auto/on/off/low/.../diffuse) with no
# "circulate". fan_modes accepts arbitrary strings, though, so expose the Resideo label
# verbatim — the app shows "Circulate" — rather than mislabelling it as HA's "Diffuse".
FAN_CIRCULATE = "Circulate"

HA_TO_RESIDEO_FAN: dict[str, str] = {
    FAN_AUTO: RES_FAN_AUTO,
    FAN_ON: RES_FAN_ON,
    FAN_CIRCULATE: RES_FAN_CIRCULATE,
}
RESIDEO_TO_HA_FAN: dict[str, str] = {v: k for k, v in HA_TO_RESIDEO_FAN.items()}

# Display fan modes in the Resideo app's order (Auto, Circulate, On) rather than the
# device's reported Positions order (On, Auto, Circulate), for parity with the app.
_FAN_MODE_ORDER = [FAN_AUTO, FAN_CIRCULATE, FAN_ON]

# After a write, force a confirming poll this many seconds later — long enough for the
# device to apply the command and the shadow to reflect it (~8s observed live).
RECONCILE_DELAY = 10  # seconds


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ResideoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Resideo climate entities."""
    coordinator = entry.runtime_data
    async_add_entities(ResideoClimate(coordinator, mac) for mac in coordinator.data)


class ResideoClimate(ResideoEntity, ClimateEntity):
    """A Resideo thermostat as an HA climate entity."""

    _attr_name = None  # use the device's own name
    _attr_translation_key = "thermostat"  # for preset_mode state-attribute translations
    # Fallbacks when /configuration is unavailable. Normally hvac_modes / fan_modes /
    # temperature_unit / min_temp / max_temp are derived live from device capabilities below.
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL]
    _attr_fan_modes = _FAN_MODE_ORDER

    def __init__(
        self, coordinator: ResideoDataUpdateCoordinator, mac: str
    ) -> None:
        super().__init__(coordinator, mac)
        self._attr_unique_id = f"{mac}_climate"
        # Optimistic overrides keyed by facet ("hvac_mode"/"fan_mode"/"cool_setpoint"/
        # "heat_setpoint"); set on a successful write, cleared once a poll confirms them.
        self._optimistic: dict[str, Any] = {}
        self._reconcile_unsub: CALLBACK_TYPE | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        # Drop optimistic values the freshly polled shadow now confirms, so the UI stops
        # overriding once the device has caught up (and never flickers on a stale poll).
        device = self.device
        if device is not None and self._optimistic:
            hvac = (
                HVACMode.HEAT
                if device.system_switch == SYSTEM_SWITCH_EMERGENCY_HEAT
                else RESIDEO_TO_HVAC.get(device.system_switch)
            )
            confirmed = {
                "hvac_mode": hvac,
                "fan_mode": (
                    RESIDEO_TO_HA_FAN.get(device.fan_position)
                    if device.fan_position is not None
                    else None
                ),
                "cool_setpoint": device.cool_setpoint,
                "heat_setpoint": device.heat_setpoint,
                "preset": RESIDEO_STATUS_TO_PRESET.get(device.setpoint_status),
            }
            for key, value in confirmed.items():
                if key in self._optimistic and self._optimistic[key] == value:
                    del self._optimistic[key]
        super()._handle_coordinator_update()

    @property
    def supported_features(self) -> ClimateEntityFeature:
        # In Auto (heat_cool) the user sets a heat/cool range; in Heat/Cool a single setpoint.
        features = ClimateEntityFeature.FAN_MODE
        if self.hvac_mode == HVACMode.HEAT_COOL:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        else:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        # Only expose the hold-preset control when there's an actual choice. With the schedule off
        # the sole reachable status is PermanentHold, so a single-option selector is hidden.
        if len(self.preset_modes) > 1:
            features |= ClimateEntityFeature.PRESET_MODE
        return features

    # --- capabilities derived from /configuration (with safe fallbacks) ------
    @property
    def temperature_unit(self) -> str:
        config = self.configuration
        if config and config.temperature_units == "C":
            return UnitOfTemperature.CELSIUS
        return UnitOfTemperature.FAHRENHEIT

    @property
    def hvac_modes(self) -> list[HVACMode]:
        config = self.configuration
        caps = config.system_switch_capabilities if config else []
        present = {RESIDEO_TO_HVAC[c] for c in caps if c in RESIDEO_TO_HVAC}
        if not present:
            return self._attr_hvac_modes
        # Stable display order; OFF is always offered.
        ordered = [
            m
            for m in (HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL)
            if m in present
        ]
        if HVACMode.OFF not in ordered:
            ordered.insert(0, HVACMode.OFF)
        return ordered

    @property
    def fan_modes(self) -> list[str]:
        config = self.configuration
        positions = config.fan_positions if config else []
        modes = [RESIDEO_TO_HA_FAN[p] for p in positions if p in RESIDEO_TO_HA_FAN]
        if not modes:
            return self._attr_fan_modes
        rank = {m: i for i, m in enumerate(_FAN_MODE_ORDER)}
        return sorted(modes, key=lambda m: rank.get(m, len(rank)))

    @property
    def min_temp(self) -> float:
        config = self.configuration
        if config:
            mins = [
                v
                for v in (config.min_heat_setpoint, config.min_cool_setpoint)
                if v is not None
            ]
            if mins:
                return min(mins)
        return super().min_temp

    @property
    def max_temp(self) -> float:
        config = self.configuration
        if config:
            maxs = [
                v
                for v in (config.max_heat_setpoint, config.max_cool_setpoint)
                if v is not None
            ]
            if maxs:
                return max(maxs)
        return super().max_temp

    @property
    def current_temperature(self) -> float | None:
        return self.device.indoor_temperature if self.device else None

    @property
    def current_humidity(self) -> int | None:
        return self.device.indoor_humidity if self.device else None

    @property
    def hvac_mode(self) -> HVACMode | None:
        if "hvac_mode" in self._optimistic:
            return self._optimistic["hvac_mode"]
        if not self.device:
            return None
        # EmergencyHeat has no HA HVAC mode -> show Heat (the emergency_heat switch carries the
        # distinction and is how it's toggled).
        if self.device.system_switch == SYSTEM_SWITCH_EMERGENCY_HEAT:
            return HVACMode.HEAT
        return RESIDEO_TO_HVAC.get(self.device.system_switch)

    @property
    def hvac_action(self) -> HVACAction | None:
        if not self.device:
            return None
        return RESIDEO_TO_ACTION.get(self.device.operation_mode)

    @property
    def target_temperature(self) -> float | None:
        device = self.device
        if not device:
            return None
        if self.hvac_mode == HVACMode.COOL:
            return self._optimistic.get("cool_setpoint", device.cool_setpoint)
        if self.hvac_mode == HVACMode.HEAT:
            return self._optimistic.get("heat_setpoint", device.heat_setpoint)
        return None  # HEAT_COOL uses target_temperature_low/high

    @property
    def target_temperature_low(self) -> float | None:
        device = self.device
        if not device or self.hvac_mode != HVACMode.HEAT_COOL:
            return None
        return self._optimistic.get("heat_setpoint", device.heat_setpoint)

    @property
    def target_temperature_high(self) -> float | None:
        device = self.device
        if not device or self.hvac_mode != HVACMode.HEAT_COOL:
            return None
        return self._optimistic.get("cool_setpoint", device.cool_setpoint)

    @property
    def preset_mode(self) -> str | None:
        if "preset" in self._optimistic:
            return self._optimistic["preset"]
        device = self.device
        if not device:
            return None
        return RESIDEO_STATUS_TO_PRESET.get(device.setpoint_status)

    @property
    def preset_modes(self) -> list[str]:
        # Holds only matter with a schedule to return to. With the schedule OFF the device stays
        # in PermanentHold — NoHold/TemporaryHold are 202-accepted but don't apply (verified live)
        # — so offer only the current (read-only) preset then. With a schedule on, expose the
        # settable holds. The current status is always included so HA never warns on display
        # (e.g. vacation / hold-until, which are read-only).
        device = self.device
        modes = list(_SETTABLE_PRESETS) if device and device.schedule_enabled else [
            PRESET_PERMANENT_HOLD
        ]
        current = self.preset_mode
        if current and current not in modes:
            modes.append(current)
        return modes

    @property
    def fan_mode(self) -> str | None:
        if "fan_mode" in self._optimistic:
            return self._optimistic["fan_mode"]
        if not self.device or self.device.fan_position is None:
            return None
        return RESIDEO_TO_HA_FAN.get(self.device.fan_position)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set a single target (Heat/Cool) or a heat/cool range (Auto)."""
        if self.hvac_mode == HVACMode.HEAT_COOL:
            await self._async_set_range(
                kwargs.get(ATTR_TARGET_TEMP_LOW), kwargs.get(ATTR_TARGET_TEMP_HIGH)
            )
            return
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        mode = self.hvac_mode
        if mode == HVACMode.COOL:
            key = "cool_setpoint"
            call = self.coordinator.api.async_set_cool_setpoint
        elif mode == HVACMode.HEAT:
            key = "heat_setpoint"
            call = self.coordinator.api.async_set_heat_setpoint
        else:
            return
        try:
            await call(self._mac, temperature)
        except ResideoError as err:
            raise HomeAssistantError(f"Failed to set temperature: {err}") from err
        await self._async_post_write({key: temperature})

    async def _async_set_range(
        self, low: float | None, high: float | None
    ) -> None:
        """Write the Auto-mode heat (low) and cool (high) setpoints."""
        if low is None and high is None:
            return
        optimistic: dict[str, Any] = {}
        try:
            # Cool first, then heat — both are independent PUTs (each 202-verified).
            if high is not None:
                await self.coordinator.api.async_set_cool_setpoint(self._mac, high)
                optimistic["cool_setpoint"] = high
            if low is not None:
                await self.coordinator.api.async_set_heat_setpoint(self._mac, low)
                optimistic["heat_setpoint"] = low
        except ResideoError as err:
            raise HomeAssistantError(f"Failed to set temperature range: {err}") from err
        await self._async_post_write(optimistic)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set the hold status (PUT hold).

        Vacation/hold-until are read-only (need dates / an end time). NoHold/TemporaryHold need an
        enabled schedule — without one the device just stays in PermanentHold (verified live), so
        reject them rather than issue a silent no-op write.
        """
        status = PRESET_TO_HOLD_STATUS.get(preset_mode)
        if status is None:
            raise ServiceValidationError(
                f"Preset '{preset_mode}' can't be set from Home Assistant — vacation and "
                "hold-until need dates or an end time; set them in the Resideo app."
            )
        device = self.device
        if preset_mode != PRESET_PERMANENT_HOLD and not (device and device.schedule_enabled):
            raise ServiceValidationError(
                f"Preset '{preset_mode}' needs the schedule enabled — with the schedule off the "
                "thermostat stays in a permanent hold."
            )
        try:
            await self.coordinator.api.async_set_hold(self._mac, status)
        except ResideoError as err:
            raise HomeAssistantError(f"Failed to set hold: {err}") from err
        await self._async_post_write({"preset": preset_mode})

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the system mode."""
        mode = HVAC_TO_RESIDEO.get(hvac_mode)
        if mode is None:
            raise ServiceValidationError(f"Unsupported HVAC mode: {hvac_mode}")
        try:
            await self.coordinator.api.async_set_system_switch(self._mac, mode)
        except ResideoError as err:
            raise HomeAssistantError(f"Failed to set HVAC mode: {err}") from err
        await self._async_post_write({"hvac_mode": hvac_mode})

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set the fan mode."""
        position = HA_TO_RESIDEO_FAN.get(fan_mode)
        if position is None:
            raise ServiceValidationError(f"Unsupported fan mode: {fan_mode}")
        try:
            await self.coordinator.api.async_set_fan(self._mac, position)
        except ResideoError as err:
            raise HomeAssistantError(f"Failed to set fan mode: {err}") from err
        await self._async_post_write({"fan_mode": fan_mode})

    # --- optimistic-write plumbing -------------------------------------------
    async def _async_post_write(self, optimistic: dict[str, Any]) -> None:
        """Reflect a just-accepted write immediately, force a refresh, and schedule a
        reconcile poll. The consumer API is async (202 != applied), so the immediate
        refresh may still read the pre-apply shadow; the optimistic value covers that gap.
        """
        self._optimistic.update(optimistic)
        self.async_write_ha_state()
        # Forced (non-debounced) refresh, mirroring the official ``lyric`` integration.
        await self.coordinator.async_refresh()
        self._schedule_reconcile()

    def _schedule_reconcile(self) -> None:
        """Force one more refresh after the device has had time to apply, then drop any
        remaining optimistic values so the UI converges to the device's reported truth."""
        if self._reconcile_unsub is not None:
            self._reconcile_unsub()

        async def _reconcile(_now: Any) -> None:
            self._reconcile_unsub = None
            await self.coordinator.async_refresh()
            if self._optimistic:
                self._optimistic.clear()
                self.async_write_ha_state()

        self._reconcile_unsub = async_call_later(self.hass, RECONCILE_DELAY, _reconcile)

    async def async_will_remove_from_hass(self) -> None:
        """Cancel a pending reconcile so it can't fire after the entity is gone."""
        if self._reconcile_unsub is not None:
            self._reconcile_unsub()
            self._reconcile_unsub = None
        await super().async_will_remove_from_hass()
