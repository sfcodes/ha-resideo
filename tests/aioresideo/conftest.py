"""Shared pytest fixtures for aioresideo tests.

Fixture JSON under ``tests/fixtures/`` is derived from live captures
(``../spikes/capture/resideo_captures/``) but **sanitized** — real MAC/serials/PII are
replaced with dummy values. ``accounts.json`` is fully synthetic.
"""

from __future__ import annotations

import json
import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def fixture_loader():
    """Return the raw fixture loader (for the many SignalR live-event fixtures)."""
    return load_fixture


@pytest.fixture
def device_shadow() -> dict:
    return load_fixture("device.json")


@pytest.fixture
def configuration() -> dict:
    return load_fixture("configuration.json")


@pytest.fixture
def accounts() -> dict:
    return load_fixture("accounts.json")


@pytest.fixture
def cool_setpoint_write() -> dict:
    return load_fixture("cool_setpoint_write.json")


@pytest.fixture
def rooms() -> dict:
    return load_fixture("rooms.json")
