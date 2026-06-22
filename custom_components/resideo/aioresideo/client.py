"""Authenticated HTTP client for the Resideo consumer API (api.resideo.com).

Async (aiohttp) port of the proven client in ``spikes/resideo_consumer.py``. Owns token
lifecycle (refresh on expiry via :class:`~aioresideo.auth.ResideoAuth`) and injects the two
mandatory headers on every call: the bearer token and the Azure APIM subscription key.

Reads return parsed JSON. Writes (``PUT`` commands on the ``devsrv`` service) carry a
``ChannelId`` and return ``202 {"TransactionId": ...}`` — the device applies them within a
few seconds, so callers should re-read state to confirm.
"""

from __future__ import annotations

import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import aiohttp

from .auth import ResideoAuth
from .const import (
    ACCOUNTS_ENDPOINT,
    API_BASE_URL,
    APP_USER_AGENT,
    DEFAULT_CHANNEL_ID,
    DEVSRV_DEVICE,
    OCP_APIM_SUBSCRIPTION_KEY,
    REQUEST_TIMEOUT,
    SETPOINT_PERMANENT_HOLD,
    SIGNALR_NEGOTIATE_URL,
    TOKEN_REFRESH_MARGIN,
)
from .exceptions import ResideoApiError, ResideoAuthError, ResideoConnectionError

# Called with the latest token dict whenever tokens are refreshed/rotated, so the caller
# (e.g. the HA integration) can persist the new refresh token. May be sync or async.
TokenUpdatedCallback = Callable[[dict[str, Any]], Awaitable[None] | None]

_LOGGER = logging.getLogger(__name__)


class ResideoClient:
    """Token-managing, header-injecting client for api.resideo.com."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        refresh_token: str | None = None,
        *,
        access_token: str | None = None,
        expires_at: float | None = None,
        token_updated_cb: TokenUpdatedCallback | None = None,
    ) -> None:
        self._session = session
        self._auth = ResideoAuth(session)
        self._refresh_token = refresh_token
        self._access_token = access_token
        self._expires_at = float(expires_at) if expires_at is not None else 0.0
        self._token_updated_cb = token_updated_cb

    @property
    def session(self) -> aiohttp.ClientSession:
        """The underlying aiohttp session (used by :class:`~aioresideo.stream.ResideoStream`)."""
        return self._session

    # -- tokens ---------------------------------------------------------------
    @property
    def refresh_token(self) -> str | None:
        """The current refresh token (may rotate after a refresh)."""
        return self._refresh_token

    @property
    def tokens(self) -> dict[str, Any]:
        """The current token snapshot (for persistence)."""
        return {
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "expires_at": self._expires_at,
        }

    def _apply_tokens(self, tok: dict[str, Any]) -> None:
        self._access_token = tok.get("access_token")
        if tok.get("refresh_token"):  # Auth0 doesn't always rotate; keep the old one if not
            self._refresh_token = tok["refresh_token"]
        self._expires_at = time.time() + int(tok.get("expires_in", 3600))

    async def _fire_token_updated(self) -> None:
        if self._token_updated_cb is None:
            return
        result = self._token_updated_cb(self.tokens)
        if inspect.isawaitable(result):
            await result

    async def async_ensure_token(self) -> str:
        """Return a valid access token, refreshing it when within the expiry margin."""
        if self._access_token and (self._expires_at - time.time()) > TOKEN_REFRESH_MARGIN:
            return self._access_token
        if not self._refresh_token:
            raise ResideoAuthError("No refresh token available — re-authenticate")
        new = await self._auth.refresh(self._refresh_token)
        self._apply_tokens(new)
        await self._fire_token_updated()
        if not self._access_token:
            raise ResideoAuthError("Refresh succeeded but returned no access_token")
        return self._access_token

    # -- transport ------------------------------------------------------------
    async def _request(self, method: str, endpoint: str, *, _retry: bool = True, **kwargs: Any) -> Any:
        token = await self.async_ensure_token()
        url = endpoint if endpoint.startswith("http") else API_BASE_URL + endpoint
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            # Azure APIM subscription key (prod) — required by the devsrv command service.
            "Ocp-Apim-Subscription-Key": OCP_APIM_SUBSCRIPTION_KEY,
            "User-Agent": APP_USER_AGENT,
        }
        try:
            resp = await self._session.request(
                method,
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                **kwargs,
            )
        except aiohttp.ClientError as err:
            raise ResideoConnectionError(f"{method} {url} failed: {err}") from err
        _LOGGER.debug("%s %s -> HTTP %s", method, url, resp.status)
        try:
            if resp.status == 401 and _retry:
                # Token may be stale; force a refresh and retry exactly once.
                self._expires_at = 0.0
                return await self._request(method, endpoint, _retry=False, **kwargs)
            if resp.status == 401:
                raise ResideoAuthError(f"Unauthorized (401) for {method} {url}")
            if not 200 <= resp.status < 300:
                body = await self._safe_body(resp)
                raise ResideoApiError(
                    f"{method} {url} -> {resp.status}", status=resp.status, body=body
                )
            if resp.status == 204:
                return None
            text = await resp.text()
            if not text:
                return None
            try:
                return json.loads(text)
            except ValueError:
                return text
        finally:
            await resp.release()

    @staticmethod
    async def _safe_body(resp: aiohttp.ClientResponse) -> Any:
        try:
            text = await resp.text()
        except Exception:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return text

    async def get(self, endpoint: str, **kwargs: Any) -> Any:
        """Authenticated GET; returns parsed JSON."""
        return await self._request("GET", endpoint, **kwargs)

    async def put(self, endpoint: str, **kwargs: Any) -> Any:
        """Authenticated PUT; returns parsed JSON (typically ``{"TransactionId": ...}``)."""
        return await self._request("PUT", endpoint, **kwargs)

    # -- reads (all confirmed live; see resideo-api-spec.md §3) ---------------
    async def get_accounts(self) -> dict[str, Any]:
        """The consumer account graph (users -> accounts -> locations -> devices)."""
        return await self.get(ACCOUNTS_ENDPOINT)

    async def get_device(self, mac: str) -> dict[str, Any]:
        """Full device shadow: ``{DeviceId, Reported{...}, Desired{...}}`` (spec §5)."""
        return await self.get(DEVSRV_DEVICE.format(mac=mac))

    async def get_configuration(self, mac: str) -> dict[str, Any]:
        """Device capabilities / allowed value sets (spec §6)."""
        return await self.get(DEVSRV_DEVICE.format(mac=mac) + "/configuration")

    async def get_priority(self, mac: str) -> dict[str, Any]:
        """Room priority + per-room aggregates (spec §3 ``/priority``)."""
        return await self.get(DEVSRV_DEVICE.format(mac=mac) + "/priority")

    async def get_rooms(self, mac: str) -> dict[str, Any]:
        """Rooms + per-accessory sensor values (spec §3 ``/group/0/rooms``)."""
        return await self.get(DEVSRV_DEVICE.format(mac=mac) + "/group/0/rooms")

    # -- real-time push (SignalR; see resideo-api-spec.md §9.2) ---------------
    async def async_signalr_negotiate(self) -> dict[str, str]:
        """Full two-step SignalR negotiate. Returns ``{"wss_url", "access_token"}``.

        Step 1 (titans ``/Hub/negotiate`` via ``_request`` → Auth0 Bearer + APIM key) yields
        the Azure SignalR base url + a short-lived Azure JWT. Step 2 (Azure
        ``/client/negotiate``, authed with that **Azure JWT only**) yields the
        ``connectionToken``. The returned ``wss_url`` already carries ``&id=`` + ``&access_token=``.

        The two-step form is **mandatory**: a token-only connection (skipping
        ``/client/negotiate`` + ``&id=``) still subscribes and receives ``ChangeRequest``
        broadcasts but **never** ``LiveFeedEvent``s.
        """
        hub = await self._request("POST", SIGNALR_NEGOTIATE_URL, data="")
        base_url = (hub or {}).get("url")
        azure_jwt = (hub or {}).get("accessToken")
        if not base_url or not azure_jwt:
            raise ResideoApiError("SignalR negotiate returned no url/accessToken", body=hub)
        # Step 2: insert 'negotiate' before the query string (.../client/?q -> .../client/negotiate?q).
        pre, _, query = base_url.partition("?")
        azure_neg_url = f"{pre}negotiate?{query}" if query else f"{pre}negotiate"
        conn = await self._azure_client_negotiate(azure_neg_url, azure_jwt)
        conn_token = conn.get("connectionToken") or conn.get("connectionId")
        if not conn_token:
            raise ResideoApiError(
                "Azure SignalR negotiate returned no connectionToken", body=conn
            )
        wss_url = base_url.replace("https://", "wss://", 1)
        wss_url += f"&id={quote(conn_token)}&access_token={azure_jwt}"
        return {"wss_url": wss_url, "access_token": azure_jwt}

    async def _azure_client_negotiate(self, url: str, azure_jwt: str) -> dict[str, Any]:
        """POST the Azure SignalR ``/client/negotiate`` with the Azure JWT (no Auth0/APIM)."""
        headers = {"Authorization": f"Bearer {azure_jwt}", "User-Agent": APP_USER_AGENT}
        try:
            resp = await self._session.post(
                url,
                headers=headers,
                data=b"",
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            )
        except aiohttp.ClientError as err:
            raise ResideoConnectionError(f"Azure SignalR negotiate failed: {err}") from err
        try:
            if not 200 <= resp.status < 300:
                body = await self._safe_body(resp)
                raise ResideoApiError(
                    f"Azure SignalR negotiate -> {resp.status}", status=resp.status, body=body
                )
            text = await resp.text()
            return json.loads(text) if text else {}
        finally:
            await resp.release()

    # -- writes (devsrv commands; see resideo-api-spec.md §4) -----------------
    async def _command(self, mac: str, command: str, body: dict[str, Any]) -> dict[str, Any]:
        """PUT a devsrv command. Injects ``ChannelId``; returns ``{"TransactionId": ...}``."""
        payload = {**body, "ChannelId": DEFAULT_CHANNEL_ID}
        _LOGGER.debug("command %s mac=%s body=%s", command, mac, payload)
        result = await self.put(DEVSRV_DEVICE.format(mac=mac) + "/" + command, json=payload)
        _LOGGER.debug("command %s accepted -> %s", command, result)
        return result

    async def set_cool_setpoint(
        self, mac: str, value: float, status: str = SETPOINT_PERMANENT_HOLD
    ) -> dict[str, Any]:
        """Set the cool setpoint. ✅ captured live."""
        return await self._command(
            mac, "coolSetpoint", {"SetpointStatus": status, "SetpointValue": float(value)}
        )

    async def set_heat_setpoint(
        self, mac: str, value: float, status: str = SETPOINT_PERMANENT_HOLD
    ) -> dict[str, Any]:
        """Set the heat setpoint. ◐ schema-confirmed — TODO verify live."""
        return await self._command(
            mac, "heatSetpoint", {"SetpointStatus": status, "SetpointValue": float(value)}
        )

    async def set_system_switch(self, mac: str, mode: str) -> dict[str, Any]:
        """Set the system mode (Heat/Cool/Off/Auto/EmergencyHeat). ◐ TODO verify live."""
        return await self._command(mac, "systemSwitch", {"SystemSwitchValue": mode})

    async def set_fan(self, mac: str, position: str, speed: int = 1) -> dict[str, Any]:
        """Set the fan (Auto/On/Circulate). ◐ TODO verify live."""
        return await self._command(
            mac, "fanSwitch", {"FanSwitch": {"Position": position, "Speed": speed}}
        )

    async def set_hold(self, mac: str, status: str) -> dict[str, Any]:
        """Set the hold status without changing a setpoint. ◐ TODO verify live."""
        return await self._command(mac, "hold", {"Status": status})

    async def set_priority(
        self,
        mac: str,
        priority_type: str,
        rooms: list[int],
        status: str = SETPOINT_PERMANENT_HOLD,
    ) -> dict[str, Any]:
        """Set room priority (PickARoom/FollowMe). ◐ TODO verify live."""
        return await self._command(
            mac,
            "priority",
            {
                "PriorityStatus": status,
                "CurrentPriority": {
                    "PriorityType": priority_type,
                    "SelectedRooms": rooms,
                    "Rooms": None,
                },
            },
        )

    async def set_feels_like(self, mac: str, enabled: bool) -> dict[str, Any]:
        """Toggle the humidity-adjusted "Feels Like" display. ✅ verified live (spec §4).

        ``FeelsLikeEnabled`` is a non-nullable bool — always send it explicitly (a body that
        omits it sets it ``false``). State reads from ``Reported.FeelsLikeEnabled``.
        """
        return await self._command(mac, "feelsLike", {"FeelsLikeEnabled": bool(enabled)})

    async def set_adaptive_recovery(self, mac: str, mode: str) -> dict[str, Any]:
        """Set the adaptive-recovery mode. ✅ verified live (spec §4).

        ``mode`` is an ``AdaptiveRecoveryModeEnumRequest``: ``None`` / ``AdaptiveIntelligentRecovery``
        / ``DelayedStartRecovery``. Applies even with the schedule off; state reads from
        ``Reported.ActiveAdaptiveRecoveryMode``.
        """
        return await self._command(mac, "adaptiveIntelligentRecovery", {"Mode": mode})

    async def set_schedule_enabled(self, mac: str, enabled: bool) -> dict[str, Any]:
        """Enable/disable the device schedule. ✅ verified live (spec §4).

        ⚠️ Enabling makes the device follow the active schedule period (``SetpointStatus`` -> ``NoHold``
        and the heat/cool setpoints jump to that period's values); disabling leaves the schedule's
        last setpoints in place (it does *not* restore the prior manual setpoint).
        """
        return await self._command(mac, "schedule/enabled", {"ScheduleEnabled": bool(enabled)})

    async def set_freeze_protection(self, mac: str, low_limit_degrees: float) -> dict[str, Any]:
        """Set the freeze-protection low-temperature floor (°F). ✅ verified live (spec §4).

        Only the limit is settable here (the ``Configured`` flag is firmware-managed). State reads
        from ``/configuration`` -> ``Reported.FreezeProtection.LowLimitDegrees``.

        ⚠️ ``LowLimitDegrees`` must be an **integer** — a float (e.g. ``41.0``) returns ``400``.
        """
        return await self._command(
            mac, "freezeProtection", {"LowLimitDegrees": int(round(low_limit_degrees))}
        )

    async def set_setpoint_capabilities(
        self,
        mac: str,
        *,
        heat_min: float | None = None,
        heat_max: float | None = None,
        cool_min: float | None = None,
        cool_max: float | None = None,
    ) -> dict[str, Any]:
        """Set the Setpoint-Limits floor/ceiling (°F, int). ✅ verified live (spec §4, 2026-06).

        Write keys are flat lowerCamelCase and **differ from the read keys**. ⚠️ The device requires
        the **full four-field body** — a partial body returns ``202`` but is **silently ignored**
        (the spec's earlier "merge" note was a misread; verified live). Callers should pass all four.
        Values are sent as **integers** (the form the app uses and the one verified to apply).

        - ``heat_min`` -> ``minHeatSetpoint`` (read-back ``MinimumHeatSetpointAllowed``)
        - ``heat_max`` -> ``maxHeatSetpoint`` (read-back ``MaximumHeatSetpointAllowed``)
        - ``cool_min`` -> ``minCoolSetpoint`` (read-back ``MinimumCoolSetpointAllowed``)
        - ``cool_max`` -> ``maxCoolSetpoint`` (read-back ``MaximumCoolSetpointAllowed``)

        Values are clamped to the device floor/ceiling; the read-back applies cloud-side in ~2 s.
        """
        body: dict[str, Any] = {}
        if heat_min is not None:
            body["minHeatSetpoint"] = int(round(heat_min))
        if heat_max is not None:
            body["maxHeatSetpoint"] = int(round(heat_max))
        if cool_min is not None:
            body["minCoolSetpoint"] = int(round(cool_min))
        if cool_max is not None:
            body["maxCoolSetpoint"] = int(round(cool_max))
        return await self._command(mac, "setPointCapabilities", body)

    async def set_accessory_value(
        self,
        mac: str,
        accessory_id: int,
        *,
        sensitivity: str,
        exclude_motion: bool,
        exclude_temp: bool,
    ) -> dict[str, Any]:
        """Write a remote room-sensor's occupancy config (spec §10.4). ✅ verified live.

        Per-accessory ``PUT .../accessories/{accessory_id}/accessoryValue``. The write keys are
        **lowercase and differ from the read keys** (``sensitivity`` ↔ ``OccupancySensitivity``,
        ``excludeMotion`` ↔ ``ExcludeMotion``, ``excludeTemp`` ↔ ``ExcludeTemp``) and there is
        **no per-field merge** — always send all three (an omitted field risks defaulting).

        Propagation is asymmetric: ``excludeMotion``/``excludeTemp`` are cloud aggregation flags
        that apply in ~10 s (immediate read-back via ``/group/0/rooms``), whereas ``sensitivity``
        is wireless-sensor config that is accepted (``202``) but **deferred** until the battery
        sensor's next check-in (eventually consistent — do not expect an immediate read-back).
        """
        return await self._command(
            mac,
            f"accessories/{accessory_id}/accessoryValue",
            {
                "sensitivity": sensitivity,
                "excludeMotion": bool(exclude_motion),
                "excludeTemp": bool(exclude_temp),
            },
        )

    # -- helpers --------------------------------------------------------------
    @staticmethod
    def iter_devices(accounts: dict[str, Any]) -> list[dict[str, Any]]:
        """Walk the consumer account graph and return flattened device dicts.

        Ported from ``spikes/resideo_consumer.py``. Each dict: name, location, deviceId
        (= MAC), globalDeviceType, globalId, consumerDeviceId.
        """
        out: list[dict[str, Any]] = []
        data = accounts.get("data", {}) or {}
        for cu in data.get("consumerUsers", []) or []:
            ca = cu.get("consumerAccount", {}) or {}
            for loc in ca.get("locations", []) or []:
                for cd in loc.get("consumerDevices", []) or []:
                    dev = cd.get("device", {}) or {}
                    out.append(
                        {
                            "name": cd.get("name") or dev.get("deviceId"),
                            "location": loc.get("name"),
                            "deviceId": dev.get("deviceId"),
                            "globalDeviceType": dev.get("globalDeviceType"),
                            "globalId": dev.get("id"),
                            "consumerDeviceId": cd.get("id"),
                        }
                    )
        return out

    @staticmethod
    def iter_locations(accounts: dict[str, Any]) -> list[dict[str, Any]]:
        """Group the account graph by location for SignalR (spec §9.2).

        Returns ``[{"node_id", "name", "device_ids": [MAC, ...]}]``. ``node_id`` is the
        location's raw base64 ``id`` (``ConsumerDeviceLocation:<uuid>``) — the exact argument
        ``SubscribeSignalRV2`` expects. ``device_ids`` are **all** devices in the location; the
        caller intersects with the thermostats it cares about.
        """
        out: list[dict[str, Any]] = []
        data = accounts.get("data", {}) or {}
        for cu in data.get("consumerUsers", []) or []:
            ca = cu.get("consumerAccount", {}) or {}
            for loc in ca.get("locations", []) or []:
                device_ids = [
                    mac
                    for cd in loc.get("consumerDevices", []) or []
                    if (mac := (cd.get("device", {}) or {}).get("deviceId"))
                ]
                out.append(
                    {
                        "node_id": loc.get("id"),
                        "name": loc.get("name"),
                        "device_ids": device_ids,
                    }
                )
        return out
