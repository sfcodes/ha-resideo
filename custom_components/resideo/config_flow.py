"""Config flow for the Resideo integration.

Two auth paths against the consumer API (no developer account):
  - ``login``  — email/password via ``aioresideo.ResideoAuth`` (Auth0).
  - ``manual`` — paste a refresh token grabbed by proxying ``login.resideo.com``.
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

from .aioresideo import Resideo, ResideoAuth
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
            else:
                return await self._finish(
                    tokens.get("refresh_token"), email=user_input[CONF_EMAIL]
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
            try:
                api = Resideo(session, refresh_token=refresh_token)
                await api.client.async_ensure_token()  # validate the token works
            except ResideoAuthError:
                errors["base"] = "invalid_auth"
            except (ResideoConnectionError, ResideoError):
                errors["base"] = "cannot_connect"
            else:
                return await self._finish(refresh_token)
        return self.async_show_form(
            step_id="manual", data_schema=STEP_MANUAL_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Re-authenticate an existing entry (token expired/revoked)."""
        return await self.async_step_login()

    async def _finish(
        self, refresh_token: str | None, email: str | None = None
    ) -> ConfigFlowResult:
        """Create the entry, or update it in place when re-authenticating."""
        if not refresh_token:
            return self.async_abort(reason="no_token")
        # TODO: set unique_id to the account id (from get_accounts) to dedupe accounts.
        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(),
                data_updates={CONF_REFRESH_TOKEN: refresh_token},
            )
        title = f"Resideo ({email})" if email else "Resideo"
        return self.async_create_entry(title=title, data={CONF_REFRESH_TOKEN: refresh_token})
