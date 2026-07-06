"""Diagnostics support for the Resideo integration.

Dumps the raw API snapshots the coordinator holds — invaluable for a reverse-engineered API,
where most bug reports come down to "what shape did the cloud actually send?".
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_REFRESH_TOKEN
from .coordinator import ResideoConfigEntry

# Secrets and hardware/household identifiers.
TO_REDACT = {
    CONF_REFRESH_TOKEN,
    "DeviceId",
    "MacID",
    "SerialNumber",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ResideoConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics: entry data + each device's raw API snapshots."""
    coordinator = entry.runtime_data
    return {
        "entry": {
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
        },
        "devices": [
            {
                "shadow": async_redact_data(data.thermostat.attributes, TO_REDACT),
                "rooms": async_redact_data(data.rooms.attributes, TO_REDACT),
                "configuration": async_redact_data(data.configuration.attributes, TO_REDACT),
                "priority": async_redact_data(data.priority.attributes, TO_REDACT),
            }
            for data in coordinator.data.values()
        ],
    }
