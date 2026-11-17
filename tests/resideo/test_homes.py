"""Every home in ``tests/homes`` replayed through the integration.

A home is somebody's real installation (see CONTRIBUTING.md). These are the checks every one
of them gets for free; a test of what is particular to a home loads it with ``load_home``.
"""

from __future__ import annotations

import json
import logging
import pathlib
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant.typing import ClientSessionGenerator

from custom_components.resideo.aioresideo import (
    ResideoChangeConfirm,
    ResideoCommand,
    ResideoLiveFeed,
    parse_event,
)
from scripts import evidence
from tests.homes import captured_pushes, home_names, load_home

from .conftest import (
    MAC,
    advance_time,
    eid,
    home_api,
    load_fixture_json,
    setup_integration,
)


async def _replay(
    hass: HomeAssistant, entry: MockConfigEntry, home: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """Set the integration up on a home's snapshots, then push it everything it captured."""
    api = home_api(home)
    await setup_integration(hass, entry, api)
    assert entry.state is ConfigEntryState.LOADED

    for mac, device in zip(api.macs, home["data"]["devices"], strict=True):
        for name, value in captured_pushes(device):
            api.streams[0].on_event(ResideoLiveFeed(mac, name, value))
    await advance_time(hass, 4)  # the resync the unmerged ones asked for

    for mac in api.macs:
        assert hass.states.get(eid(hass, "climate", f"{mac}_climate")).state != STATE_UNAVAILABLE
    problems = [r for r in caplog.records if r.levelno >= logging.WARNING and "resideo" in r.name]
    assert not problems, [r.getMessage() for r in problems]


@pytest.fixture(params=home_names())
def home(request: pytest.FixtureRequest) -> dict[str, Any]:
    return load_home(request.param)


def test_home_holds_nothing_private(home: dict[str, Any]) -> None:
    assert evidence.find_leaks(home) == []


def test_home_says_what_hardware_it_is(home: dict[str, Any]) -> None:
    assert home["home"]["hardware"].strip()


async def test_home_sets_up_and_takes_every_push_it_captured(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    home: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _replay(hass, mock_config_entry, home, caplog)


async def test_a_download_becomes_a_home_that_replays(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
    mock_api: AsyncMock,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The whole path a contributor walks: the integration records, Home Assistant downloads,
    the script files it, and the tests replay it — on a second, clean Home Assistant setup."""
    stream = mock_api.streams[0]
    stream.on_event(parse_event(load_fixture_json("live_setpoint.json")))
    stream.on_event(parse_event(load_fixture_json("live_sensor_remote.json")))
    stream.on_event(ResideoLiveFeed(MAC, "ScheduleStatus", {"Value": "Running"}))
    write = load_fixture_json("cool_setpoint_write.json")
    mock_api.command_cb(
        ResideoCommand(MAC, "coolSetpoint", write["request_body"], result=write["response_body"])
    )
    stream.on_event(
        ResideoChangeConfirm(
            MAC, write["response_body"]["TransactionId"], "changeSetpoint", "AppInitiated", True
        )
    )
    await advance_time(hass, 4)
    download = tmp_path / "config_entry-resideo.json"
    download.write_text(
        json.dumps(
            {
                "home_assistant": {"version": "2026.11.1"},
                "integration_manifest": {"version": "0.4.0"},
                "data": await get_diagnostics_for_config_entry(hass, hass_client, init_integration),
            }
        )
    )
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.config_entries.async_remove(init_integration.entry_id)

    monkeypatch.setattr(evidence, "HOMES", tmp_path)
    home = json.loads(evidence.add_home(evidence.load(download), "t10", "T10+").read_text())

    assert evidence.find_leaks(home) == []
    (device,) = home["data"]["devices"]
    assert sorted(name for name, _ in captured_pushes(device)) == [
        "ScheduleStatus",
        "Sensor",
        "Setpoint",
    ]
    assert device["writes"][0]["confirmation"]["success"] is True
    caplog.clear()
    second = MockConfigEntry(domain=init_integration.domain, data=dict(init_integration.data))
    await _replay(hass, second, home, caplog)
