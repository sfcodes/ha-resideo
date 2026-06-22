"""aioresideo — async client for the private Resideo consumer API (api.resideo.com).

Public surface:
  - :class:`Resideo`        — high-level facade returning typed models (use this from an app).
  - :class:`ResideoAuth`    — Auth0 login / refresh (email/password -> tokens).
  - :class:`ResideoClient`  — low-level authenticated HTTP client (raw dicts).
  - object models + exceptions.
"""

from __future__ import annotations

from typing import Any

import aiohttp

from .auth import ResideoAuth, decode_jwt_claims
from .client import ResideoClient, TokenUpdatedCallback
from .exceptions import (
    ResideoApiError,
    ResideoAuthError,
    ResideoConnectionError,
    ResideoError,
)
from .merge import LIVE_FEED_MERGED_PROPERTIES, apply_live_feed
from .objects import (
    ResideoAccessory,
    ResideoAccountDevice,
    ResideoBaseObject,
    ResideoChangeConfirm,
    ResideoConfiguration,
    ResideoEvent,
    ResideoLiveFeed,
    ResideoPriority,
    ResideoRoom,
    ResideoRooms,
    ResideoThermostat,
    parse_event,
)
from .stream import ResideoStream

__version__ = "0.1.0"


class Resideo:
    """High-level facade: device discovery + thermostat read/write returning typed models."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        refresh_token: str | None = None,
        *,
        access_token: str | None = None,
        expires_at: float | None = None,
        token_updated_cb: TokenUpdatedCallback | None = None,
    ) -> None:
        self.client = ResideoClient(
            session,
            refresh_token,
            access_token=access_token,
            expires_at=expires_at,
            token_updated_cb=token_updated_cb,
        )

    @property
    def refresh_token(self) -> str | None:
        return self.client.refresh_token

    @property
    def tokens(self) -> dict[str, Any]:
        return self.client.tokens

    # -- discovery ------------------------------------------------------------
    async def async_get_devices(self) -> list[ResideoAccountDevice]:
        """All devices in the consumer account graph."""
        accounts = await self.client.get_accounts()
        return [ResideoAccountDevice(d) for d in ResideoClient.iter_devices(accounts)]

    async def async_get_thermostats(self) -> list[ResideoAccountDevice]:
        """Only the thermostats from the account graph."""
        return [d for d in await self.async_get_devices() if d.is_thermostat]

    # -- reads ----------------------------------------------------------------
    async def async_get_device(self, mac: str) -> ResideoThermostat:
        return ResideoThermostat(await self.client.get_device(mac))

    async def async_get_configuration(self, mac: str) -> ResideoConfiguration:
        return ResideoConfiguration(await self.client.get_configuration(mac))

    async def async_get_priority(self, mac: str) -> ResideoPriority:
        return ResideoPriority(await self.client.get_priority(mac))

    async def async_get_rooms(self, mac: str) -> ResideoRooms:
        """Rooms + per-accessory live values (remote sensors), from /group/0/rooms."""
        return ResideoRooms(await self.client.get_rooms(mac))

    # -- real-time push (SignalR; see resideo-api-spec.md §9) -----------------
    async def async_get_signalr_targets(self) -> list[dict[str, Any]]:
        """Per-location SignalR targets ``[{node_id, name, device_ids}]`` (one stream each)."""
        accounts = await self.client.get_accounts()
        return ResideoClient.iter_locations(accounts)

    def create_stream(
        self,
        location_node_id: str,
        device_ids: list[str],
        on_event: Any,
        **callbacks: Any,
    ) -> ResideoStream:
        """Build a :class:`ResideoStream` for one location (caller drives ``async_run``/``async_stop``)."""
        return ResideoStream(self.client, location_node_id, device_ids, on_event, **callbacks)

    # -- writes (passthrough to the client; see spec §4) ----------------------
    async def async_set_cool_setpoint(self, mac: str, value: float, **kw: Any) -> dict[str, Any]:
        return await self.client.set_cool_setpoint(mac, value, **kw)

    async def async_set_heat_setpoint(self, mac: str, value: float, **kw: Any) -> dict[str, Any]:
        return await self.client.set_heat_setpoint(mac, value, **kw)

    async def async_set_system_switch(self, mac: str, mode: str) -> dict[str, Any]:
        return await self.client.set_system_switch(mac, mode)

    async def async_set_fan(self, mac: str, position: str, speed: int = 1) -> dict[str, Any]:
        return await self.client.set_fan(mac, position, speed)

    async def async_set_hold(self, mac: str, status: str) -> dict[str, Any]:
        return await self.client.set_hold(mac, status)

    async def async_set_priority(
        self, mac: str, priority_type: str, rooms: list[int], **kw: Any
    ) -> dict[str, Any]:
        return await self.client.set_priority(mac, priority_type, rooms, **kw)

    async def async_set_feels_like(self, mac: str, enabled: bool) -> dict[str, Any]:
        return await self.client.set_feels_like(mac, enabled)

    async def async_set_adaptive_recovery(self, mac: str, mode: str) -> dict[str, Any]:
        return await self.client.set_adaptive_recovery(mac, mode)

    async def async_set_schedule_enabled(self, mac: str, enabled: bool) -> dict[str, Any]:
        return await self.client.set_schedule_enabled(mac, enabled)

    async def async_set_freeze_protection(
        self, mac: str, low_limit_degrees: float
    ) -> dict[str, Any]:
        return await self.client.set_freeze_protection(mac, low_limit_degrees)

    async def async_set_setpoint_capabilities(
        self,
        mac: str,
        *,
        heat_min: float | None = None,
        heat_max: float | None = None,
        cool_min: float | None = None,
        cool_max: float | None = None,
    ) -> dict[str, Any]:
        return await self.client.set_setpoint_capabilities(
            mac, heat_min=heat_min, heat_max=heat_max, cool_min=cool_min, cool_max=cool_max
        )

    async def async_set_accessory_value(
        self,
        mac: str,
        accessory_id: int,
        *,
        sensitivity: str,
        exclude_motion: bool,
        exclude_temp: bool,
    ) -> dict[str, Any]:
        return await self.client.set_accessory_value(
            mac,
            accessory_id,
            sensitivity=sensitivity,
            exclude_motion=exclude_motion,
            exclude_temp=exclude_temp,
        )


__all__ = [
    "LIVE_FEED_MERGED_PROPERTIES",
    "Resideo",
    "ResideoAccessory",
    "ResideoAccountDevice",
    "ResideoApiError",
    "ResideoAuth",
    "ResideoAuthError",
    "ResideoBaseObject",
    "ResideoChangeConfirm",
    "ResideoClient",
    "ResideoConfiguration",
    "ResideoConnectionError",
    "ResideoError",
    "ResideoEvent",
    "ResideoLiveFeed",
    "ResideoPriority",
    "ResideoRoom",
    "ResideoRooms",
    "ResideoStream",
    "ResideoThermostat",
    "TokenUpdatedCallback",
    "__version__",
    "apply_live_feed",
    "decode_jwt_claims",
    "parse_event",
]
