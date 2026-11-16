"""Shared pytest fixtures for aioresideo tests.

Fixture JSON under ``tests/fixtures/`` is derived from live captures
(``../spikes/capture/resideo_captures/``) but **sanitized** — real MAC/serials/PII are
replaced with dummy values. ``accounts.json`` is fully synthetic.
"""

from __future__ import annotations

import inspect
import json
import pathlib
from unittest.mock import Mock

import aiohttp
import aioresponses.core
import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


class _ClientResponse(aiohttp.ClientResponse):
    """``ClientResponse`` as aiohttp 3.14 needs ``aioresponses`` to build it."""

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("stream_writer", Mock(output_size=0))
        super().__init__(*args, **kwargs)


@pytest.fixture(autouse=True)
def aioresponses_builds_responses_on_aiohttp_3_14(monkeypatch) -> None:
    """Let ``aioresponses`` 0.7.9 build responses on aiohttp 3.14.

    aiohttp 3.14 made ``stream_writer`` a required argument of ``ClientResponse``, read only
    for its ``output_size``. The released ``aioresponses`` doesn't pass it; the fix is the
    unmerged pnuckowski/aioresponses#288. Remove this once a release carries it.
    """
    if "stream_writer" in inspect.signature(aiohttp.ClientResponse).parameters:
        monkeypatch.setattr(aioresponses.core, "ClientResponse", _ClientResponse)


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
