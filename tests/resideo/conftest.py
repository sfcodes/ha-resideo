"""Fixtures for the Resideo integration (HA-layer) tests.

The ``Resideo`` facade is mocked at the integration boundary and serves the same sanitized
fixtures as the client tests (``tests/aioresideo/fixtures``); the SignalR stream is replaced
by :class:`FakeStream`, which captures the coordinator callbacks so tests can inject live
events, connect resyncs, and errors.
"""

from __future__ import annotations

import json
import pathlib
from copy import deepcopy
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.resideo.aioresideo import (
    Resideo,
    ResideoAccountDevice,
    ResideoClient,
    ResideoConfiguration,
    ResideoPriority,
    ResideoRooms,
    ResideoThermostat,
)
from custom_components.resideo.const import CONF_REFRESH_TOKEN, DOMAIN

FIXTURES = pathlib.Path(__file__).parent.parent / "aioresideo" / "fixtures"

MAC = "AABBCCDDEEFF"
SUB = "auth0|user-1"

WRITE_METHODS = (
    "async_set_cool_setpoint",
    "async_set_heat_setpoint",
    "async_set_system_switch",
    "async_set_fan",
    "async_set_hold",
    "async_set_priority",
    "async_set_feels_like",
    "async_set_adaptive_recovery",
    "async_set_schedule_enabled",
    "async_set_freeze_protection",
    "async_set_setpoint_capabilities",
    "async_set_accessory_value",
)


def load_fixture_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class FakeStream:
    """Stands in for ResideoStream; captures the coordinator's callbacks for injection."""

    def __init__(self, node_id, device_ids, on_event, on_connected, on_error) -> None:
        self.node_id = node_id
        self.device_ids = device_ids
        self.on_event = on_event
        self.on_connected = on_connected
        self.on_error = on_error
        self.connected = False
        self.stopped = False

    async def async_connect_once_or_raise(self, timeout: float = 30.0) -> None:
        self.connected = True

    async def async_run(self) -> None:
        return  # the supervisor loop is irrelevant to these tests

    async def async_stop(self) -> None:
        self.stopped = True


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let the HA test loader discover custom_components/resideo."""
    return


@pytest.fixture
def accounts_data() -> dict:
    return load_fixture_json("accounts.json")


@pytest.fixture
def device_shadow() -> dict:
    return load_fixture_json("device.json")


@pytest.fixture
def rooms_data() -> dict:
    return load_fixture_json("rooms.json")


@pytest.fixture
def configuration_data() -> dict:
    return load_fixture_json("configuration.json")


@pytest.fixture
def shadows(device_shadow: dict) -> dict[str, Any]:
    """Per-MAC device shadows served by the mock; a test may map a MAC to an Exception."""
    return {MAC: device_shadow}


@pytest.fixture
def mock_api(
    accounts_data: dict,
    shadows: dict[str, Any],
    rooms_data: dict,
    configuration_data: dict,
) -> AsyncMock:
    """A Resideo facade mock serving the sanitized fixtures.

    Reads snapshot their source dicts lazily and deep-copy them, so a test can mutate
    ``device_shadow`` / ``configuration_data`` / ``shadows`` and trigger a resync.
    """
    api = AsyncMock(spec=Resideo)
    api.refresh_token = "refresh-token"

    api.async_get_thermostats.side_effect = lambda: [
        d
        for d in (ResideoAccountDevice(x) for x in ResideoClient.iter_devices(accounts_data))
        if d.is_thermostat
    ]
    api.async_get_signalr_targets.side_effect = lambda: ResideoClient.iter_locations(
        accounts_data
    )

    def _get_device(mac: str) -> ResideoThermostat:
        shadow = shadows[mac]
        if isinstance(shadow, Exception):
            raise shadow
        return ResideoThermostat(deepcopy(shadow))

    api.async_get_device.side_effect = _get_device
    api.async_get_rooms.side_effect = lambda mac: ResideoRooms(deepcopy(rooms_data))
    api.async_get_configuration.side_effect = lambda mac: ResideoConfiguration(
        deepcopy(configuration_data)
    )
    api.async_get_priority.side_effect = lambda mac: ResideoPriority(
        {"PriorityStatus": "NoHold", "Priority": {"PriorityType": "PickARoom", "SelectedRooms": []}}
    )
    for name in WRITE_METHODS:
        getattr(api, name).side_effect = None
        getattr(api, name).return_value = {"TransactionId": "tx-1"}

    streams: list[FakeStream] = []

    def _create_stream(node_id, device_ids, on_event, *, on_connected=None, on_error=None):
        stream = FakeStream(node_id, device_ids, on_event, on_connected, on_error)
        streams.append(stream)
        return stream

    api.create_stream = MagicMock(side_effect=_create_stream)
    api.streams = streams
    return api


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Resideo (test@example.com)",
        data={CONF_REFRESH_TOKEN: "refresh-token"},
        unique_id=SUB,
    )


async def setup_integration(
    hass: HomeAssistant, entry: MockConfigEntry, api: AsyncMock
) -> None:
    """Add + set up the entry with the mocked facade (US units for °F fixture parity)."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    entry.add_to_hass(hass)
    with patch("custom_components.resideo.Resideo", return_value=api):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


@pytest.fixture
async def init_integration(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_api: AsyncMock
) -> MockConfigEntry:
    """The integration set up and loaded against the mocked facade."""
    await setup_integration(hass, mock_config_entry, mock_api)
    return mock_config_entry


def eid(hass: HomeAssistant, platform: str, unique_id: str) -> str:
    """Resolve an entity_id from its unique_id (robust against display-name changes)."""
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    assert entity_id is not None, f"no {platform} entity with unique_id {unique_id}"
    return entity_id


async def advance_time(hass: HomeAssistant, seconds: float) -> None:
    """Fire HA timers due within the next ``seconds`` (debounced resync, reconcile, ...)."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()
