"""Config-flow tests: login, manual token, dedupe, and reauth guarding."""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.resideo.aioresideo.exceptions import (
    ResideoAuthError,
    ResideoConnectionError,
)
from custom_components.resideo.const import CONF_REFRESH_TOKEN, DOMAIN

from .conftest import SUB


def _jwt(sub: str) -> str:
    """A structurally valid (unsigned) JWT carrying the given ``sub`` claim."""
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
    return f"{b64(b'{\"alg\":\"none\"}')}.{b64(json.dumps({'sub': sub}).encode())}."


def _tokens(sub: str = SUB) -> dict:
    return {"refresh_token": "new-refresh", "access_token": _jwt(sub)}


async def _start_login_flow(hass: HomeAssistant, source: str = "user"):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": source})
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "login"}
    )
    assert result["type"] is FlowResultType.FORM
    return result


async def test_login_creates_entry(hass: HomeAssistant) -> None:
    result = await _start_login_flow(hass)
    with (
        patch(
            "custom_components.resideo.config_flow.ResideoAuth"
        ) as mock_auth,
        patch("custom_components.resideo.async_setup_entry", return_value=True),
    ):
        mock_auth.return_value.login = AsyncMock(return_value=_tokens())
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"email": "test@example.com", "password": "hunter2"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Resideo (test@example.com)"
    assert result["data"] == {CONF_REFRESH_TOKEN: "new-refresh"}
    assert result["result"].unique_id == SUB


@pytest.mark.parametrize(
    ("raised", "error"),
    [
        (ResideoAuthError("wrong password"), "invalid_auth"),
        (ResideoConnectionError("timeout"), "cannot_connect"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_login_errors_then_recovers(
    hass: HomeAssistant, raised: Exception, error: str
) -> None:
    result = await _start_login_flow(hass)
    with patch("custom_components.resideo.config_flow.ResideoAuth") as mock_auth:
        mock_auth.return_value.login = AsyncMock(side_effect=raised)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"email": "test@example.com", "password": "nope"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    # The same flow recovers on a subsequent valid submission.
    with (
        patch("custom_components.resideo.config_flow.ResideoAuth") as mock_auth,
        patch("custom_components.resideo.async_setup_entry", return_value=True),
    ):
        mock_auth.return_value.login = AsyncMock(return_value=_tokens())
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"email": "test@example.com", "password": "hunter2"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_login_without_refresh_token_aborts(hass: HomeAssistant) -> None:
    result = await _start_login_flow(hass)
    with patch("custom_components.resideo.config_flow.ResideoAuth") as mock_auth:
        mock_auth.return_value.login = AsyncMock(return_value={"access_token": _jwt(SUB)})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"email": "test@example.com", "password": "hunter2"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_token"


def _mock_manual_api(sub: str = SUB, rotated: str = "rotated-refresh") -> MagicMock:
    api = MagicMock()
    api.client.async_ensure_token = AsyncMock(return_value="at")
    api.refresh_token = rotated
    api.tokens = {"access_token": _jwt(sub), "refresh_token": rotated}
    return api


async def test_manual_token_creates_entry_with_rotated_token(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "manual"}
    )
    assert result["type"] is FlowResultType.FORM
    with (
        patch(
            "custom_components.resideo.config_flow.Resideo",
            return_value=_mock_manual_api(),
        ),
        patch("custom_components.resideo.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REFRESH_TOKEN: "pasted-token"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The validation refresh rotated the token; the rotated one must be stored.
    assert result["data"] == {CONF_REFRESH_TOKEN: "rotated-refresh"}
    assert result["result"].unique_id == SUB


async def test_manual_token_invalid(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "manual"}
    )
    api = _mock_manual_api()
    api.client.async_ensure_token = AsyncMock(side_effect=ResideoAuthError("revoked"))
    with patch("custom_components.resideo.config_flow.Resideo", return_value=api):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REFRESH_TOKEN: "bad-token"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_duplicate_account_aborts(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)  # existing entry with unique_id == SUB
    result = await _start_login_flow(hass)
    with patch("custom_components.resideo.config_flow.ResideoAuth") as mock_auth:
        mock_auth.return_value.login = AsyncMock(return_value=_tokens())
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"email": "test@example.com", "password": "hunter2"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def _start_reauth(hass: HomeAssistant, entry: MockConfigEntry):
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "reauth_confirm"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "login"}
    )


async def test_reauth_updates_entry(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await _start_reauth(hass, mock_config_entry)
    with (
        patch("custom_components.resideo.config_flow.ResideoAuth") as mock_auth,
        patch("custom_components.resideo.async_setup_entry", return_value=True),
    ):
        mock_auth.return_value.login = AsyncMock(return_value=_tokens())
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"email": "test@example.com", "password": "hunter2"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_REFRESH_TOKEN] == "new-refresh"


async def test_reauth_with_other_account_aborts(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await _start_reauth(hass, mock_config_entry)
    with patch("custom_components.resideo.config_flow.ResideoAuth") as mock_auth:
        mock_auth.return_value.login = AsyncMock(return_value=_tokens(sub="auth0|intruder"))
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"email": "other@example.com", "password": "hunter2"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_account_mismatch"
    assert mock_config_entry.data[CONF_REFRESH_TOKEN] == "refresh-token"  # untouched


async def test_reauth_adopts_unique_id_on_legacy_entry(hass: HomeAssistant) -> None:
    """Entries created before unique_id existed adopt it on their first re-auth."""
    legacy = MockConfigEntry(
        domain=DOMAIN, title="Resideo", data={CONF_REFRESH_TOKEN: "old"}, unique_id=None
    )
    legacy.add_to_hass(hass)
    result = await _start_reauth(hass, legacy)
    with (
        patch("custom_components.resideo.config_flow.ResideoAuth") as mock_auth,
        patch("custom_components.resideo.async_setup_entry", return_value=True),
    ):
        mock_auth.return_value.login = AsyncMock(return_value=_tokens())
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"email": "test@example.com", "password": "hunter2"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert legacy.unique_id == SUB
    assert legacy.data[CONF_REFRESH_TOKEN] == "new-refresh"
