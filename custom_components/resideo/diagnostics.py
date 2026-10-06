"""Diagnostics support for the Resideo integration.

Dumps the raw API snapshots the coordinator holds — invaluable for a reverse-engineered API,
where most bug reports come down to "what shape did the cloud actually send?".
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .aioresideo.exceptions import ResideoError
from .const import CONF_REFRESH_TOKEN
from .coordinator import ResideoConfigEntry

# Secrets and hardware/household identifiers.
TO_REDACT = {
    CONF_REFRESH_TOKEN,
    "DeviceId",
    "MacID",
    "SerialNumber",
}

# The account graph (/accounts) on top of the above: who the user is and where they live.
# ``name`` covers location names, which are often a street address. ``id`` covers the
# user/account/location/device node ids, and the two cross-references below carry the same
# account and location ids under other names. Kept on purpose: ``countryCode``, ``locale``,
# ``timeZone``, ``weatherLocationId``, ``globalDeviceType`` — the regional metadata that
# decides things like which unit the cloud's outdoor temperature arrives in.
ACCOUNT_TO_REDACT = TO_REDACT | {
    "id",
    "deviceId",
    "consumerAccountId",
    "consumerDeviceLocationId",
    "name",
    "firstName",
    "lastName",
    "contactEmail",
    "primaryPhoneNumber",
    "address",
    "geoCoordinate",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ResideoConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics: entry data, the account graph, each device's raw snapshots."""
    coordinator = entry.runtime_data
    # The account graph isn't cached (setup only keeps what it derives from it), so fetch it
    # fresh; a failure must not cost the user the device snapshots they came for.
    try:
        account: dict[str, Any] = async_redact_data(
            await coordinator.api.async_get_accounts(), ACCOUNT_TO_REDACT
        )
    except ResideoError as err:
        account = {"error": str(err)}
    return {
        "entry": {
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
        },
        "account": account,
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
