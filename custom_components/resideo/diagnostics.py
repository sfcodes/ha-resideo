"""Diagnostics support for the Resideo integration.

Dumps the raw API snapshots the coordinator holds — invaluable for a reverse-engineered API,
where most bug reports come down to "what shape did the cloud actually send?" — plus the
pushes received and the commands sent since the integration loaded (see ``capture.py``), and
where the state held in memory has drifted from what the cloud reports right now.

These files get attached to public issues and committed as test fixtures (``tests/homes``), so
everything that identifies the person, the household or the hardware is blanked first (see
``scrub``).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant

from .aioresideo import ResideoAccountDevice, ResideoClient
from .aioresideo.exceptions import ResideoError
from .const import CONF_REFRESH_TOKEN
from .coordinator import ResideoConfigEntry, ResideoDataUpdateCoordinator

# Secrets, hardware identifiers, and what the household typed in or had typed in for it.
TO_REDACT = {
    CONF_REFRESH_TOKEN,
    "DeviceId",
    "MacID",
    "SerialNumber",
    "DiscriminatorValue",  # Matter: how this one thermostat announces itself for pairing
    # A Matter fabric's label is the home's name in the ecosystem that paired it, and people
    # name their home after its street address.
    "Label",
    # The installer's name, phone, e-mail and website: it places the home in a town.
    "DealerInformation",
    # To the millisecond, so it singles out one household, and no bug has ever hinged on it.
    "RegistrationDate",
}

# The account graph (/accounts) on top of the above: who the user is and where they live.
# ``id`` covers the user/account/location/device node ids, and the two cross-references below
# carry the same account and location ids under other names. Kept on purpose: ``countryCode``,
# ``locale``, ``timeZone``, ``weatherLocationId``, ``globalDeviceType`` — the regional metadata
# that decides things like which unit the cloud's outdoor temperature arrives in.
ACCOUNT_TO_REDACT = TO_REDACT | {
    "id",
    "deviceId",
    "consumerAccountId",
    "consumerDeviceLocationId",
    "contactEmail",
    "primaryPhoneNumber",
    "photoUrl",
    "address",
    "geoCoordinate",
    # The weather service's key for the town the location is in.
    "weatherLocationId",
    # When the account and each device in it were created, to the millisecond.
    "createdAt",
    "updatedAt",
}

# A key ending in "Name" holds text somebody chose — a room, a sensor, the thermostat, a
# location (often a street address), a person — whatever the payload it turns up in. That
# matters for pushes: their samples are dumped raw, including types nobody has seen yet. These
# are the exceptions, holding the API's own vocabulary: the push type, the graph node type,
# the product a device is sold as, and the schedule period (Wake / Away / Home / Sleep).
NAME_KEYS_KEPT = {"PropertyName", "__typename", "productName", "PeriodName"}


def _is_private(key: str, to_redact: set[str]) -> bool:
    return key in to_redact or (key.lower().endswith("name") and key not in NAME_KEYS_KEPT)


def _redact(data: Any, to_redact: set[str], macs: tuple[str, ...]) -> Any:
    """Blank every private key's value, and every MAC wherever it sits inside a string.

    Key by key this is Home Assistant's ``async_redact_data`` (``None`` and ``""`` are left
    alone, so "not set" stays visible) with the name rule added. The string pass is for what
    no key can catch: a refused command's error message quotes the request URL, and the URL
    has the MAC in its path.
    """
    if isinstance(data, str):
        for mac in macs:
            data = data.replace(mac, REDACTED)
        return data
    if isinstance(data, list):
        return [_redact(item, to_redact, macs) for item in data]
    if not isinstance(data, dict):
        return data
    return {
        key: REDACTED
        if _is_private(key, to_redact) and value not in (None, "")
        else _redact(value, to_redact, macs)
        for key, value in data.items()
    }


def scrub(diagnostics: dict[str, Any], macs: tuple[str, ...] = ()) -> dict[str, Any]:
    """The diagnostics with everything private blanked. Scrubbing twice changes nothing, which
    is how a file is checked before it is committed (``scripts/evidence.py``)."""
    return {
        section: _redact(value, ACCOUNT_TO_REDACT if section == "account" else TO_REDACT, macs)
        for section, value in diagnostics.items()
    }


def _account_devices(accounts: dict[str, Any]) -> list[dict[str, Any]]:
    """Every device in the account by model and kind, thermostat or not.

    The kind is the device-class signal, and it is spelled inside the device's global id next
    to its MAC, so redaction takes it out of the account graph. A report about hardware this
    integration doesn't know yet hinges on it.
    """
    devices = (ResideoAccountDevice(d) for d in ResideoClient.iter_devices(accounts))
    return [
        {"model": d.model, "kind": d.device_kind, "supported": d.is_thermostat} for d in devices
    ]


_ABSENT = object()


def _differences(held: Any, cloud: Any, path: str = "") -> list[dict[str, Any]]:
    """Every leaf where two snapshots disagree, as ``{"path", "held", "cloud"}``; a side that
    lacks the key is left out of its entry."""
    if isinstance(held, dict) and isinstance(cloud, dict):
        return [
            difference
            for key in sorted(held.keys() | cloud.keys())
            for difference in _differences(
                held.get(key, _ABSENT), cloud.get(key, _ABSENT), f"{path}.{key}" if path else key
            )
        ]
    if isinstance(held, list) and isinstance(cloud, list) and len(held) == len(cloud):
        return [
            difference
            for index, (ours, theirs) in enumerate(zip(held, cloud, strict=True))
            for difference in _differences(ours, theirs, f"{path}[{index}]")
        ]
    if held == cloud:
        return []
    sides = {"held": held, "cloud": cloud}
    return [{"path": path, **{k: v for k, v in sides.items() if v is not _ABSENT}}]



async def _drift(
    coordinator: ResideoDataUpdateCoordinator, mac: str, held: dict[str, Any], macs: tuple[str, ...]
) -> dict[str, Any]:
    """Where the state built from pushes disagrees with a fresh read of the cloud.

    Only the shadow and the rooms are compared: they are what pushes are merged into. A path
    that a merged push type writes and that stays listed from one download to the next means
    the merge put a value in the wrong place. Also listed, and harmless: a reading that moved
    in the seconds since its last push, a key that pushes carry and the snapshots do not, and
    paths the cloud never pushes (they wait for the next resync).

    ``pushes_merged_since_read`` says how much the comparison is worth: at zero the state held
    is itself a read of the cloud, so finding no drift proves nothing about the merges.
    """
    merged = coordinator.capture.merged_since_read(mac)
    try:
        fresh = await coordinator.async_read_device(mac)
    except ResideoError as err:
        return {"error": str(err)}
    return {
        "pushes_merged_since_read": merged,
        "shadow": _differences(
            held["shadow"], _redact(fresh.thermostat.attributes, TO_REDACT, macs)
        ),
        "rooms": _differences(held["rooms"], _redact(fresh.rooms.attributes, TO_REDACT, macs)),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ResideoConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics: entry data, the account graph, and per device its raw
    snapshots, the pushes received, the commands sent, and the drift from the cloud."""
    coordinator = entry.runtime_data
    capture = coordinator.capture
    macs = tuple(coordinator.data)
    # The account graph isn't cached (setup only keeps what it derives from it), so fetch it
    # fresh; a failure must not cost the user the device snapshots they came for.
    account_devices: list[dict[str, Any]] = []
    try:
        account: dict[str, Any] = await coordinator.api.async_get_accounts()
        account_devices = _account_devices(account)
    except ResideoError as err:
        account = {"error": str(err)}
    diagnostics = scrub(
        {
            # Not the entry's title: it is "Resideo (<the account's e-mail>)".
            "entry": {"data": dict(entry.data)},
            "account": account,
            "account_devices": account_devices,
            "capture_since": capture.since,
            "devices": [
                {
                    "shadow": data.thermostat.attributes,
                    "rooms": data.rooms.attributes,
                    "configuration": data.configuration.attributes,
                    "priority": data.priority.attributes,
                    "pushes": capture.pushes(mac),
                    "writes": capture.writes(mac),
                }
                for mac, data in coordinator.data.items()
            ],
        },
        macs,
    )
    for mac, device in zip(macs, diagnostics["devices"], strict=True):
        device["drift"] = _redact(await _drift(coordinator, mac, device, macs), TO_REDACT, macs)
    return diagnostics
