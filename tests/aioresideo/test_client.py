"""Client transport tests — headers, 401-retry, timeout mapping, single-flight refresh.

HTTP is mocked at the aiohttp layer with ``aioresponses``; no live API is touched.
"""

from __future__ import annotations

import asyncio
import time

import aiohttp
import pytest
from aioresponses import CallbackResult, aioresponses

from custom_components.resideo.aioresideo.client import ResideoClient
from custom_components.resideo.aioresideo.const import (
    API_BASE_URL,
    OAUTH_TOKEN_URL,
    OCP_APIM_SUBSCRIPTION_KEY,
)
from custom_components.resideo.aioresideo.exceptions import (
    ResideoApiError,
    ResideoAuthError,
    ResideoConnectionError,
)

MAC = "AABBCCDDEEFF"
DEVICE_URL = f"{API_BASE_URL}/devsrv/api/v2/device/{MAC}"


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as s:
        yield s


def _fresh_client(session: aiohttp.ClientSession, **kwargs) -> ResideoClient:
    """A client whose access token is valid for the whole test (no refresh needed)."""
    return ResideoClient(
        session,
        refresh_token="rt",
        access_token="at",
        expires_at=time.time() + 3600,
        **kwargs,
    )


async def test_request_sends_required_headers(session) -> None:
    """Every call must carry the bearer token AND the Azure APIM subscription key."""
    client = _fresh_client(session)
    seen_headers: dict = {}

    def capture(url, **kwargs):
        seen_headers.update(kwargs["headers"])
        return CallbackResult(status=200, payload={"DeviceId": MAC})

    with aioresponses() as m:
        m.get(DEVICE_URL, callback=capture)
        data = await client.get_device(MAC)

    assert data == {"DeviceId": MAC}
    assert seen_headers["Authorization"] == "Bearer at"
    assert seen_headers["Ocp-Apim-Subscription-Key"] == OCP_APIM_SUBSCRIPTION_KEY
    assert "User-Agent" in seen_headers


async def test_write_carries_channel_id_and_returns_transaction(session) -> None:
    """devsrv writes inject ChannelId and hand back the 202 TransactionId body."""
    client = _fresh_client(session)
    seen_body: dict = {}

    def capture(url, **kwargs):
        seen_body.update(kwargs["json"])
        return CallbackResult(status=202, payload={"TransactionId": "tx-1"})

    with aioresponses() as m:
        m.put(f"{DEVICE_URL}/coolSetpoint", callback=capture)
        result = await client.set_cool_setpoint(MAC, 74)

    assert result == {"TransactionId": "tx-1"}
    assert seen_body["ChannelId"] == "ds-notification-service"
    assert seen_body["SetpointValue"] == 74.0
    assert seen_body["SetpointStatus"] == "PermanentHold"


async def test_401_refreshes_and_retries_once(session) -> None:
    """A stale-token 401 forces a refresh and retries exactly once with the new token."""
    client = _fresh_client(session)
    auth_headers: list[str] = []

    def capture_401(url, **kwargs):
        auth_headers.append(kwargs["headers"]["Authorization"])
        return CallbackResult(status=401)

    def capture_ok(url, **kwargs):
        auth_headers.append(kwargs["headers"]["Authorization"])
        return CallbackResult(status=200, payload={"DeviceId": MAC})

    with aioresponses() as m:
        m.get(DEVICE_URL, callback=capture_401)
        m.post(OAUTH_TOKEN_URL, payload={"access_token": "new", "expires_in": 3600})
        m.get(DEVICE_URL, callback=capture_ok)
        data = await client.get_device(MAC)

    assert data == {"DeviceId": MAC}
    assert auth_headers == ["Bearer at", "Bearer new"]


async def test_persistent_401_raises_auth_error(session) -> None:
    client = _fresh_client(session)
    with aioresponses() as m:
        m.get(DEVICE_URL, status=401)
        m.post(OAUTH_TOKEN_URL, payload={"access_token": "new", "expires_in": 3600})
        m.get(DEVICE_URL, status=401)
        with pytest.raises(ResideoAuthError):
            await client.get_device(MAC)


async def test_timeout_maps_to_connection_error(session) -> None:
    """aiohttp raises bare TimeoutError on total-timeout — it must not escape unmapped."""
    client = _fresh_client(session)
    with aioresponses() as m:
        m.get(DEVICE_URL, exception=TimeoutError())
        with pytest.raises(ResideoConnectionError):
            await client.get_device(MAC)


async def test_client_error_maps_to_connection_error(session) -> None:
    client = _fresh_client(session)
    with aioresponses() as m:
        m.get(DEVICE_URL, exception=aiohttp.ClientConnectionError("boom"))
        with pytest.raises(ResideoConnectionError):
            await client.get_device(MAC)


async def test_api_error_carries_status_and_body(session) -> None:
    client = _fresh_client(session)
    with aioresponses() as m:
        m.get(DEVICE_URL, status=404, payload={"error": "no such device"})
        with pytest.raises(ResideoApiError) as excinfo:
            await client.get_device(MAC)
    assert excinfo.value.status == 404
    assert excinfo.value.body == {"error": "no such device"}


async def test_concurrent_refresh_is_single_flight(session) -> None:
    """Parallel callers must share ONE refresh (Auth0 reuse detection can revoke the grant)."""
    client = ResideoClient(session, refresh_token="rt")
    calls = 0

    async def fake_refresh(refresh_token: str) -> dict:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)  # hold the lock so the others must wait
        return {"access_token": "new", "refresh_token": "rt2", "expires_in": 3600}

    client._auth.refresh = fake_refresh  # type: ignore[method-assign]
    tokens = await asyncio.gather(*(client.async_ensure_token() for _ in range(5)))

    assert calls == 1
    assert set(tokens) == {"new"}
    assert client.refresh_token == "rt2"  # the rotated token was adopted


async def test_refresh_keeps_old_token_when_not_rotated_and_fires_callback(session) -> None:
    updates: list[dict] = []
    client = ResideoClient(session, refresh_token="rt", token_updated_cb=updates.append)
    with aioresponses() as m:
        # Auth0 does not always rotate: no refresh_token in the response.
        m.post(OAUTH_TOKEN_URL, payload={"access_token": "new", "expires_in": 3600})
        token = await client.async_ensure_token()
    assert token == "new"
    assert client.refresh_token == "rt"
    assert updates and updates[0]["refresh_token"] == "rt"


async def test_invalid_refresh_token_raises_auth_error(session) -> None:
    client = ResideoClient(session, refresh_token="rt")
    with aioresponses() as m:
        m.post(OAUTH_TOKEN_URL, status=401)
        with pytest.raises(ResideoAuthError):
            await client.async_ensure_token()
