"""Valve platform for the Resideo integration — the L5 / L7 water shutoff valve.

State comes from ``GET .../shutOffValves/{mac}/info`` at setup and on every (re)connect, then the
push stream keeps it live (``ActuatorValve`` events carry the position). Open/close is
``PUT .../shutOffValves/{mac}/valve``. The valve takes ~10 s to move, so a command shows
``opening``/``closing`` immediately and the stream then reports the final position; a fallback
re-read ``RECONCILE_DELAY`` seconds later covers a dropped stream.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.valve import (
    ValveDeviceClass,
    ValveEntity,
    ValveEntityFeature,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .aioresideo.const import (
    VALVE_STATE_CLOSED,
    VALVE_STATE_CLOSING,
    VALVE_STATE_OPEN,
    VALVE_STATE_OPENING,
)
from .aioresideo.exceptions import ResideoError
from .coordinator import ResideoConfigEntry, ResideoDataUpdateCoordinator
from .entity import ResideoValveEntity

# Commands: serialize service calls (reads are pure push).
PARALLEL_UPDATES = 1

# The valve needs ~10 s to move; if the stream hasn't confirmed the final position by then
# plus a margin, re-read ``/info`` so the entity converges to the device's reported truth.
RECONCILE_DELAY = 30  # seconds


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ResideoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create a valve entity per discovered shutoff valve."""
    coordinator = entry.runtime_data
    async_add_entities(ResideoShutoffValveEntity(coordinator, mac) for mac in coordinator.valves)


class ResideoShutoffValveEntity(ResideoValveEntity, ValveEntity):
    """The water shutoff valve (open / closed / opening / closing)."""

    _attr_name = None  # the entity *is* the device
    _attr_device_class = ValveDeviceClass.WATER
    _attr_supported_features = ValveEntityFeature.OPEN | ValveEntityFeature.CLOSE
    _attr_reports_position = False

    def __init__(self, coordinator: ResideoDataUpdateCoordinator, mac: str) -> None:
        super().__init__(coordinator, mac)
        self._attr_unique_id = f"{mac}_valve"
        self._reconcile_unsub: CALLBACK_TYPE | None = None

    @property
    def is_opening(self) -> bool | None:
        valve = self.valve
        return valve.valve_state == VALVE_STATE_OPENING if valve else None

    @property
    def is_closing(self) -> bool | None:
        valve = self.valve
        return valve.valve_state == VALVE_STATE_CLOSING if valve else None

    @property
    def is_closed(self) -> bool | None:
        """While moving, report the state it is leaving *toward* (opening counts as closed)."""
        valve = self.valve
        if valve is None:
            return None
        state = valve.valve_state
        if state in (VALVE_STATE_CLOSED, VALVE_STATE_OPENING):
            return True
        if state in (VALVE_STATE_OPEN, VALVE_STATE_CLOSING):
            return False
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        valve = self.valve
        if valve is None:
            return None
        return {"last_command_source": valve.command_source}

    async def async_open_valve(self) -> None:
        await self._async_move(open_valve=True)

    async def async_close_valve(self) -> None:
        await self._async_move(open_valve=False)

    async def _async_move(self, *, open_valve: bool) -> None:
        try:
            await self.coordinator.async_set_valve(self._mac, open_valve)
        except ResideoError as err:
            verb = "open" if open_valve else "close"
            raise HomeAssistantError(f"Could not {verb} the valve: {err}") from err
        self._schedule_reconcile()

    def _schedule_reconcile(self) -> None:
        """Re-read ``/info`` once the valve has had time to finish moving."""
        if self._reconcile_unsub is not None:
            self._reconcile_unsub()

        async def _reconcile(_now: Any) -> None:
            self._reconcile_unsub = None
            await self.coordinator.async_refresh()

        self._reconcile_unsub = async_call_later(self.hass, RECONCILE_DELAY, _reconcile)

    async def async_will_remove_from_hass(self) -> None:
        """Cancel a pending reconcile so it can't fire after the entity is gone."""
        if self._reconcile_unsub is not None:
            self._reconcile_unsub()
            self._reconcile_unsub = None
        await super().async_will_remove_from_hass()
