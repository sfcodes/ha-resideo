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
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ResideoConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Allow deleting devices (thermostats / room sensors) the account no longer reports."""
    coordinator = entry.runtime_data
    current: set[str] = set()
    for mac, data in coordinator.data.items():
        current.add(mac)
        for room, accessory in data.rooms.air_sensor_accessories():
            current.add(f"{mac}_room{room.id}_acc{accessory.accessory_id}")
    return not any(
        domain == DOMAIN and identifier in current
        for domain, identifier in device_entry.identifiers
    )
