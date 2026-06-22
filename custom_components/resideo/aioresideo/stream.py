"""Async Azure SignalR client for the Resideo data-sync live feed (see ``resideo-api-spec.md`` §9).

One :class:`ResideoStream` owns one WSS connection per **location**. It does the full two-step
negotiate, subscribes, **activates** the values feed by reading ``/priority`` per device, sends a
``{"type":6}`` ping to keep the socket alive, and reconnects — on drop, and **proactively shortly
before the feed's ~12-min ``SubscriptionExpiration``** (which cannot be extended in place), which
starts a fresh feed window.

Parsed events are delivered to a **synchronous** ``on_event`` callback — call it from the event loop
(e.g. ``DataUpdateCoordinator.async_set_updated_data``). The supervisor (:meth:`async_run`) runs until
cancelled; :meth:`async_connect_once_or_raise` is the setup gate that establishes the first connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime

import aiohttp

from .client import ResideoClient
from .const import (
    SIGNALR_FEED_RECONNECT_MARGIN,
    SIGNALR_FEED_TTL_FALLBACK,
    SIGNALR_HANDSHAKE,
    SIGNALR_PING_INTERVAL,
    SIGNALR_RECORD_SEPARATOR,
    SIGNALR_STALL_TIMEOUT,
)
from .exceptions import ResideoAuthError, ResideoConnectionError, ResideoError
from .objects.events import ResideoEvent, parse_event

_LOGGER = logging.getLogger(__name__)

RS = SIGNALR_RECORD_SEPARATOR
_HANDSHAKE_FRAME = json.dumps(SIGNALR_HANDSHAKE) + RS
_PING_FRAME = json.dumps({"type": 6}) + RS
_BACKOFF_CAP = 60.0

EventCallback = Callable[[ResideoEvent], None]
ConnectedCallback = Callable[[], Awaitable[None]]
ErrorCallback = Callable[[Exception], None]


class ResideoStream:
    """A single-location SignalR live-feed connection with reconnect + feed renewal."""

    def __init__(
        self,
        client: ResideoClient,
        location_node_id: str,
        device_ids: list[str],
        on_event: EventCallback,
        *,
        on_connected: ConnectedCallback | None = None,
        on_error: ErrorCallback | None = None,
    ) -> None:
        self._client = client
        self._location_node_id = location_node_id
        self._device_ids = list(device_ids)
        self._on_event = on_event
        self._on_connected = on_connected
        self._on_error = on_error

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._last_frame = 0.0  # time.monotonic() of the last inbound frame (stall watchdog)
        self._feed_expiry = 0.0  # time.time() epoch of the feed's SubscriptionExpiration
        self._closing = False
        self._invocation = 0

    # -- public API -----------------------------------------------------------
    async def async_connect_once_or_raise(self, timeout: float = 30.0) -> None:
        """Setup gate: do the first connect + subscribe + activate, raising on failure.

        Leaves the socket connected (with keepalive running); :meth:`async_run` adopts it.
        """
        async with asyncio.timeout(timeout):
            await self._connect_once()

    async def async_run(self) -> None:
        """Supervisor loop: keep the connection up, reconnecting with backoff, until cancelled."""
        backoff = 1.0
        adopt = self._ws is not None and not self._ws.closed  # adopt the setup-gate connection
        while not self._closing:
            try:
                if not adopt:
                    await self._connect_once()
                adopt = False
                backoff = 1.0
                await self._recv_loop()  # returns when the socket closes
            except asyncio.CancelledError:
                raise
            except ResideoAuthError as err:
                _LOGGER.error("SignalR auth failure (fatal): %s", err)
                if self._on_error is not None:
                    self._on_error(err)  # coordinator surfaces this as reauth
                self._closing = True  # stop the supervisor; reauth/reload owns recovery
            except (TimeoutError, ResideoError, aiohttp.ClientError, OSError) as err:
                _LOGGER.warning("SignalR error: %s (reconnect in %.0fs)", err, backoff)
                if self._on_error is not None:
                    self._on_error(err)
            finally:
                await self._teardown_socket()
            if self._closing:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_CAP)

    async def async_stop(self) -> None:
        """Best-effort graceful teardown (unsubscribe + close)."""
        self._closing = True
        ws = self._ws
        if ws is not None and not ws.closed:
            with contextlib.suppress(Exception):
                await self._send_invocation("UnsubscribeSignalRV2", [self._location_node_id])
        await self._teardown_socket()

    # -- connection lifecycle -------------------------------------------------
    async def _connect_once(self) -> None:
        neg = await self._client.async_signalr_negotiate()
        try:
            self._ws = await self._client.session.ws_connect(neg["wss_url"], heartbeat=None)
        except aiohttp.ClientError as err:
            raise ResideoConnectionError(f"SignalR WSS connect failed: {err}") from err
        await self._ws.send_str(_HANDSHAKE_FRAME)
        await self._read_handshake_ack()
        await self._send_invocation("SubscribeSignalRV2", [self._location_node_id])
        self._last_frame = time.monotonic()
        self._feed_expiry = time.time() + SIGNALR_FEED_TTL_FALLBACK
        # Resync first (so HA has fresh data); its /priority reads also help warm the feed.
        if self._on_connected is not None:
            try:
                await self._on_connected()
            except Exception:
                _LOGGER.warning("SignalR resync-on-connect failed", exc_info=True)
        await self._activate_feed()  # GET /priority per device -> starts LiveFeedEvents
        self._keepalive_task = asyncio.create_task(self._keepalive())
        _LOGGER.debug("SignalR connected + subscribed + activated (%s)", self._location_node_id)

    async def _read_handshake_ack(self) -> None:
        assert self._ws is not None
        msg = await self._ws.receive(timeout=20)
        if msg.type is not aiohttp.WSMsgType.TEXT:
            raise ResideoConnectionError(f"SignalR handshake failed: {msg.type}")
        for frame in _split(msg.data):
            obj = _loads(frame)
            if isinstance(obj, dict) and obj.get("error"):
                raise ResideoConnectionError(f"SignalR handshake error: {obj['error']}")

    async def _activate_feed(self) -> None:
        """Read ``/priority`` per device — the trigger that starts the values feed (spec §9.2)."""
        for mac in self._device_ids:
            try:
                await self._client.get_priority(mac)
            except ResideoError as err:
                _LOGGER.debug("Feed activation /priority failed for %s: %s", mac, err)

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        async for msg in self._ws:
            if msg.type is aiohttp.WSMsgType.TEXT:
                self._last_frame = time.monotonic()
                for frame in _split(msg.data):
                    self._handle_frame(frame)
            elif msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
            ):
                break

    def _handle_frame(self, frame: str) -> None:
        obj = _loads(frame)
        if not isinstance(obj, dict):
            return
        mtype = obj.get("type")
        if mtype == 6:  # inbound keepalive ping
            return
        if mtype == 3:  # completion of one of our invocations
            if obj.get("error"):
                _LOGGER.debug("SignalR invocation error: %s", obj["error"])
            return
        if mtype == 1 and obj.get("target") == "events":
            for arg in obj.get("arguments", []) or []:
                self._dispatch_event(arg)

    def _dispatch_event(self, arg: object) -> None:
        try:
            event = parse_event(arg)  # type: ignore[arg-type]
        except Exception:
            _LOGGER.debug("Failed to parse SignalR event", exc_info=True)
            return
        if event is None:
            return
        exp = getattr(event, "subscription_expiration", None)
        if exp:
            self._note_expiry(exp)
        try:
            self._on_event(event)
        except Exception:
            _LOGGER.exception("Resideo on_event callback raised")

    def _note_expiry(self, iso: str) -> None:
        ts = _parse_iso(iso)
        if ts is not None:
            self._feed_expiry = ts

    # -- keepalive + renewal --------------------------------------------------
    async def _keepalive(self) -> None:
        try:
            while True:
                await asyncio.sleep(SIGNALR_PING_INTERVAL)
                ws = self._ws
                if ws is None or ws.closed:
                    return
                if time.monotonic() - self._last_frame > SIGNALR_STALL_TIMEOUT:
                    _LOGGER.warning("SignalR stall (no frames %ds) -> reconnect", SIGNALR_STALL_TIMEOUT)
                    await ws.close()
                    return
                try:
                    await ws.send_str(_PING_FRAME)
                except Exception:
                    return
                # The values feed expires ~12 min after activation and CANNOT be extended in place
                # (HeartbeatV2 / re-reading /priority don't move SubscriptionExpiration — verified
                # live). Reconnect shortly before expiry to start a fresh feed window; the supervisor
                # re-negotiates, re-subscribes, resyncs and re-activates.
                if time.time() >= self._feed_expiry - SIGNALR_FEED_RECONNECT_MARGIN:
                    _LOGGER.debug("SignalR feed nearing expiry -> cycling connection for a fresh feed")
                    await ws.close()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.debug("SignalR keepalive task error", exc_info=True)

    # -- low-level ------------------------------------------------------------
    async def _send_invocation(self, target: str, arguments: list[object]) -> None:
        assert self._ws is not None
        self._invocation += 1
        frame = json.dumps(
            {
                "type": 1,
                "invocationId": str(self._invocation),
                "target": target,
                "arguments": arguments,
            }
        )
        await self._ws.send_str(frame + RS)

    async def _teardown_socket(self) -> None:
        task = self._keepalive_task
        self._keepalive_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        ws = self._ws
        self._ws = None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()


# -- module helpers -----------------------------------------------------------
def _split(buf: str) -> list[str]:
    return [p for p in buf.split(RS) if p]


def _loads(frame: str) -> object:
    try:
        return json.loads(frame)
    except (ValueError, TypeError):
        return None


def _parse_iso(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value).timestamp()
    except (ValueError, TypeError):
        return None
