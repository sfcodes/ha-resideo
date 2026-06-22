"""Typed data models wrapping Resideo consumer-API JSON responses."""

from __future__ import annotations

from .account import ResideoAccountDevice
from .base import ResideoBaseObject
from .configuration import ResideoConfiguration
from .device import ResideoThermostat
from .events import ResideoChangeConfirm, ResideoEvent, ResideoLiveFeed, parse_event
from .priority import ResideoPriority
from .rooms import ResideoAccessory, ResideoRoom, ResideoRooms

__all__ = [
    "ResideoAccessory",
    "ResideoAccountDevice",
    "ResideoBaseObject",
    "ResideoChangeConfirm",
    "ResideoConfiguration",
    "ResideoEvent",
    "ResideoLiveFeed",
    "ResideoPriority",
    "ResideoRoom",
    "ResideoRooms",
    "ResideoThermostat",
    "parse_event",
]
