"""Typed data models wrapping Resideo consumer-API JSON responses."""

from __future__ import annotations

from .account import ResideoAccountDevice, ResideoLocation
from .base import ResideoBaseObject
from .configuration import ResideoConfiguration
from .device import ResideoThermostat, outdoor_temperature_unit
from .events import (
    ResideoChangeConfirm,
    ResideoEvent,
    ResideoLiveFeed,
    ResideoValveStatus,
    ResideoWaterFlow,
    parse_event,
)
from .priority import ResideoPriority
from .rooms import ResideoAccessory, ResideoRoom, ResideoRooms
from .valve import ResideoShutoffValve, normalize_valve_state

__all__ = [
    "ResideoAccessory",
    "ResideoAccountDevice",
    "ResideoBaseObject",
    "ResideoChangeConfirm",
    "ResideoConfiguration",
    "ResideoEvent",
    "ResideoLiveFeed",
    "ResideoLocation",
    "ResideoPriority",
    "ResideoRoom",
    "ResideoRooms",
    "ResideoShutoffValve",
    "ResideoThermostat",
    "ResideoValveStatus",
    "ResideoWaterFlow",
    "normalize_valve_state",
    "outdoor_temperature_unit",
    "parse_event",
]
