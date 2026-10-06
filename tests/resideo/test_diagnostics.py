"""Tests for the diagnostics dump."""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.resideo.aioresideo.exceptions import ResideoConnectionError

from .conftest import MAC

REDACTED = "**REDACTED**"


async def test_diagnostics(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, init_integration, mock_api: AsyncMock
) -> None:
    diag = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)

    assert diag["entry"]["data"]["refresh_token"] == REDACTED

    # Regional metadata survives — it's what unit questions (issue #7) hinge on.
    account = diag["account"]["data"]
    assert account["countryCode"] == "US"
    assert account["locale"] == "en-US"
    location = account["consumerUsers"][0]["consumerAccount"]["locations"][0]
    assert location["consumerDevices"][0]["device"]["globalDeviceType"] == "Denali_S1200"
    # ...while anything identifying the person or the household does not.
    assert account["firstName"] == REDACTED
    assert account["contactEmail"] == REDACTED
    assert location["name"] == REDACTED
    assert location["id"] == REDACTED
    assert location["consumerDevices"][0]["device"]["deviceId"] == REDACTED
    assert location["consumerAccountId"] == REDACTED
    assert location["consumerDevices"][0]["consumerDeviceLocationId"] == REDACTED

    (device,) = diag["devices"]
    assert device["shadow"]["DeviceId"] == REDACTED
    assert device["shadow"]["Reported"]["DisplayedOutdoorTemperature"] == 82.0
    assert device["configuration"]["Reported"]["TemperatureUnits"] == "F"
    assert MAC not in str(diag)


async def test_diagnostics_without_account_graph(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, init_integration, mock_api: AsyncMock
) -> None:
    """A failed /accounts fetch is reported inline and doesn't cost the device snapshots."""
    mock_api.async_get_accounts.side_effect = ResideoConnectionError("cloud unreachable")

    diag = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)

    assert diag["account"] == {"error": "cloud unreachable"}
    assert diag["devices"][0]["shadow"]["Reported"]["DisplayedIndoorTemperature"] == 76.0
