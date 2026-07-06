"""DataUpdateCoordinator for the Resideo integration — push via SignalR (spec §9).

Reads are **push-only**: there is no periodic poll (``update_interval=None``). REST is used only
to **bootstrap** at setup and to **resync** once on each (re)connect. Live state arrives over the
Azure SignalR stream and is merged into the cached shadow. If the stream fails, the coordinator
**reports an error** (entities go unavailable) — it never silently falls back to polling.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .aioresideo import (
    LIVE_FEED_MERGED_PROPERTIES,
    Resideo,
    ResideoAccessory,
    ResideoChangeConfirm,
    ResideoConfiguration,
    ResideoLiveFeed,
    ResideoLocation,
    ResideoPriority,
    ResideoRooms,
    ResideoStream,
    ResideoThermostat,
    apply_live_feed,
)
from .aioresideo.exceptions import (
    ResideoApiError,
    ResideoAuthError,
    ResideoConnectionError,
    ResideoError,
)
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

type ResideoConfigEntry = ConfigEntry[ResideoDataUpdateCoordinator]

# Wait this long for the first SignalR connect + subscribe + activate during setup.
STREAM_CONNECT_TIMEOUT = 30

# Coalesce a burst of value-less stream events (settings ChangeRequests + unmerged LiveFeed types)
# into one REST resync, fired this many seconds after the first event (trailing debounce). Also lets
# a just-written value settle on the cloud before we re-read it.
RESYNC_DEBOUNCE = 3.0


@dataclass
class ResideoDeviceData:
    """Everything the coordinator holds for one thermostat."""

    thermostat: ResideoThermostat
    rooms: ResideoRooms  # /group/0/rooms — remote room sensors (may be empty)
    configuration: ResideoConfiguration  # /configuration — capabilities/equipment (may be empty)
    priority: ResideoPriority  # /priority — room priority/selection (may be empty)


class ResideoDataUpdateCoordinator(DataUpdateCoordinator[dict[str, ResideoDeviceData]]):
    """Holds the shadow + room sensors of every thermostat, fed by the SignalR live stream."""

    config_entry: ResideoConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ResideoConfigEntry,
        api: Resideo,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=None,  # push-only — no periodic poll
        )
        self.api = api
        self._macs: list[str] = []
        self._targets: list[ResideoLocation] = []
        self._streams: list[ResideoStream] = []
        # Settings (Feels Like, Adaptive Recovery, ...) and unmerged value-types carry no usable
        # value on the stream; a stream event schedules this debounced REST resync to fetch them.
        self._resync_debouncer = Debouncer(
            hass,
            _LOGGER,
            cooldown=RESYNC_DEBOUNCE,
            immediate=False,
            function=self.async_refresh,
        )
        # accessoryValue (remote-sensor occupancy config) has no per-field merge, so every write
        # must send the full body — but its three controls (sensitivity + the two exclude flags)
        # are separate entities. These per-accessory optimistic overrides are the shared source for
        # composing that body: without them a write for one field would echo the others from the
        # shadow, which lags a ``202`` by ~2 s, so a second write inside that window would clobber
        # the first. Keyed by (mac, accessory_id); each field is cleared once a refresh confirms it.
        self._accessory_overrides: dict[tuple[str, int], dict[str, Any]] = {}
        # setPointCapabilities has the same no-merge gotcha: a partial body 202s but is silently
        # ignored, so every write must carry all four limits. Same shared-override trick (keyed by
        # mac) so two limit edits inside the ~2 s read-back window can't clobber each other.
        self._setpoint_limit_overrides: dict[str, dict[str, Any]] = {}

    async def _async_setup(self) -> None:
        """One-time discovery: the thermostats + the per-location SignalR targets."""
        try:
            thermostats = await self.api.async_get_thermostats()
            self._targets = await self.api.async_get_signalr_targets()
        except ResideoAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (ResideoConnectionError, ResideoError) as err:
            raise UpdateFailed(str(err)) from err
        self._macs = [t.mac for t in thermostats if t.mac]
        _LOGGER.debug(
            "Discovered %d thermostat(s) %s across %d location(s)",
            len(self._macs),
            self._macs,
            len(self._targets),
        )

    async def _async_update_data(self) -> dict[str, ResideoDeviceData]:
        """Bootstrap / resync read (NOT a periodic poll).

        Reads the shadow + rooms + configuration + priority per thermostat. The ``/priority`` read
        also (re)warms the SignalR live feed (spec §9.2). Runs at setup and once per (re)connect.

        Failures are isolated per device: a failing thermostat keeps its previous snapshot (or
        drops out if it never had one) so the others stay live; only every-device-failing raises.
        """
        result: dict[str, ResideoDeviceData] = {}
        failures: list[str] = []
        for mac in self._macs:
            try:
                result[mac] = await self._async_read_device(mac)
            except ResideoAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except (ResideoConnectionError, ResideoError) as err:
                failures.append(f"{mac}: {err}")
                previous = (self.data or {}).get(mac)
                if previous is not None:
                    result[mac] = previous
        if failures:
            if len(failures) == len(self._macs):
                raise UpdateFailed("; ".join(failures))
            _LOGGER.warning(
                "Resync failed for %d of %d thermostat(s): %s",
                len(failures),
                len(self._macs),
                "; ".join(failures),
            )
        return result

    async def _async_read_device(self, mac: str) -> ResideoDeviceData:
        """Read one thermostat's full snapshot.

        Rooms / configuration / priority are best-effort: a stripped-down thermostat may 404
        on these, but the shadow must succeed.
        """
        thermostat = await self.api.async_get_device(mac)
        try:
            rooms = await self.api.async_get_rooms(mac)
        except ResideoApiError as err:
            _LOGGER.debug("No room sensors for %s (%s)", mac, err)
            rooms = ResideoRooms({})
        try:
            configuration = await self.api.async_get_configuration(mac)
        except ResideoApiError as err:
            _LOGGER.debug("No configuration for %s (%s)", mac, err)
            configuration = ResideoConfiguration({})
        try:
            priority = await self.api.async_get_priority(mac)
        except ResideoApiError as err:
            _LOGGER.debug("No priority for %s (%s)", mac, err)
            priority = ResideoPriority({})
        return ResideoDeviceData(
            thermostat=thermostat,
            rooms=rooms,
            configuration=configuration,
            priority=priority,
        )

    # -- full-body writes (optimism-coalesced; see ``__init__``) ---------------
    @staticmethod
    async def _async_write_with_override(
        override: dict[str, Any],
        field: str,
        value: Any,
        write: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Record ``override[field] = value`` and send the full body via ``write(override)``.

        On failure the override is reverted, so a failed write can't poison later full-body
        composes. The calling entity clears its field once a refresh confirms it.
        """
        had_previous = field in override
        previous = override.get(field)
        override[field] = value
        try:
            return await write(override)
        except Exception:
            if had_previous:
                override[field] = previous
            else:
                override.pop(field, None)
            raise

    def accessory_override(self, mac: str, accessory_id: int) -> dict[str, Any]:
        """Shared optimistic overrides for one accessory's writable fields (see ``__init__``)."""
        return self._accessory_overrides.setdefault((mac, accessory_id), {})

    async def async_write_accessory_value(
        self,
        mac: str,
        accessory_id: int,
        accessory: ResideoAccessory,
        *,
        field: str,
        value: Any,
    ) -> dict[str, Any]:
        """Set one ``accessoryValue`` field, sending the full body from the shared overrides.

        ``field`` is one of ``sensitivity`` / ``exclude_motion`` / ``exclude_temp`` (the API kwargs).
        Fields with no pending override fall back to the current shadow ``accessory`` snapshot.
        """
        return await self._async_write_with_override(
            self.accessory_override(mac, accessory_id),
            field,
            value,
            lambda ov: self.api.async_set_accessory_value(
                mac,
                accessory_id,
                sensitivity=ov.get("sensitivity", accessory.occupancy_sensitivity),
                exclude_motion=ov.get("exclude_motion", accessory.exclude_motion),
                exclude_temp=ov.get("exclude_temp", accessory.exclude_temperature),
            ),
        )

    def setpoint_limit_override(self, mac: str) -> dict[str, Any]:
        """Shared optimistic overrides for a device's four setpoint limits (see ``__init__``)."""
        return self._setpoint_limit_overrides.setdefault(mac, {})

    async def async_write_setpoint_limit(
        self,
        mac: str,
        configuration: ResideoConfiguration,
        *,
        field: str,
        value: float,
    ) -> dict[str, Any]:
        """Set one setpoint limit, sending the full four-field body from the shared overrides.

        ``field`` is one of ``heat_min`` / ``heat_max`` / ``cool_min`` / ``cool_max``. Fields with
        no pending override fall back to the current ``configuration`` snapshot.
        """
        return await self._async_write_with_override(
            self.setpoint_limit_override(mac),
            field,
            value,
            lambda ov: self.api.async_set_setpoint_capabilities(
                mac,
                heat_min=ov.get("heat_min", configuration.min_heat_setpoint),
                heat_max=ov.get("heat_max", configuration.max_heat_setpoint),
                cool_min=ov.get("cool_min", configuration.min_cool_setpoint),
                cool_max=ov.get("cool_max", configuration.max_cool_setpoint),
            ),
        )

    # -- SignalR streaming ----------------------------------------------------
    async def async_start_streams(self) -> None:
        """Build + connect one stream per location with a thermostat. Raises on first-connect failure.

        On success, schedules each supervisor (``async_run``) as a background task. A connect
        failure raises ``ConfigEntryNotReady`` / ``ConfigEntryAuthFailed`` so setup is retried —
        push is the contract, so we never proceed without it.
        """
        known = set(self._macs)
        for target in self._targets:
            device_ids = [m for m in target.device_ids if m in known]
            if not device_ids or not target.node_id:
                continue
            self._streams.append(
                self.api.create_stream(
                    target.node_id,
                    device_ids,
                    self._on_stream_event,
                    on_connected=self._on_stream_connected,
                    on_error=self._on_stream_error,
                )
            )
        if not self._streams:
            raise ConfigEntryNotReady("No SignalR-capable thermostat locations found")

        try:
            for stream in self._streams:
                await stream.async_connect_once_or_raise(STREAM_CONNECT_TIMEOUT)
        except ResideoAuthError as err:
            await self.async_stop_streams()
            raise ConfigEntryAuthFailed(str(err)) from err
        except (ResideoError, OSError, TimeoutError) as err:
            await self.async_stop_streams()
            raise ConfigEntryNotReady(f"SignalR stream failed to start: {err}") from err

        for stream in self._streams:
            self.config_entry.async_create_background_task(
                self.hass, stream.async_run(), name=f"{DOMAIN}_signalr"
            )

    async def async_stop_streams(self) -> None:
        """Tear down all streams (graceful unsubscribe + close) + cancel the resync debouncer."""
        streams, self._streams = self._streams, []
        for stream in streams:
            await stream.async_stop()
        self._resync_debouncer.async_shutdown()

    @callback
    def _on_stream_event(self, event: ResideoLiveFeed | ResideoChangeConfirm) -> None:
        """Apply one SignalR event to the cached data (called on the loop from the recv loop)."""
        if isinstance(event, ResideoChangeConfirm):
            if not event.success:
                # The cloud accepted (202) but the device/service rejected the change — the
                # optimistic UI value will be rolled back by the reconcile refresh.
                _LOGGER.warning(
                    "Resideo rejected change %s (txn=%s) for %s",
                    event.change_name,
                    event.transaction_id,
                    event.device_id,
                )
                return
            _LOGGER.debug(
                "ChangeRequest %s ok txn=%s", event.change_name, event.transaction_id
            )
            # Settings changes (Feels Like, Adaptive Recovery, schedule, reminders, ...) ride
            # ChangeRequest and carry NO values -> re-read the shadow to reflect them (spec §9a/§9.1).
            self.schedule_resync()
            return
        if not isinstance(event, ResideoLiveFeed) or not self.data:
            return
        current = self.data.get(event.device_id)
        if current is None:
            return  # event for a device we don't track
        if event.property_name not in LIVE_FEED_MERGED_PROPERTIES:
            # A value-bearing type we don't merge in-memory (Schedule*, DrEventStatus, ...) ->
            # resync to reflect it without guessing its push shape.
            self.schedule_resync()
            return
        new_shadow, new_rooms = apply_live_feed(
            current.thermostat.attributes, current.rooms.attributes, event
        )
        updated = ResideoDeviceData(
            thermostat=ResideoThermostat(new_shadow),
            rooms=ResideoRooms(new_rooms),
            configuration=current.configuration,
            priority=current.priority,
        )
        # Pushes the new data AND clears any prior error state -> entities available again.
        self.async_set_updated_data({**self.data, event.device_id: updated})

    @callback
    def schedule_resync(self) -> None:
        """Schedule the debounced REST resync (coalesces bursts into one refresh).

        Used for value-less stream events (settings ChangeRequests, unmerged LiveFeed types)
        and by entities after a write — the optimistic value covers the UI meanwhile, and the
        debounce lets the just-written value settle on the cloud before it is re-read.
        """
        self._resync_debouncer.async_schedule_call()

    async def _on_stream_connected(self) -> None:
        """Resync once on each (re)connect — catch changes missed while down + never-pushed fields."""
        await self.async_refresh()

    @callback
    def _on_stream_error(self, err: Exception) -> None:
        """A sustained stream failure -> entities unavailable (reported, not masked by polling)."""
        self.async_set_update_error(UpdateFailed(str(err)))
        if isinstance(err, ResideoAuthError):
            self.config_entry.async_start_reauth(self.hass)
