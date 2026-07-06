"""Config flow for the Resideo integration.

Two auth paths against the consumer API (no developer account):
  - ``login``  — email/password via ``aioresideo.ResideoAuth`` (Auth0).
  - ``manual`` — paste a refresh token grabbed by proxying ``login.resideo.com``.

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

from .aioresideo import Resideo, ResideoAuth, decode_jwt_claims
from .aioresideo.exceptions import (
    ResideoAuthError,
    ResideoConnectionError,
    ResideoError,
)
from .const import CONF_REFRESH_TOKEN, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_LOGIN_SCHEMA = vol.Schema(
    {vol.Required(CONF_EMAIL): str, vol.Required(CONF_PASSWORD): str}
)
STEP_MANUAL_SCHEMA = vol.Schema({vol.Required(CONF_REFRESH_TOKEN): str})


class ResideoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Resideo."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """First step: pick how to authenticate."""
        return self.async_show_menu(step_id="user", menu_options=["login", "manual"])

    async def async_step_login(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Email/password login via aioresideo (Auth0)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            try:
                tokens = await ResideoAuth(session).login(
                    user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
                )
            except ResideoAuthError:
                errors["base"] = "invalid_auth"
            except (ResideoConnectionError, ResideoError):
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during Resideo login")
                errors["base"] = "unknown"
            else:
                return await self._finish(
                    tokens.get("refresh_token"),
                    access_token=tokens.get("access_token"),
                    email=user_input[CONF_EMAIL],
                )
        return self.async_show_form(
            step_id="login", data_schema=STEP_LOGIN_SCHEMA, errors=errors
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manual refresh-token entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            refresh_token = user_input[CONF_REFRESH_TOKEN]
            api = Resideo(session, refresh_token=refresh_token)
            try:
                await api.client.async_ensure_token()  # validate the token works
            except ResideoAuthError:
                errors["base"] = "invalid_auth"
            except (ResideoConnectionError, ResideoError):
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error validating a Resideo refresh token")
                errors["base"] = "unknown"
            else:
                # The refresh may have rotated the token; persist the latest one.
                return await self._finish(
                    api.refresh_token or refresh_token,
                    access_token=api.tokens.get("access_token"),
                )
        return self.async_show_form(
            step_id="manual", data_schema=STEP_MANUAL_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Re-authenticate an existing entry (token expired/revoked)."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick how to re-authenticate (same two paths as setup)."""
        return self.async_show_menu(
            step_id="reauth_confirm", menu_options=["login", "manual"]
        )

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
