"""Config flow for the Resideo integration.

Three auth paths against the consumer API (no developer account):
  - ``login``   — email/password via ``aioresideo.ResideoAuth`` (Auth0), headless.
  - ``browser`` — open Auth0's own sign-in page, paste the redirect back. The only path
    that survives a bot-detection CAPTCHA, since a human drives the page.
  - ``manual``  — paste a refresh token grabbed by proxying ``login.resideo.com``.

Every failure returns to a menu of all three paths rather than re-showing the form that
just failed: a form is a dead end in the HA frontend (no back button), so a CAPTCHA on the
direct login would otherwise strand the user on the one path that cannot succeed.

Entries are deduped by the Auth0 ``sub`` claim of the access token (the account identity),
which also guards re-auth against silently rewiring an entry to a different account.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .aioresideo import (
    AuthorizeRequest,
    Resideo,
    ResideoAuth,
    build_authorize_url,
    decode_jwt_claims,
    parse_authorize_redirect,
)
from .aioresideo.exceptions import (
    ResideoAuthError,
    ResideoConnectionError,
    ResideoError,
)
from .const import CONF_REDIRECT_URL, CONF_REFRESH_TOKEN, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_LOGIN_SCHEMA = vol.Schema(
    {vol.Required(CONF_EMAIL): str, vol.Required(CONF_PASSWORD): str}
)
STEP_MANUAL_SCHEMA = vol.Schema({vol.Required(CONF_REFRESH_TOKEN): str})
STEP_BROWSER_SCHEMA = vol.Schema({vol.Required(CONF_REDIRECT_URL): str})

AUTH_MENU = ["login", "browser", "manual"]
# When Resideo itself is refusing the direct login, lead with the path that still works.
BLOCKED_MENU = ["browser", "manual", "login"]
BLOCKED_KEYS = {"captcha_required", "too_many_attempts"}
# Resideo's guide to moving from the old (Lyric) app to First Alert. A placeholder rather
# than part of strings.json, because hassfest rejects URLs in translation strings.
MIGRATION_GUIDE_URL = (
    "https://www.resideo.com/us/en/support/if-i-switch-from-the-resideo-app-to-the-first-alert-app-what-will-happen-to-my-current-integrations/"
)

# Every failure that gets a menu of its own. Each needs an ``async_step_<key>`` handler and a
# matching block in strings.json — see ``_auth_menu`` for why both are mandatory.
FAILURE_MENUS = (
    "invalid_auth",
    "invalid_token",
    "captcha_required",
    "too_many_attempts",
    "login_failed",
    "invalid_code",
    "state_mismatch",
    "cannot_connect",
    "unknown",
)

# Auth0 error code -> failure-menu key. Anything unrecognised (including a failure in the
# login flow's own mechanics) falls to ``login_failed`` rather than blaming the credentials.
_AUTH_ERROR_KEYS = {
    "invalid_credentials": "invalid_auth",
    "invalid_captcha": "captcha_required",
    "too_many_attempts": "too_many_attempts",
    "blocked_user": "too_many_attempts",
    # Browser path: the paste itself was wrong, not the account.
    "invalid_code": "invalid_code",
    "state_mismatch": "state_mismatch",
}


def _auth_error(err: ResideoAuthError, *, token_path: bool = False) -> tuple[str, str]:
    """Map an auth failure to a ``(menu key, detail)`` pair.

    ``detail`` is interpolated into the message so a screenshot of the menu is enough to
    diagnose the failure — the whole point being that "Invalid authentication" alone is not.
    """
    key = _AUTH_ERROR_KEYS.get(err.code or "", "login_failed")
    if token_path and key in ("invalid_auth", "login_failed"):
        # A pasted refresh token was rejected; nothing to say about an email/password.
        key = "invalid_token"
    detail = ": ".join(part for part in (err.step, err.code) if part) or str(err)
    return key, detail


class ResideoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Resideo."""

    VERSION = 1

    def __init__(self) -> None:
        # Held between the two halves of the browser step: the user leaves to sign in,
        # and the PKCE verifier + state must still be here when they paste the redirect.
        self._authorize: AuthorizeRequest | None = None
        # Last failure's detail, so a failure menu can re-render after a page reload.
        self._detail: str = ""
        # Last email tried, so picking "try again" does not make the user retype it.
        self._email: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """First step: pick how to authenticate."""
        return self.async_show_menu(step_id="user", menu_options=AUTH_MENU)

    async def async_step_login(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Email/password login via aioresideo (Auth0)."""
        if user_input is not None:
            self._email = user_input[CONF_EMAIL]
            session = async_get_clientsession(self.hass)
            try:
                tokens = await ResideoAuth(session).login(
                    user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
                )
            except ResideoAuthError as err:
                key, detail = _auth_error(err)
                _LOGGER.debug("Resideo login failed (%s): %s", key, err)
                return self._auth_menu(key, detail)
            except (ResideoConnectionError, ResideoError):
                return self._auth_menu("cannot_connect")
            except Exception:
                _LOGGER.exception("Unexpected error during Resideo login")
                return self._auth_menu("unknown")
            return await self._finish(
                tokens.get("refresh_token"),
                access_token=tokens.get("access_token"),
                email=user_input[CONF_EMAIL],
            )
        return self.async_show_form(
            step_id="login",
            data_schema=self.add_suggested_values_to_schema(
                STEP_LOGIN_SCHEMA, {CONF_EMAIL: self._email} if self._email else None
            ),
        )

    async def async_step_browser(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Sign in via Auth0's own page in the user's browser, then paste the redirect back.

        Auth0's bot detection can demand a CAPTCHA that a headless client cannot solve; here
        the human drives the page, so the CAPTCHA is simply part of signing in. The redirect
        lands on the mobile app's custom scheme, which the browser cannot open — the URL is
        still in the address bar, and that is what gets pasted.
        """
        if self._authorize is None:
            self._authorize = build_authorize_url()
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            try:
                code = parse_authorize_redirect(
                    user_input[CONF_REDIRECT_URL], self._authorize.state
                )
                tokens = await ResideoAuth(session).exchange_code(
                    code, self._authorize.code_verifier
                )
            except ResideoAuthError as err:
                key, detail = _auth_error(err)
                _LOGGER.debug("Resideo browser sign-in failed (%s): %s", key, err)
                # A spent or mismatched code can't be retried — start a fresh authorization.
                self._authorize = build_authorize_url()
                return self._auth_menu(key, detail)
            except (ResideoConnectionError, ResideoError):
                return self._auth_menu("cannot_connect")
            except Exception:
                _LOGGER.exception("Unexpected error during Resideo browser sign-in")
                return self._auth_menu("unknown")
            return await self._finish(
                tokens.get("refresh_token"), access_token=tokens.get("access_token")
            )
        return self.async_show_form(
            step_id="browser",
            data_schema=STEP_BROWSER_SCHEMA,
            description_placeholders={"url": self._authorize.url},
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manual refresh-token entry."""
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            refresh_token = user_input[CONF_REFRESH_TOKEN]
            api = Resideo(session, refresh_token=refresh_token)
            try:
                await api.client.async_ensure_token()  # validate the token works
            except ResideoAuthError as err:
                key, detail = _auth_error(err, token_path=True)
                _LOGGER.debug("Resideo token validation failed: %s", err)
                return self._auth_menu(key, detail)
            except (ResideoConnectionError, ResideoError):
                return self._auth_menu("cannot_connect")
            except Exception:
                _LOGGER.exception("Unexpected error validating a Resideo refresh token")
                return self._auth_menu("unknown")
            # The refresh may have rotated the token; persist the latest one.
            return await self._finish(
                api.refresh_token or refresh_token,
                access_token=api.tokens.get("access_token"),
            )
        return self.async_show_form(
            step_id="manual",
            data_schema=STEP_MANUAL_SCHEMA,
        )

    def _auth_menu(self, key: str, detail: str = "") -> ConfigFlowResult:
        """Offer the three auth paths again, explaining what just failed.

        The ``step_id`` is the failure key, so every failure gets wording of its own — and
        a handler of its own, because HA re-enters a menu by its step id when the frontend
        re-reads the flow (a page reload), and an unknown step id aborts the flow.
        """
        self._detail = detail
        return self.async_show_menu(
            step_id=key,
            menu_options=BLOCKED_MENU if key in BLOCKED_KEYS else AUTH_MENU,
            description_placeholders={
                "detail": detail,
                "migration_url": MIGRATION_GUIDE_URL,
            },
        )

    # --- Failure menus: one handler per key in ``FAILURE_MENUS``, each existing so that a
    # reload of its menu re-renders it instead of aborting the flow.

    async def async_step_invalid_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Resideo rejected that email and password."""
        return self._auth_menu("invalid_auth", self._detail)

    async def async_step_invalid_token(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Resideo rejected the pasted refresh token."""
        return self._auth_menu("invalid_token", self._detail)

    async def async_step_captcha_required(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Auth0 demanded a CAPTCHA; only the browser path can clear it."""
        return self._auth_menu("captcha_required", self._detail)

    async def async_step_too_many_attempts(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Auth0 has temporarily blocked this account or address."""
        return self._auth_menu("too_many_attempts", self._detail)

    async def async_step_login_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The sign-in broke somewhere that is not the credentials."""
        return self._auth_menu("login_failed", self._detail)

    async def async_step_invalid_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The pasted redirect carried no sign-in code."""
        return self._auth_menu("invalid_code", self._detail)

    async def async_step_state_mismatch(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The pasted redirect belongs to an earlier authorization."""
        return self._auth_menu("state_mismatch", self._detail)

    async def async_step_cannot_connect(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Resideo could not be reached at all."""
        return self._auth_menu("cannot_connect", self._detail)

    async def async_step_unknown(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Something unforeseen went wrong; the traceback is in the log."""
        return self._auth_menu("unknown", self._detail)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Re-authenticate an existing entry (token expired/revoked)."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick how to re-authenticate (same paths as setup)."""
        return self.async_show_menu(step_id="reauth_confirm", menu_options=AUTH_MENU)

    async def _finish(
        self,
        refresh_token: str | None,
        *,
        access_token: str | None = None,
        email: str | None = None,
    ) -> ConfigFlowResult:
        """Create the entry, or update it in place when re-authenticating."""
        if not refresh_token:
            return self.async_abort(reason="no_token")
        # The Auth0 ``sub`` claim identifies the account (no extra API call needed).
        sub = (decode_jwt_claims(access_token) or {}).get("sub")
        if sub:
            await self.async_set_unique_id(sub)
        else:
            _LOGGER.debug("Access token carried no decodable `sub`; skipping unique_id")
        if self.source == SOURCE_REAUTH:
            reauth_entry = self._get_reauth_entry()
            if sub and reauth_entry.unique_id:
                self._abort_if_unique_id_mismatch(reason="reauth_account_mismatch")
            if sub:
                # Adopt the unique_id on entries created before it was recorded.
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    unique_id=sub,
                    data_updates={CONF_REFRESH_TOKEN: refresh_token},
                )
            return self.async_update_reload_and_abort(
                reauth_entry,
                data_updates={CONF_REFRESH_TOKEN: refresh_token},
            )
        if sub:
            self._abort_if_unique_id_configured()
        title = f"Resideo ({email})" if email else "Resideo"
        return self.async_create_entry(title=title, data={CONF_REFRESH_TOKEN: refresh_token})
