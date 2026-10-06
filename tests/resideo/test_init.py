"""Setup / unload / device-removal tests."""

from __future__ import annotations

import re

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir

from custom_components.resideo import async_remove_config_entry_device
from custom_components.resideo.aioresideo.exceptions import (
    ResideoAuthError,
    ResideoConnectionError,
    ResideoUnavailableError,
)
from custom_components.resideo.const import CLOUD_UNAVAILABLE_ISSUE, DOMAIN

from .conftest import MAC, FakeStream, eid

# Verbatim from api.resideo.com, which has answered every consumer call with this since
# Resideo retired that host in Sept 2026.
RESIDEO_SAYS = "The API is temporarily down for planned maintenance. Please try again later."


def _refusing_cloud() -> ResideoUnavailableError:
    return ResideoUnavailableError(
        "GET https://api.ha.resideo.com/ris-public-api/api/v1/accounts -> 503",
        service_message=RESIDEO_SAYS,
    )


def _issue(hass: HomeAssistant, entry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"{CLOUD_UNAVAILABLE_ISSUE}_{entry.entry_id}"
    )


async def test_setup_and_unload(hass: HomeAssistant, init_integration, mock_api) -> None:
    entry = init_integration
    assert entry.state is ConfigEntryState.LOADED

    # One stream per location, connected during setup.
    assert len(mock_api.streams) == 1
    stream = mock_api.streams[0]
    assert stream.connected
    assert stream.device_ids == [MAC]  # the smoke detector was filtered out

    # A representative entity per platform exists and has state.
    assert hass.states.get(eid(hass, "climate", f"{MAC}_climate"))
    assert hass.states.get(eid(hass, "sensor", f"{MAC}_indoor_temperature"))
    assert hass.states.get(eid(hass, "binary_sensor", f"{MAC}_online"))
    assert hass.states.get(eid(hass, "switch", f"{MAC}_feels_like"))

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert stream.stopped


async def test_discovery_auth_failure_starts_reauth(
    hass: HomeAssistant, mock_config_entry, mock_api
) -> None:
    mock_api.async_get_accounts.side_effect = ResideoAuthError("token revoked")
    mock_config_entry.add_to_hass(hass)
    from unittest.mock import patch

    with patch("custom_components.resideo.Resideo", return_value=mock_api):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(flow["context"]["source"] == "reauth" for flow in flows)


async def test_no_supported_thermostats_fails_setup_listing_devices(
    hass: HomeAssistant, mock_config_entry, mock_api, accounts_data
) -> None:
    """An account with devices but no thermostats fails permanently, naming what it found."""
    location = accounts_data["data"]["consumerUsers"][0]["consumerAccount"]["locations"][0]
    location["consumerDevices"] = [
        cd
        for cd in location["consumerDevices"]
        if cd["device"]["globalDeviceType"] != "Denali_S1200"
    ]
    mock_config_entry.add_to_hass(hass)
    from unittest.mock import patch

    with patch("custom_components.resideo.Resideo", return_value=mock_api):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    assert "No supported thermostats" in mock_config_entry.reason
    assert "OneLink_SC (SmokeDetectorDevice)" in mock_config_entry.reason


async def test_empty_account_fails_setup_with_platform_hint(
    hass: HomeAssistant, mock_config_entry, mock_api, accounts_data
) -> None:
    """An account with no devices at all fails permanently with the platform hint."""
    location = accounts_data["data"]["consumerUsers"][0]["consumerAccount"]["locations"][0]
    location["consumerDevices"] = []
    mock_config_entry.add_to_hass(hass)
    from unittest.mock import patch

    with patch("custom_components.resideo.Resideo", return_value=mock_api):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    assert "No devices found" in mock_config_entry.reason
    assert "separate Resideo system" in mock_config_entry.reason


async def test_stream_connect_failure_retries_setup(
    hass: HomeAssistant, mock_config_entry, mock_api, monkeypatch
) -> None:
    async def _fail(self, timeout: float = 30.0) -> None:
        raise ResideoConnectionError("negotiate failed")

    monkeypatch.setattr(FakeStream, "async_connect_once_or_raise", _fail)
    mock_config_entry.add_to_hass(hass)
    from unittest.mock import patch

    with patch("custom_components.resideo.Resideo", return_value=mock_api):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    # The failed streams were torn down rather than leaked.
    assert all(stream.stopped for stream in mock_api.streams)


async def test_refused_cloud_retries_setup_and_raises_a_repair(
    hass: HomeAssistant, mock_config_entry, mock_api
) -> None:
    """A 503 at discovery: retry (not a permanent failure), tell the user what Resideo said,
    and raise the repair card that explains how to tell an outage from a retired endpoint."""
    mock_api.async_get_accounts.side_effect = _refusing_cloud()
    mock_config_entry.add_to_hass(hass)
    from unittest.mock import patch

    with patch("custom_components.resideo.Resideo", return_value=mock_api):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    # HA raises ConfigEntryNotReady bare from the first refresh, which would leave the card
    # blank; the integration re-raises it with the coordinator's message.
    assert mock_config_entry.reason is not None
    assert RESIDEO_SAYS in mock_config_entry.reason

    issue = _issue(hass, mock_config_entry)
    assert issue is not None
    assert issue.translation_key == CLOUD_UNAVAILABLE_ISSUE
    assert issue.translation_placeholders["message"] == RESIDEO_SAYS


def test_refusal_message_never_promises_recovery() -> None:
    """The regression this feature exists to fix.

    The first version of this told users "Home Assistant will keep retrying automatically" and
    "this issue clears itself once the API is back". Both were false — Resideo had retired the
    host, not taken it down — and that reassurance is precisely what stopped anyone looking
    further for a day and a half.

    The one-line reason states what happened and quotes Resideo; it must claim nothing about
    what happens next. (The repair description *may* say things will recover, but only inside
    the branch where the user has confirmed the phone app is broken too.)
    """
    from custom_components.resideo.availability import unavailable_reason

    reason = unavailable_reason(_refusing_cloud())
    assert RESIDEO_SAYS in reason  # Resideo's own words, verbatim
    ours = reason.replace(RESIDEO_SAYS, "").lower()  # ...and nothing of ours around them
    for promise in ("retry", "retrying", "automatically", "comes back", "clears itself"):
        assert promise not in ours


async def test_repair_text_resolves_and_sends_the_user_to_the_phone_app(
    hass: HomeAssistant, init_integration, mock_config_entry
) -> None:
    """Goes through HA's own translation loader, so it also pins the wiring.

    Home Assistant reads `translations/en.json`, never `strings.json` (hassfest validates the
    latter), so a block spliced into only one of them renders as a raw key in the UI with every
    unit test still green. And the copy itself carries the discriminator: nothing in a 503 tells
    "Resideo is down" apart from "Resideo stopped serving this address", but the First Alert app
    does, instantly. If that instruction is ever edited out, the card is unactionable again.
    """
    from homeassistant.helpers import translation

    strings = await translation.async_get_translations(hass, "en", "issues", {DOMAIN})
    prefix = f"component.{DOMAIN}.issues.{CLOUD_UNAVAILABLE_ISSUE}"
    assert strings[f"{prefix}.title"]
    description = strings[f"{prefix}.description"]

    assert "First Alert app" in description
    # Both halves of the fork, or the user can't act on the answer either way.
    assert "app is failing too" in description
    assert "app still works normally" in description

    # Every placeholder the text asks for is actually supplied at create time.
    from custom_components.resideo.availability import async_report_unavailable

    async_report_unavailable(hass, mock_config_entry, _refusing_cloud())
    supplied = _issue(hass, mock_config_entry).translation_placeholders
    assert set(re.findall(r"{(\w+)}", description)) == set(supplied)
    description.format(**supplied)  # raises if a placeholder is missing or misspelled


async def test_repair_keeps_its_original_start_time_across_failures(
    hass: HomeAssistant, mock_config_entry, mock_api
) -> None:
    """Re-reporting must not restart the clock: how long this has lasted is half of what
    tells a real outage apart from a retired endpoint."""
    from custom_components.resideo.availability import async_report_unavailable

    mock_config_entry.add_to_hass(hass)
    async_report_unavailable(hass, mock_config_entry, _refusing_cloud())
    first = _issue(hass, mock_config_entry)
    assert first is not None
    created, since = first.created, first.translation_placeholders["since"]

    async_report_unavailable(hass, mock_config_entry, _refusing_cloud())
    again = _issue(hass, mock_config_entry)
    assert again.created == created
    assert again.translation_placeholders["since"] == since


async def test_recovery_clears_the_repair(
    hass: HomeAssistant, mock_config_entry, mock_api
) -> None:
    """Once anything gets through, the card goes away on its own."""
    serving_accounts = mock_api.async_get_accounts.side_effect  # conftest's fixture-backed lambda
    mock_api.async_get_accounts.side_effect = _refusing_cloud()
    mock_config_entry.add_to_hass(hass)
    from unittest.mock import patch

    with patch("custom_components.resideo.Resideo", return_value=mock_api):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        assert _issue(hass, mock_config_entry) is not None

        mock_api.async_get_accounts.side_effect = serving_accounts
        await hass.config_entries.async_reload(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert _issue(hass, mock_config_entry) is None


async def test_unload_leaves_the_repair_standing(
    hass: HomeAssistant, mock_config_entry, mock_api
) -> None:
    """Unload must not clear it: a reload runs through unload, and deleting the issue would
    reset both the user's dismissal and the start time. Removing the entry does clear it."""
    from custom_components.resideo.availability import async_report_unavailable

    mock_config_entry.add_to_hass(hass)
    from unittest.mock import patch

    with patch("custom_components.resideo.Resideo", return_value=mock_api):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        async_report_unavailable(hass, mock_config_entry, _refusing_cloud())

        assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    assert _issue(hass, mock_config_entry) is not None

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass, mock_config_entry) is None


async def test_remove_config_entry_device(hass: HomeAssistant, init_integration) -> None:
    entry = init_integration
    registry = dr.async_get(hass)

    live = registry.async_get_device(identifiers={(DOMAIN, MAC)})
    assert live is not None
    stale = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "REMOVED_SENSOR")}
    )

    assert await async_remove_config_entry_device(hass, entry, live) is False
    assert await async_remove_config_entry_device(hass, entry, stale) is True


async def test_token_rotation_persisted(
    hass: HomeAssistant, mock_config_entry, mock_api
) -> None:
    """The token_updated callback writes a rotated refresh token back into the entry."""
    captured: dict = {}

    from unittest.mock import patch

    def _capture_resideo(session, *, refresh_token, token_updated_cb):
        captured["cb"] = token_updated_cb
        return mock_api

    with patch("custom_components.resideo.Resideo", side_effect=_capture_resideo):
        mock_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        captured["cb"]({"refresh_token": "rotated", "access_token": "x"})
        await hass.async_block_till_done()

    assert mock_config_entry.data["refresh_token"] == "rotated"
