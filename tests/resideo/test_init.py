"""Setup / unload / device-removal tests."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.resideo import async_remove_config_entry_device
from custom_components.resideo.aioresideo.exceptions import (
    ResideoAuthError,
    ResideoConnectionError,
)
from custom_components.resideo.const import DOMAIN

from .conftest import MAC, FakeStream, eid


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
    mock_api.async_get_thermostats.side_effect = ResideoAuthError("token revoked")
    mock_config_entry.add_to_hass(hass)
    from unittest.mock import patch

    with patch("custom_components.resideo.Resideo", return_value=mock_api):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(flow["context"]["source"] == "reauth" for flow in flows)


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
