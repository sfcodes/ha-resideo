"""Stream-side tests: event parsing + location grouping (no live socket)."""

from __future__ import annotations

import json

from custom_components.resideo.aioresideo import (
    ResideoChangeConfirm,
    ResideoLiveFeed,
    parse_event,
)
from custom_components.resideo.aioresideo.client import ResideoClient


def test_parse_live_feed_setpoint(fixture_loader) -> None:
    ev = parse_event(fixture_loader("live_setpoint.json"))
    assert isinstance(ev, ResideoLiveFeed)
    assert ev.device_id == "AABBCCDDEEFF"
    assert ev.property_name == "Setpoint"
    assert ev.value["CoolSetpoint"] == 77
    assert ev.subscription_expiration == "2026-06-19T18:38:10+00:00"


def test_parse_from_raw_string(fixture_loader) -> None:
    # the hub delivers each ``events`` argument as a JSON STRING, not a dict
    ev = parse_event(json.dumps(fixture_loader("live_operation_status.json")))
    assert isinstance(ev, ResideoLiveFeed)
    assert ev.property_name == "OperationStatus"
    assert ev.value["Mode"] == "Heat"


def test_parse_change_request(fixture_loader) -> None:
    ev = parse_event(fixture_loader("change_request_success.json"))
    assert isinstance(ev, ResideoChangeConfirm)
    assert ev.success is True
    assert ev.transaction_id == "RHIE-qLo2sThYMXL"
    assert ev.change_name == "changeSetpoint"
    assert ev.change_direction == "AppInitiated"


def test_parse_garbage_returns_none() -> None:
    assert parse_event("not json") is None
    assert parse_event({"NotificationType": "SomethingElse", "Body": {}}) is None
    assert parse_event(123) is None  # type: ignore[arg-type]


def test_iter_locations(accounts) -> None:
    targets = ResideoClient.iter_locations(accounts)
    assert len(targets) == 1
    target = targets[0]
    assert target["node_id"] == (
        "Q29uc3VtZXJEZXZpY2VMb2NhdGlvbjowMDAwMDAwMC0wMDAwLTAwMDAtMDAwMC0wMDAwMDAwMDAwMDA="
    )
    assert target["name"] == "Home"
    # all devices in the location (thermostat + smoke); the caller intersects with thermostats
    assert target["device_ids"] == ["AABBCCDDEEFF", "DDEEFFAABBCC"]
