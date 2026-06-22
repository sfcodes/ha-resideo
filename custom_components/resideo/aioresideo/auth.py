"""Auth0 authentication for the Resideo consumer API.

Async (aiohttp) port of the proven flow in ``spikes/resideo_consumer.py`` — the Auth0
"classic" Universal Login + WS-Fed callback dance, plus refresh-token exchange. Uses the
Resideo / First Alert app's own public Auth0 client, so no developer account is needed.

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
from typing import Any
from urllib.parse import parse_qs, urlparse

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


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


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


class ResideoAuth:
    """Drives the Auth0 flows (login + refresh) for the Resideo consumer API."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        # ``_session`` runs the stateless refresh/token POSTs. The interactive
        # ``login`` flow uses its own cookie-jar session (it needs the ``_csrf`` cookie),
        # so it never pollutes a shared session's cookie state.
        self._session = session

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
                        raise ResideoAuthError(f"step1 /authorize expected 302, got {r.status}")
                    location = r.headers.get("Location", "")
                auth0_state = parse_qs(urlparse(location).query).get("state", [None])[0]
                if not auth0_state:
                    raise ResideoAuthError("step1 no `state` in redirect")

                # Step 2 -- /login page sets the `_csrf` cookie --------------------
                async with s.get(AUTH0_LOGIN_PAGE_URL, params={"state": auth0_state}) as r:
                    if r.status != 200:
                        raise ResideoAuthError(f"step2 /login page returned {r.status}")
                csrf = next((m.value for m in s.cookie_jar if m.key == "_csrf"), None)
                if not csrf:
                    raise ResideoAuthError(
                        "step2 no `_csrf` cookie (tenant may use New Universal Login)"
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
                if status != 200 or "Wrong email or password" in body or "invalid_grant" in body:
                    if "Wrong email or password" in body or "invalid_grant" in body:
                        raise ResideoAuthError("Invalid email or password")
                    raise ResideoAuthError(f"step3 /usernamepassword/login returned {status}")
                m = re.search(r'name="wresult"\s+value="([^"]+)"', body)
                if not m:
                    raise ResideoAuthError(
                        "step3 could not extract `wresult` (tenant may use New Universal Login)"
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
                            f"step4 /login/callback expected 302, got {r.status}"
                        )
                    resume_url = r.headers.get("Location", "")
                if not resume_url:
                    raise ResideoAuthError("step4 no resume `Location`")
                if not resume_url.startswith("http"):
                    resume_url = AUTH0_BASE_URL + resume_url

                # Step 5 -- resume -> redirect to redirect_uri?code=... ------------
                async with s.get(resume_url, allow_redirects=False) as r:
                    if r.status != 302:
                        raise ResideoAuthError(f"step5 resume expected 302, got {r.status}")
                    location = r.headers.get("Location", "")
                q = parse_qs(urlparse(location).query)
                code = q.get("code", [None])[0]
                if not code:
                    raise ResideoAuthError(
                        f"step5 no `code`: {q.get('error', ['?'])[0]} - "
                        f"{q.get('error_description', ['?'])[0]}"
                    )
                if q.get("state", [None])[0] != state:
                    raise ResideoAuthError("step5 state mismatch")

                # Step 6 -- exchange code (+ PKCE verifier) for tokens -------------
                async with s.post(
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
                    },
                ) as r:
                    if r.status != 200:
                        text = await r.text()
                        raise ResideoAuthError(
                            f"step6 token exchange returned {r.status}: {text[:200]}"
                        )
                    return await r.json(content_type=None)
            except aiohttp.ClientError as err:
                raise ResideoConnectionError(f"Auth0 login transport error: {err}") from err

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
                if r.status == 401:
                    raise ResideoAuthError("Invalid or expired refresh token")
                if r.status != 200:
                    text = await r.text()
                    raise ResideoAuthError(f"refresh failed {r.status}: {text[:200]}")
                return await r.json(content_type=None)
        except aiohttp.ClientError as err:
            raise ResideoConnectionError(f"token refresh transport error: {err}") from err
