"""Room-priority model (from GET /devsrv/api/v2/device/{mac}/priority).

This endpoint reports the room *priority/selection* (which rooms drive the thermostat). The
live per-accessory sensor *values* live in :mod:`aioresideo.objects.rooms` (``/group/0/rooms``).
See ``resideo-api-spec.md`` §3. Used by the (future) room-priority select entity.
"""

from __future__ import annotations

from typing import Any

from .base import ResideoBaseObject


class ResideoPriority(ResideoBaseObject):
    """Root of the /priority response: ``{PriorityStatus, Priority:{PriorityType, SelectedRooms}}``."""

    @property
    def priority_status(self) -> str | None:
        return self.attributes.get("PriorityStatus")

    @property
    def _priority(self) -> dict[str, Any]:
        return self.attributes.get("Priority", {}) or {}

    @property
    def priority_type(self) -> str | None:
        """``PickARoom`` or ``FollowMe``."""
        return self._priority.get("PriorityType")

    @property
    def selected_rooms(self) -> list[int]:
        return self._priority.get("SelectedRooms", []) or []
