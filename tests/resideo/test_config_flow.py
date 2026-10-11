"""Config-flow tests: login, manual token, dedupe, failure menus, and reauth guarding."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.resideo.aioresideo.const import REDIRECT_URI
from custom_components.resideo.aioresideo.exceptions import (
    ResideoAuthError,
    ResideoConnectionError,
)
from custom_components.resideo.config_flow import (
    _AUTH_ERROR_KEYS,
    FAILURE_MENUS,
    MIGRATION_GUIDE_URL,
    ResideoConfigFlow,
)
from custom_components.resideo.const import (
    CONF_REDIRECT_URL,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)

from .conftest import SUB

_COMPONENT = Path(__file__).parents[2] / "custom_components" / "resideo"


def test_every_menu_has_a_handler_and_complete_strings() -> None:
    """A menu with no handler aborts the flow on a page reload; one with no strings renders raw ids.

    Neither shows up in a flow test that never reloads, so assert the invariants directly.
    """
    strings = json.loads((_COMPONENT / "strings.json").read_text())["config"]["step"]
    assert set(_AUTH_ERROR_KEYS.values()) <= set(FAILURE_MENUS)

    for menu in (*FAILURE_MENUS, "user", "reauth_confirm"):
        assert hasattr(ResideoConfigFlow, f"async_step_{menu}"), f"{menu} has no handler"
        block = strings.get(menu)
        assert block, f"{menu} has no strings"
        assert block.get("title") and block.get("description")
        assert set(block["menu_options"]) == {"login", "browser", "manual"}
        # Only `detail` and `migration_url` are ever passed, and only by the failure menus.
        placeholders = set(re.findall(r"\{(\w+)\}", block["description"]))
        allowed = {"detail", "migration_url"} if menu in FAILURE_MENUS else set()
        assert placeholders <= allowed


def test_translations_match_strings() -> None:
    """en.json is the shipped copy of strings.json; a drifting copy silently wins in the UI."""
    assert (_COMPONENT / "translations" / "en.json").read_text() == (
        _COMPONENT / "strings.json"
    ).read_text()


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
        # Only a genuinely rejected credential may be reported as bad credentials.
        (
            ResideoAuthError("nope", step="credentials", code="invalid_credentials"),
            "invalid_auth",
        ),
        # Auth0 bot detection rejects the request before credentials are evaluated.
        (
            ResideoAuthError("captcha", step="credentials", code="invalid_captcha"),
            "captcha_required",
        ),
        (
            ResideoAuthError("slow down", step="credentials", code="too_many_attempts"),
            "too_many_attempts",
        ),
        (ResideoAuthError("blocked", code="blocked_user"), "too_many_attempts"),
        # A break in the flow's own mechanics is not the user's password.
        (ResideoAuthError("no `_csrf` cookie", step="login-page"), "login_failed"),
        (ResideoAuthError("bare"), "login_failed"),
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
    # Every failure lands on a menu of all three paths, never back on the dead-end form.
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == error
    # The menu always carries a `detail` placeholder; the descriptions interpolate it.
    assert "detail" in result["description_placeholders"]
    assert set(result["menu_options"]) == {"login", "browser", "manual"}

    # The same flow recovers by picking email/password again and submitting valid ones.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "login"}
    )
    assert result["type"] is FlowResultType.FORM
    with (
        patch("custom_components.resideo.config_flow.ResideoAuth") as mock_auth,
        patch("custom_components.resideo.async_setup_entry", return_value=True),
    ):
        mock_auth.return_value.login = AsyncMock(return_value=_tokens())
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"email": "test@example.com", "password": "hunter2"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_captcha_menu_offers_the_browser_path(hass: HomeAssistant) -> None:
    """The reported bug: a CAPTCHA must not strand the user on the one path that can't work."""
    result = await _start_login_flow(hass)
    with patch("custom_components.resideo.config_flow.ResideoAuth") as mock_auth:
        mock_auth.return_value.login = AsyncMock(
            side_effect=ResideoAuthError("captcha", step="credentials", code="invalid_captcha")
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"email": "test@example.com", "password": "hunter2"}
        )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "captcha_required"
    # The path that survives a CAPTCHA is offered first — offered, not forced.
    assert next(iter(result["menu_options"])) == "browser"

    # Switching to it mid-flow now works, which is what was impossible before.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "browser"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "browser"
    state = parse_qs(urlparse(result["description_placeholders"]["url"]).query)["state"][0]
    with (
        patch("custom_components.resideo.config_flow.ResideoAuth") as mock_auth,
        patch("custom_components.resideo.async_setup_entry", return_value=True),
    ):
        mock_auth.return_value.exchange_code = AsyncMock(return_value=_tokens())
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_REDIRECT_URL: f"{REDIRECT_URI}?code=the-code&state={state}"},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_REFRESH_TOKEN: "new-refresh"}


async def test_failure_menu_survives_a_page_reload(hass: HomeAssistant) -> None:
    """Re-reading an in-progress flow re-enters a menu by its step id, so each needs a handler."""
    result = await _start_login_flow(hass)
    with patch("custom_components.resideo.config_flow.ResideoAuth") as mock_auth:
        mock_auth.return_value.login = AsyncMock(
            side_effect=ResideoAuthError("nope", step="credentials", code="invalid_credentials")
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"email": "test@example.com", "password": "nope"}
        )
    assert result["step_id"] == "invalid_auth"

    # What the frontend does on a reload: GET the flow, i.e. configure with no input.
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "invalid_auth"
    assert result["description_placeholders"]["detail"] == "credentials: invalid_credentials"
    assert result["description_placeholders"]["migration_url"] == MIGRATION_GUIDE_URL


async def test_login_form_remembers_the_email_on_retry(hass: HomeAssistant) -> None:
    """A failed attempt costs a click to retry; it must not also cost retyping the email."""
    result = await _start_login_flow(hass)
    with patch("custom_components.resideo.config_flow.ResideoAuth") as mock_auth:
        mock_auth.return_value.login = AsyncMock(
            side_effect=ResideoAuthError("nope", step="credentials", code="invalid_credentials")
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"email": "test@example.com", "password": "typo"}
        )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "login"}
    )
    suggested = {
        key.schema: key.description["suggested_value"]
        for key in result["data_schema"].schema
        if key.description
    }
    assert suggested == {"email": "test@example.com"}


async def test_login_without_refresh_token_aborts(hass: HomeAssistant) -> None:
    result = await _start_login_flow(hass)
    with patch("custom_components.resideo.config_flow.ResideoAuth") as mock_auth:
        mock_auth.return_value.login = AsyncMock(return_value={"access_token": _jwt(SUB)})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"email": "test@example.com", "password": "hunter2"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_token"


async def _start_browser_flow(hass: HomeAssistant, source: str = "user"):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": source})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "browser"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "browser"
    return result


async def test_browser_flow_shows_an_authorize_url_then_creates_entry(
    hass: HomeAssistant,
) -> None:
    """The user opens the URL, signs in (solving any CAPTCHA), and pastes the redirect."""
    result = await _start_browser_flow(hass)
    url = result["description_placeholders"]["url"]
    assert url.startswith("https://login.resideo.com/authorize?")
    state = parse_qs(urlparse(url).query)["state"][0]

    with (
        patch("custom_components.resideo.config_flow.ResideoAuth") as mock_auth,
        patch("custom_components.resideo.async_setup_entry", return_value=True),
    ):
        mock_auth.return_value.exchange_code = AsyncMock(return_value=_tokens())
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_REDIRECT_URL: f"{REDIRECT_URI}?code=the-code&state={state}"},
        )
        # The code is exchanged with the verifier from the URL we handed out.
        mock_auth.return_value.exchange_code.assert_awaited_once()
        assert mock_auth.return_value.exchange_code.await_args.args[0] == "the-code"

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_REFRESH_TOKEN: "new-refresh"}
    assert result["result"].unique_id == SUB


async def test_browser_flow_rejects_a_redirect_from_another_attempt(
    hass: HomeAssistant,
) -> None:
    result = await _start_browser_flow(hass)
    first_url = result["description_placeholders"]["url"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REDIRECT_URL: f"{REDIRECT_URI}?code=c&state=stale"}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "state_mismatch"

    # Starting the browser sign-in again issues a fresh authorization, since the old one
    # can no longer be completed.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "browser"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["description_placeholders"]["url"] != first_url


async def test_browser_flow_rejects_a_paste_with_no_code(hass: HomeAssistant) -> None:
    result = await _start_browser_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REDIRECT_URL: "https://login.resideo.com/?foo=bar"}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "invalid_code"


async def test_browser_flow_reauth_updates_entry(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Re-auth via the browser path — the escape hatch when a token dies behind a CAPTCHA."""
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "browser"}
    )
    state = parse_qs(urlparse(result["description_placeholders"]["url"]).query)["state"][0]
    with (
        patch("custom_components.resideo.config_flow.ResideoAuth") as mock_auth,
        patch("custom_components.resideo.async_setup_entry", return_value=True),
    ):
        mock_auth.return_value.exchange_code = AsyncMock(return_value=_tokens())
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REDIRECT_URL: f"{REDIRECT_URI}?code=c&state={state}"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_REFRESH_TOKEN] == "new-refresh"


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
    api.client.async_ensure_token = AsyncMock(
        side_effect=ResideoAuthError(
            "revoked", step="refresh", status=401, code="invalid_credentials"
        )
    )
    with patch("custom_components.resideo.config_flow.Resideo", return_value=api):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REFRESH_TOKEN: "bad-token"}
        )
    assert result["type"] is FlowResultType.MENU
    # A pasted token was rejected — never phrase that as a bad email/password.
    assert result["step_id"] == "invalid_token"
    assert result["description_placeholders"]["detail"] == "refresh: invalid_credentials"


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


async def test_reauth_failure_menu_routes_to_the_browser_path(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Re-auth is the worse dead end — dismissing it means going back via the repair."""
    mock_config_entry.add_to_hass(hass)
    result = await _start_reauth(hass, mock_config_entry)
    with patch("custom_components.resideo.config_flow.ResideoAuth") as mock_auth:
        mock_auth.return_value.login = AsyncMock(
            side_effect=ResideoAuthError("captcha", step="credentials", code="invalid_captcha")
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"email": "test@example.com", "password": "hunter2"}
        )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "captcha_required"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "browser"}
    )
    state = parse_qs(urlparse(result["description_placeholders"]["url"]).query)["state"][0]
    with (
        patch("custom_components.resideo.config_flow.ResideoAuth") as mock_auth,
        patch("custom_components.resideo.async_setup_entry", return_value=True),
    ):
        mock_auth.return_value.exchange_code = AsyncMock(return_value=_tokens())
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REDIRECT_URL: f"{REDIRECT_URI}?code=c&state={state}"}
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
