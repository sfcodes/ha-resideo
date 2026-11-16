"""Auth0 login/refresh tests — step tagging and Auth0 error-code normalization.

HTTP is mocked at the aiohttp layer with ``aioresponses`` (it also intercepts the private
cookie-jar session ``login`` opens for itself); no live Auth0 tenant is touched.

The codes matter: Auth0 bot detection rejects ``/usernamepassword/login`` with
``invalid_captcha`` *before* credentials are evaluated, so a captcha block must never be
reported to the user as a bad password.
"""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import parse_qs, urlparse

import aiohttp
import pytest
from aioresponses import CallbackResult, aioresponses

from custom_components.resideo.aioresideo.auth import (
    ResideoAuth,
    _b64url,
    build_authorize_url,
    parse_authorize_redirect,
)
from custom_components.resideo.aioresideo.const import (
    AUTH0_BASE_URL,
    AUTH0_CALLBACK_URL,
    OAUTH_TOKEN_URL,
    REDIRECT_URI,
)
from custom_components.resideo.aioresideo.exceptions import (
    ResideoAuthError,
    ResideoConnectionError,
)


class mocked(aioresponses):
    """``aioresponses`` that also feeds ``Set-Cookie`` into the session's cookie jar.

    It stubs ``ClientSession._request`` wholesale, which skips the jar update real aiohttp
    performs — and step 2 of the login flow exists purely to collect the ``_csrf`` cookie.
    """

    async def _request_mock(self, orig_self, method, url, *args, **kwargs):
        response = await super()._request_mock(orig_self, method, url, *args, **kwargs)
        if response.cookies:
            orig_self.cookie_jar.update_cookies(response.cookies, response.url)
        return response


AUTHORIZE_RE = re.compile(r"^https://login\.resideo\.com/authorize\?.*")
LOGIN_PAGE_RE = re.compile(r"^https://login\.resideo\.com/login\?.*")
RESUME_URL = f"{AUTH0_BASE_URL}/authorize/resume?state=a0state"

TOKENS = {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
WRESULT_FORM = '<form><input name="wresult" value="wr" /><input name="wctx" value="wc" /></form>'


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as s:
        yield s


def _captured_state(url) -> str:
    """The PKCE ``state`` the client generated, echoed back so step 5 validates."""
    return parse_qs(urlparse(str(url)).query)["state"][0]


def _mock_steps(m: aioresponses, *, login_result: CallbackResult | None = None) -> None:
    """Stub steps 1-6 of the Auth0 flow; ``login_result`` overrides step 3."""
    state_box: list[str] = []

    def authorize(url, **kwargs):
        state_box.append(_captured_state(url))
        return CallbackResult(status=302, headers={"Location": "/login?state=a0state"})

    m.get(AUTHORIZE_RE, callback=authorize)
    m.get(
        LOGIN_PAGE_RE,
        status=200,
        body="<html></html>",
        headers={"Set-Cookie": "_csrf=csrf-token; Path=/"},
    )
    m.post(
        AUTH0_BASE_URL + "/usernamepassword/login",
        **(
            {"callback": lambda url, **kw: login_result}
            if login_result is not None
            else {"status": 200, "body": WRESULT_FORM}
        ),
    )
    m.post(AUTH0_CALLBACK_URL, status=302, headers={"Location": RESUME_URL})

    def resume(url, **kwargs):
        location = f"{REDIRECT_URI}?code=auth-code&state={state_box[0]}"
        return CallbackResult(status=302, headers={"Location": location})

    m.get(RESUME_URL, callback=resume)
    m.post(OAUTH_TOKEN_URL, status=200, payload=TOKENS)


async def test_login_returns_tokens(session) -> None:
    """The happy path walks all six steps and hands back the token response."""
    with mocked() as m:
        _mock_steps(m)
        assert await ResideoAuth(session).login("a@b.com", "pw") == TOKENS


async def test_step3_captcha_is_not_reported_as_bad_credentials(session) -> None:
    """Auth0 bot detection — the failure behind issue #1."""
    body = json.dumps(
        {
            "statusCode": 401,
            "description": "Invalid captcha value",
            "name": "invalid_captcha",
            "code": "invalid_captcha",
        }
    )
    with mocked() as m:
        _mock_steps(m, login_result=CallbackResult(status=401, body=body))
        with pytest.raises(ResideoAuthError) as exc:
            await ResideoAuth(session).login("a@b.com", "pw")

    assert exc.value.code == "invalid_captcha"
    assert exc.value.step == "credentials"
    assert exc.value.status == 401
    assert "Invalid captcha value" in str(exc.value)


@pytest.mark.parametrize(
    "body",
    [
        json.dumps({"code": "invalid_user_password", "description": "Wrong email or password."}),
        json.dumps({"error": "invalid_grant", "error_description": "Wrong email or password."}),
        "<html>Wrong email or password</html>",  # classic-ULP HTML error page
    ],
    ids=["invalid_user_password", "invalid_grant", "html"],
)
async def test_step3_wrong_password_normalizes(session, body: str) -> None:
    """Every spelling of a rejected credential collapses to one code."""
    with mocked() as m:
        _mock_steps(m, login_result=CallbackResult(status=401, body=body))
        with pytest.raises(ResideoAuthError) as exc:
            await ResideoAuth(session).login("a@b.com", "pw")

    assert exc.value.code == "invalid_credentials"
    assert exc.value.step == "credentials"
    assert str(exc.value) == "Invalid email or password"


async def test_step3_wrong_password_in_a_200_body(session) -> None:
    """Auth0 can report rejected credentials with a 200 + error page."""
    with mocked() as m:
        _mock_steps(
            m, login_result=CallbackResult(status=200, body="<html>invalid_grant</html>")
        )
        with pytest.raises(ResideoAuthError) as exc:
            await ResideoAuth(session).login("a@b.com", "pw")

    assert exc.value.code == "invalid_credentials"


async def test_missing_csrf_cookie_tags_the_login_page_step(session) -> None:
    """A flow-mechanics break must not be attributed to the user's credentials."""
    with mocked() as m:
        m.get(
            AUTHORIZE_RE,
            status=302,
            headers={"Location": "/login?state=a0state"},
        )
        m.get(LOGIN_PAGE_RE, status=200, body="<html></html>")  # no Set-Cookie
        with pytest.raises(ResideoAuthError) as exc:
            await ResideoAuth(session).login("a@b.com", "pw")

    assert exc.value.step == "login-page"
    assert exc.value.code is None


async def test_missing_wresult_tags_the_step_without_a_code(session) -> None:
    with mocked() as m:
        _mock_steps(m, login_result=CallbackResult(status=200, body="<html>nope</html>"))
        with pytest.raises(ResideoAuthError) as exc:
            await ResideoAuth(session).login("a@b.com", "pw")

    assert exc.value.step == "credentials"
    assert exc.value.code is None
    assert "wresult" in str(exc.value)


async def test_refresh_401_parses_the_auth0_code(session) -> None:
    body = json.dumps({"error": "invalid_grant", "error_description": "Unknown or invalid refresh token."})
    with mocked() as m:
        m.post(OAUTH_TOKEN_URL, status=401, body=body)
        with pytest.raises(ResideoAuthError) as exc:
            await ResideoAuth(session).refresh("stale")

    assert exc.value.step == "refresh"
    assert exc.value.status == 401
    assert exc.value.code == "invalid_credentials"


async def test_refresh_returns_tokens(session) -> None:
    with mocked() as m:
        m.post(OAUTH_TOKEN_URL, status=200, payload=TOKENS)
        assert await ResideoAuth(session).refresh("rt") == TOKENS


async def test_transport_error_maps_to_connection_error(session) -> None:
    with mocked() as m:
        m.get(AUTHORIZE_RE, exception=aiohttp.ClientError("boom"))
        with pytest.raises(ResideoConnectionError):
            await ResideoAuth(session).login("a@b.com", "pw")


# --- browser authorization flow ----------------------------------------------


def test_build_authorize_url_carries_pkce_and_state() -> None:
    """The URL must be openable by a human and verifiable when they come back."""
    req = build_authorize_url()
    q = parse_qs(urlparse(req.url).query)

    assert req.url.startswith(f"{AUTH0_BASE_URL}/authorize?")
    assert q["state"] == [req.state]
    assert q["response_type"] == ["code"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["redirect_uri"] == [REDIRECT_URI]
    # The challenge is the S256 of the verifier we keep, never the verifier itself.
    expected = _b64url(hashlib.sha256(req.code_verifier.encode("ascii")).digest())
    assert q["code_challenge"] == [expected]
    assert req.code_verifier not in req.url


def test_build_authorize_url_is_unique_per_call() -> None:
    assert build_authorize_url().state != build_authorize_url().state


@pytest.mark.parametrize(
    "pasted",
    [
        f"{REDIRECT_URI}?code=the-code&state=st",
        f"  {REDIRECT_URI}?code=the-code&state=st  ",  # sloppy copy/paste
        "https://login.resideo.com/whatever?code=the-code&state=st",
        "?code=the-code&state=st",  # bare query, e.g. copied out of devtools
        "the-code",  # bare code, no state to check
        # Chrome's console error, which is where issue #13's reporter found the address.
        f"Failed to launch '{REDIRECT_URI}?code=the-code&state=st' because the scheme does not"
        " have a registered handler.",
        # The same line as "Copy message" yields it, source link first.
        f"login?state=hKFo2SB:1 Failed to launch '{REDIRECT_URI}?code=the-code&state=st'"
        " because the scheme does not have a registered handler.",
        f'"{REDIRECT_URI}?code=the-code&state=st"',
        # Firefox has no entry for the redirect, only the header that announced it.
        f"Location: {REDIRECT_URI}?code=the-code&state=st",
        f"HTTP/2 302\r\nlocation: {REDIRECT_URI}?code=the-code&state=st\r\ncontent-length: 0\r\n",
    ],
    ids=[
        "custom-scheme",
        "whitespace",
        "https",
        "bare-query",
        "bare-code",
        "chrome-console-error",
        "chrome-console-error-with-source",
        "quoted",
        "location-header",
        "response-headers",
    ],
)
def test_parse_authorize_redirect_accepts_what_users_actually_paste(pasted: str) -> None:
    assert parse_authorize_redirect(pasted, "st") == "the-code"


@pytest.mark.parametrize(
    "pasted",
    [
        # Firefox's last Network entry: the request whose redirect failed, not the redirect.
        "https://login.resideo.com/authorize/resume?state=st",
        "Prevented navigation to “https://login.resideo.com/authorize/resume?state=st” due to"
        " an unknown protocol.",
        "the scheme does not have a registered handler",
    ],
    ids=["firefox-resume-entry", "firefox-console-warning", "prose"],
)
def test_parse_authorize_redirect_rejects_a_paste_without_the_redirect(pasted: str) -> None:
    with pytest.raises(ResideoAuthError) as exc:
        parse_authorize_redirect(pasted, "st")
    assert exc.value.code == "invalid_code"


def test_parse_authorize_redirect_rejects_a_stale_attempt() -> None:
    with pytest.raises(ResideoAuthError) as exc:
        parse_authorize_redirect(f"{REDIRECT_URI}?code=c&state=other", "st")
    assert exc.value.code == "state_mismatch"


def test_parse_authorize_redirect_rejects_a_url_without_a_code() -> None:
    with pytest.raises(ResideoAuthError) as exc:
        parse_authorize_redirect("https://login.resideo.com/?foo=bar", "st")
    assert exc.value.code == "invalid_code"


def test_parse_authorize_redirect_surfaces_an_auth0_error_redirect() -> None:
    """Auth0 reports a refused sign-in in the redirect itself, not just by HTTP status."""
    with pytest.raises(ResideoAuthError) as exc:
        parse_authorize_redirect(
            f"{REDIRECT_URI}?error=access_denied&error_description=Nope&state=st", "st"
        )
    assert exc.value.code == "access_denied"
    assert "Nope" in str(exc.value)


def test_parse_authorize_redirect_rejects_an_empty_paste() -> None:
    with pytest.raises(ResideoAuthError) as exc:
        parse_authorize_redirect("   ", "st")
    assert exc.value.code == "invalid_code"


async def test_exchange_code_returns_tokens(session) -> None:
    seen: dict = {}

    def capture(url, **kwargs):
        seen.update(kwargs["json"])
        return CallbackResult(status=200, payload=TOKENS)

    with mocked() as m:
        m.post(OAUTH_TOKEN_URL, callback=capture)
        assert await ResideoAuth(session).exchange_code("the-code", "verifier") == TOKENS

    assert seen["grant_type"] == "authorization_code"
    assert seen["code"] == "the-code"
    assert seen["code_verifier"] == "verifier"
    assert seen["redirect_uri"] == REDIRECT_URI


async def test_exchange_code_surfaces_the_auth0_code(session) -> None:
    """A reused or expired authorization code comes back as invalid_grant."""
    body = json.dumps(
        {"error": "invalid_grant", "error_description": "Invalid authorization code"}
    )
    with mocked() as m:
        m.post(OAUTH_TOKEN_URL, status=403, body=body)
        with pytest.raises(ResideoAuthError) as exc:
            await ResideoAuth(session).exchange_code("spent", "verifier")

    assert exc.value.step == "token"
    assert exc.value.status == 403
    assert exc.value.code == "invalid_credentials"
    assert "Invalid authorization code" in str(exc.value)
