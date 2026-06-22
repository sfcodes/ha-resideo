"""Exceptions raised by aioresideo."""

from __future__ import annotations

from typing import Any


class ResideoError(Exception):
    """Base error for all aioresideo failures."""


class ResideoAuthError(ResideoError):
    """Authentication / token failure (HTTP 401, invalid credentials, expired refresh token)."""


class ResideoConnectionError(ResideoError):
    """Network/transport error talking to api.resideo.com (timeouts, DNS, TLS, ...)."""


class ResideoApiError(ResideoError):
    """A non-2xx API response that is not an authentication failure.

    Carries the HTTP ``status`` and parsed ``body`` (when available) for diagnostics.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
