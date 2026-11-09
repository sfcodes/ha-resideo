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
    Platform.VALVE,
]

MANUFACTURER = "Resideo"

# Repairs issue raised while Resideo's cloud is answering 503 (see availability.py). The
# translation key is this bare string; the issue *id* is suffixed with the entry id so two
# accounts get their own card.
CLOUD_UNAVAILABLE_ISSUE = "cloud_unavailable"

# --- config entry data keys --------------------------------------------------
CONF_REFRESH_TOKEN = "refresh_token"

# Config-flow-only field: the redirect the user pastes back from the browser sign-in.
CONF_REDIRECT_URL = "redirect_url"

# Reads are push-only (Azure SignalR; see coordinator.py) — there is no periodic poll interval.
