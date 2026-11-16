"""Auth0 authentication for the Resideo consumer API.

Async (aiohttp) port of the proven flow in ``spikes/resideo_consumer.py`` — the Auth0
"classic" Universal Login + WS-Fed callback dance, plus refresh-token exchange. Uses the
First Alert app's own public Auth0 client, so no developer account is needed.

Login flow (6 steps), see ``resideo-reverse-engineering.md`` §4B:
  1. GET  /authorize          -> redirect carrying Auth0 ``state``
  2. GET  /login?state=...    -> sets the ``_csrf`` cookie
  3. POST /usernamepassword/login (JSON creds) -> HTML form w/ ``wresult``/``wctx``
  4. POST /login/callback     (wresult/wctx)    -> redirect to a resume URL
  5. GET  <resume>            -> redirect to redirect_uri?code=...
  6. POST /oauth/token        (code + PKCE verifier) -> tokens
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp

from .const import (
    AUDIENCE,
    AUTH0_AUTHORIZE_URL,
    AUTH0_BASE_URL,
    AUTH0_CALLBACK_URL,
    AUTH0_CLIENT_APP,
    AUTH0_CLIENT_BROWSER,
    AUTH0_LOGIN_PAGE_URL,
    AUTH0_LOGIN_URL,
    CONNECTION,
    OAUTH_CLIENT_ID,
    OAUTH_TOKEN_URL,
    REDIRECT_URI,
    SCOPE,
    SIGN_UP_URL,
    TENANT,
    WEB_USER_AGENT,
)
from .exceptions import ResideoAuthError, ResideoConnectionError

# Normalized Auth0 error codes the callers act on; every other code is passed through
# verbatim (``invalid_captcha``, ``too_many_attempts``, ``blocked_user``, ``mfa_required``, ...).
ERROR_INVALID_CREDENTIALS = "invalid_credentials"

# Auth0 spells "those credentials were rejected" differently per endpoint and flow.
_CREDENTIAL_CODES = frozenset({"invalid_user_password", "invalid_grant", "invalid_credentials"})


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _auth0_error(body: str) -> tuple[str | None, str | None]:
    """Extract a normalized ``(code, description)`` from an Auth0 error response body.

    Auth0 answers ``/usernamepassword/login`` with JSON such as ``{"code": "invalid_captcha",
    "description": "Invalid captcha value"}`` — bot detection rejects the request *before*
    credentials are evaluated, so a captcha block must not be reported as a bad password. The
    classic Universal Login can also hand back an HTML error page, hence the substring fallback.
    """
    code: str | None = None
    description: str | None = None
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        raw_code = payload.get("code") or payload.get("name") or payload.get("error")
        raw_desc = payload.get("description") or payload.get("error_description")
        code = str(raw_code) if raw_code else None
        description = str(raw_desc) if raw_desc else None
    if code in _CREDENTIAL_CODES:
        code = ERROR_INVALID_CREDENTIALS
    elif code is None and ("Wrong email or password" in body or "invalid_grant" in body):
        code = ERROR_INVALID_CREDENTIALS
    return code, description


def decode_jwt_claims(token: str | None) -> dict[str, Any] | None:
    """Best-effort decode of a JWT payload (NOT cryptographically verified)."""
    if not token or token.count(".") < 2:
        return None
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None


@dataclass(frozen=True, slots=True)
class AuthorizeRequest:
    """A pending browser authorization: where to send the user, and what to check on return.

    ``code_verifier`` and ``state`` must survive until the user pastes the redirect back,
    so the caller has to hold this between config-flow steps.
    """

    url: str
    code_verifier: str
    state: str


def build_authorize_url() -> AuthorizeRequest:
    """Build a PKCE ``/authorize`` URL for the user to open in their own browser.

    Same parameters the headless flow uses, so Auth0 behaves identically — the difference
    is only who drives the page. A human can satisfy a bot-detection CAPTCHA; a
    ``ClientSession`` cannot, which is the whole reason this path exists.
    """
    code_verifier = _b64url(secrets.token_bytes(32))
    code_challenge = _b64url(hashlib.sha256(code_verifier.encode("ascii")).digest())
    state = _b64url(secrets.token_bytes(32))
    query = urlencode(
        {
            "state": state,
            "scope": SCOPE,
            "signUpUrl": SIGN_UP_URL,
            "client_id": OAUTH_CLIENT_ID,
            "code_challenge_method": "S256",
            "response_type": "code",
            "max_age": "0",
            "audience": AUDIENCE,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": code_challenge,
            "prompt": "login",
            "auth0Client": AUTH0_CLIENT_APP,
        }
    )
    return AuthorizeRequest(f"{AUTH0_AUTHORIZE_URL}?{query}", code_verifier, state)


# The redirect seldom arrives bare. Chrome quotes it inside its "Failed to launch '…' because
# the scheme does not have a registered handler" console error, and Firefox only has it as
# the value of a ``Location`` response header. So split the paste on whitespace and quotes,
# then look for the piece that carries the result. (Firefox's messages use curly quotes.)
_PASTED_PIECE = re.compile(r"[^\s'\"\u201c\u201d\u2018\u2019<>]+")
_CARRIES_RESULT = re.compile(r"(?:^|[?&])(?:code|error)=")


def parse_authorize_redirect(pasted: str, expected_state: str) -> str:
    """Pull the authorization ``code`` out of whatever the user pasted back.

    Accepts the full ``com.resideo.firstalert://…?code=…&state=…`` redirect, any URL or bare
    query carrying the same parameters, either of those with text around it (a console error
    line, a ``Location:`` header, a block of response headers), or a bare code. ``state`` is
    verified whenever the paste carries one.
    """
    pieces = _PASTED_PIECE.findall(pasted)
    if not pieces:
        raise ResideoAuthError("Nothing pasted", step="browser", code="invalid_code")

    redirect = next((piece for piece in pieces if _CARRIES_RESULT.search(piece)), None)
    if redirect is None:
        if len(pieces) == 1 and "=" not in pieces[0]:
            # Treat it as a bare code; nothing to cross-check but the exchange will reject junk.
            return pieces[0]
        raise ResideoAuthError(
            "That paste carries no `code` parameter",
            step="browser",
            code="invalid_code",
        )

    query = parse_qs(urlparse(redirect).query if "?" in redirect else redirect)

    if error := query.get("error", [None])[0]:
        raise ResideoAuthError(
            f"Resideo returned an error: {error} - {query.get('error_description', ['?'])[0]}",
            step="browser",
            code=error,
        )

    code = query.get("code", [None])[0]
    if not code:
        raise ResideoAuthError(
            "That URL carries no `code` parameter",
            step="browser",
            code="invalid_code",
        )
    if (returned := query.get("state", [None])[0]) and returned != expected_state:
        raise ResideoAuthError(
            "The pasted redirect belongs to a different sign-in attempt",
            step="browser",
            code="state_mismatch",
        )
    return code


class ResideoAuth:
    """Drives the Auth0 flows (login + refresh) for the Resideo consumer API."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        # ``_session`` runs the stateless refresh/token POSTs. The interactive
        # ``login`` flow uses its own cookie-jar session (it needs the ``_csrf`` cookie),
        # so it never pollutes a shared session's cookie state.
        self._session = session

    async def exchange_code(self, code: str, code_verifier: str) -> dict[str, Any]:
        """Exchange an authorization code (+ PKCE verifier) for tokens.

        Step 6 of the headless flow, and the only step the browser path needs.
        """
        try:
            async with self._session.post(
                OAUTH_TOKEN_URL,
                json={
                    "client_id": OAUTH_CLIENT_ID,
                    "code": code,
                    "redirect_uri": REDIRECT_URI,
                    "code_verifier": code_verifier,
                    "grant_type": "authorization_code",
                },
                headers={
                    "Auth0-Client": AUTH0_CLIENT_APP,
                    "Content-Type": "application/json",
                    "User-Agent": WEB_USER_AGENT,
                },
            ) as r:
                if r.status != 200:
                    text = await r.text()
                    token_code, description = _auth0_error(text)
                    raise ResideoAuthError(
                        f"token exchange returned {r.status}"
                        f"{f' ({token_code})' if token_code else ''}"
                        f": {description or text[:200]}",
                        step="token",
                        status=r.status,
                        code=token_code,
                    )
                return await r.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ResideoConnectionError(f"token exchange transport error: {err}") from err

    async def login(self, email: str, password: str) -> dict[str, Any]:
        """Run the full Auth0 email/password flow; return the token response dict.

        Raises ``ResideoAuthError`` on bad credentials / unexpected responses and
        ``ResideoConnectionError`` on transport failures.
        """
        code_verifier = _b64url(secrets.token_bytes(32))
        code_challenge = _b64url(hashlib.sha256(code_verifier.encode("ascii")).digest())
        state = _b64url(secrets.token_bytes(32))

        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(),
            headers={"User-Agent": WEB_USER_AGENT},
        ) as s:
            try:
                # Step 1 -- /authorize -> redirect carrying Auth0 `state` -----------
                async with s.get(
                    AUTH0_AUTHORIZE_URL,
                    params={
                        "state": state,
                        "scope": SCOPE,
                        "signUpUrl": SIGN_UP_URL,
                        "client_id": OAUTH_CLIENT_ID,
                        "code_challenge_method": "S256",
                        "response_type": "code",
                        "max_age": "0",
                        "audience": AUDIENCE,
                        "redirect_uri": REDIRECT_URI,
                        "code_challenge": code_challenge,
                        "prompt": "login",
                        "auth0Client": AUTH0_CLIENT_APP,
                    },
                    allow_redirects=False,
                ) as r:
                    if r.status != 302:
                        raise ResideoAuthError(
                            f"step1 /authorize expected 302, got {r.status}",
                            step="authorize",
                            status=r.status,
                        )
                    location = r.headers.get("Location", "")
                auth0_state = parse_qs(urlparse(location).query).get("state", [None])[0]
                if not auth0_state:
                    raise ResideoAuthError("step1 no `state` in redirect", step="authorize")

                # Step 2 -- /login page sets the `_csrf` cookie --------------------
                async with s.get(AUTH0_LOGIN_PAGE_URL, params={"state": auth0_state}) as r:
                    if r.status != 200:
                        raise ResideoAuthError(
                            f"step2 /login page returned {r.status}",
                            step="login-page",
                            status=r.status,
                        )
                csrf = next((m.value for m in s.cookie_jar if m.key == "_csrf"), None)
                if not csrf:
                    raise ResideoAuthError(
                        "step2 no `_csrf` cookie (tenant may use New Universal Login)",
                        step="login-page",
                    )

                # Step 3 -- submit credentials -> HTML form w/ wresult/wctx --------
                async with s.post(
                    AUTH0_LOGIN_URL,
                    json={
                        "client_id": OAUTH_CLIENT_ID,
                        "redirect_uri": REDIRECT_URI,
                        "tenant": TENANT,
                        "response_type": "code",
                        "scope": SCOPE,
                        "audience": AUDIENCE,
                        "_csrf": csrf,
                        "state": auth0_state,
                        "_intstate": "deprecated",
                        "username": email,
                        "password": password,
                        "connection": CONNECTION,
                    },
                    headers={
                        "Auth0-Client": AUTH0_CLIENT_BROWSER,
                        "Origin": AUTH0_BASE_URL,
                        "Content-Type": "application/json",
                    },
                ) as r:
                    status = r.status
                    body = await r.text()
                code, description = _auth0_error(body)
                if status != 200 or code == ERROR_INVALID_CREDENTIALS:
                    if code == ERROR_INVALID_CREDENTIALS:
                        raise ResideoAuthError(
                            "Invalid email or password",
                            step="credentials",
                            status=status,
                            code=code,
                        )
                    detail = f" — {description}" if description else ""
                    raise ResideoAuthError(
                        f"step3 /usernamepassword/login returned {status}"
                        f"{f' ({code})' if code else ''}{detail}",
                        step="credentials",
                        status=status,
                        code=code,
                    )
                m = re.search(r'name="wresult"\s+value="([^"]+)"', body)
                if not m:
                    raise ResideoAuthError(
                        "step3 could not extract `wresult` (tenant may use New Universal Login)",
                        step="credentials",
                        status=status,
                    )
                wresult = html.unescape(m.group(1))
                mc = re.search(r'name="wctx"\s+value="([^"]+)"', body)
                wctx = html.unescape(mc.group(1)) if mc else ""

                # Step 4 -- post wresult/wctx -> redirect to a resume URL ----------
                form = {"wa": "wsignin1.0", "wresult": wresult}
                if wctx:
                    form["wctx"] = wctx
                async with s.post(
                    AUTH0_CALLBACK_URL,
                    data=form,
                    headers={"Origin": AUTH0_BASE_URL},
                    allow_redirects=False,
                ) as r:
                    if r.status != 302:
                        raise ResideoAuthError(
                            f"step4 /login/callback expected 302, got {r.status}",
                            step="callback",
                            status=r.status,
                        )
                    resume_url = r.headers.get("Location", "")
                if not resume_url:
                    raise ResideoAuthError("step4 no resume `Location`", step="callback")
                if not resume_url.startswith("http"):
                    resume_url = AUTH0_BASE_URL + resume_url

                # Step 5 -- resume -> redirect to redirect_uri?code=... ------------
                async with s.get(resume_url, allow_redirects=False) as r:
                    if r.status != 302:
                        raise ResideoAuthError(
                            f"step5 resume expected 302, got {r.status}",
                            step="resume",
                            status=r.status,
                        )
                    location = r.headers.get("Location", "")
                q = parse_qs(urlparse(location).query)
                auth_code = q.get("code", [None])[0]
                if not auth_code:
                    raise ResideoAuthError(
                        f"step5 no `code`: {q.get('error', ['?'])[0]} - "
                        f"{q.get('error_description', ['?'])[0]}",
                        step="resume",
                        code=q.get("error", [None])[0],
                    )
                if q.get("state", [None])[0] != state:
                    raise ResideoAuthError("step5 state mismatch", step="resume")
            except (aiohttp.ClientError, TimeoutError) as err:
                # aiohttp raises bare TimeoutError (not a ClientError) on timeout expiry.
                raise ResideoConnectionError(f"Auth0 login transport error: {err}") from err

        # Step 6 -- exchange code (+ PKCE verifier) for tokens -----------------
        # Outside the cookie-jar session: the exchange is stateless, and this is exactly
        # what the browser path calls once the user pastes their redirect back.
        return await self.exchange_code(auth_code, code_verifier)

    async def refresh(self, refresh_token: str) -> dict[str, Any]:
        """Exchange a refresh token for a fresh access token (+ rotated refresh token)."""
        try:
            async with self._session.post(
                OAUTH_TOKEN_URL,
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": OAUTH_CLIENT_ID,
                },
                headers={"Content-Type": "application/json", "User-Agent": WEB_USER_AGENT},
            ) as r:
                if r.status != 200:
                    text = await r.text()
                    code, description = _auth0_error(text)
                    if r.status == 401:
                        raise ResideoAuthError(
                            "Invalid or expired refresh token"
                            f"{f' ({code})' if code else ''}"
                            f"{f': {description}' if description else ''}",
                            step="refresh",
                            status=r.status,
                            code=code,
                        )
                    raise ResideoAuthError(
                        f"refresh failed {r.status}: {text[:200]}",
                        step="refresh",
                        status=r.status,
                        code=code,
                    )
                return await r.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ResideoConnectionError(f"token refresh transport error: {err}") from err
