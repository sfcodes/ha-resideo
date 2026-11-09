"""The Resideo (consumer API) integration.

Thin Home Assistant shell over the ``aioresideo`` client library. Owns the config-entry
lifecycle; the actual API/auth work lives in ``aioresideo``. Reads are **push** (Azure SignalR);
REST is used only to bootstrap at setup and resync on (re)connect (see ``coordinator.py``).
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .aioresideo import Resideo
from .aioresideo.exceptions import (
    ResideoAuthError,
    ResideoConnectionError,
    ResideoError,
)
from .availability import async_clear_unavailable
from .const import CONF_REFRESH_TOKEN, DOMAIN, PLATFORMS
from .coordinator import ResideoConfigEntry, ResideoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ResideoConfigEntry) -> bool:
    """Set up Resideo from a config entry."""
    session = async_get_clientsession(hass)

    def _token_updated(tokens: dict) -> None:
        """Persist a rotated refresh token back into the config entry."""
        if not tokens.get("refresh_token"):
            return
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_REFRESH_TOKEN: tokens["refresh_token"]},
        )

    api = Resideo(
        session,
        refresh_token=entry.data[CONF_REFRESH_TOKEN],
        token_updated_cb=_token_updated,
    )

    coordinator = ResideoDataUpdateCoordinator(hass, entry, api)

    # Bootstrap read — capabilities + the initial snapshot can't come from the stream.
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady as err:
        # `async_config_entry_first_refresh` raises this *bare*, carrying the real failure only
        # as __cause__ — and HA sets the integration card's reason from `str(exc)`, so the user
        # is told nothing at all about why setup failed. Re-raise with the coordinator's own
        # message, which for a refused cloud explains exactly what Resideo said.
        reason = str(coordinator.last_exception or "")
        if not reason:
            raise
        raise ConfigEntryNotReady(reason) from err.__cause__
    except ResideoAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except (ResideoConnectionError, ResideoError) as err:
        raise ConfigEntryNotReady(str(err)) from err

    entry.runtime_data = coordinator

    # Push is the data source: require the SignalR stream to come up. A failure raises
    # ConfigEntryNotReady / ConfigEntryAuthFailed (HA retries setup); we never fall back to polling.
    await coordinator.async_start_streams()
    entry.async_on_unload(coordinator.async_stop_streams)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ResideoConfigEntry) -> bool:
    """Unload a config entry.

    Note this deliberately does *not* clear the cloud-unavailable repair issue: a reload runs
    through here (reauth triggers one), and deleting the issue would reset both the user's
    dismissal and the start time the message is built from. It is cleared on recovery, and on
    removal by `async_remove_entry`.
    """
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ResideoConfigEntry) -> None:
    """Take the repair card down with the entry that raised it."""
    async_clear_unavailable(hass, entry)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ResideoConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Allow deleting devices (thermostats / room sensors / valves) the account no longer reports."""
    coordinator = entry.runtime_data
    current: set[str] = set(coordinator.valves)
    for mac, data in coordinator.data.items():
        current.add(mac)
        for room, accessory in data.rooms.air_sensor_accessories():
            current.add(f"{mac}_room{room.id}_acc{accessory.accessory_id}")
    return not any(
        domain == DOMAIN and identifier in current
        for domain, identifier in device_entry.identifiers
    )
