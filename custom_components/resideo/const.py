"""Constants for the Resideo (consumer API) integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "resideo"

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

MANUFACTURER = "Resideo"

# --- config entry data keys --------------------------------------------------
CONF_REFRESH_TOKEN = "refresh_token"
CONF_ACCESS_TOKEN = "access_token"
CONF_TOKEN_EXPIRES_AT = "token_expires_at"
CONF_EMAIL = "email"

# Reads are push-only (Azure SignalR; see coordinator.py) — there is no periodic poll interval.
