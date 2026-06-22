# aioresideo — vendored client reference

Async Python client for the **private Resideo consumer API** at `api.resideo.com` — the same
backend the Resideo / First Alert mobile app uses. It authenticates through the app's own public
**Auth0** client with your normal Resideo email/password (or a refresh token), so **no
`developer.honeywellhome.com` account is required**.

This client is **vendored** into the integration at `custom_components/resideo/aioresideo/`
(it is not published to PyPI). Within the integration it is imported relatively, e.g.
`from .aioresideo import Resideo`; the examples below show the public API surface.

Scope: **thermostats** — read state + control (setpoints, mode, fan, hold) **and a real-time
push stream** (Azure SignalR) for live state without polling.

> ⚠️ Reverse-engineered and unofficial. The consumer API is undocumented and may change without
> notice.

## Layout

- `auth.py` — Auth0 PKCE login / token refresh.
- `client.py` — low-level authenticated HTTP client (raw dicts).
- `const.py` — API URLs, Auth0 config, enums.
- `objects/` — typed device/account/room/event models.
- `stream.py` — Azure SignalR live-stream client.
- `merge.py` — delta-merge of push events into the cached device shadow.
- `exceptions.py` — error hierarchy.

## Usage (sketch)

```python
import aiohttp
from custom_components.resideo.aioresideo import Resideo, ResideoAuth

async def main():
    async with aiohttp.ClientSession() as session:
        # First time: log in with email/password to obtain tokens.
        tokens = await ResideoAuth(session).login("you@example.com", "password")

        # Thereafter: construct from the refresh token (persist it yourself).
        api = Resideo(session, refresh_token=tokens["refresh_token"])
        for device in await api.async_get_devices():
            print(device.name, device.device_id)

        shadow = await api.async_get_device("5CFCE1B7F5BA")
        print(shadow.indoor_temperature, shadow.cool_setpoint)

        await api.async_set_cool_setpoint("5CFCE1B7F5BA", 74.0)
```

### Real-time stream (SignalR)

```python
import asyncio
from custom_components.resideo.aioresideo import apply_live_feed  # merge a push delta into a cached shadow/rooms dict

targets = await api.async_get_signalr_targets()      # [{node_id, name, device_ids}], one per location

def on_event(ev):                                    # ev: ResideoLiveFeed | ResideoChangeConfirm
    ...  # e.g. new_shadow, new_rooms = apply_live_feed(shadow, rooms, ev)

stream = api.create_stream(targets[0]["node_id"], targets[0]["device_ids"], on_event)
await stream.async_connect_once_or_raise()           # negotiate → subscribe → activate (/priority)
asyncio.create_task(stream.async_run())              # supervises: keepalive, reconnect, refresh before expiry
```

The values feed has a fixed ~12-min lifetime per activation; `async_run` keeps it alive by
reconnecting shortly before `SubscriptionExpiration` (it can't be extended in place).

## Auth & headers

- Auth0 PKCE (`login.resideo.com`), scopes `openid profile email offline_access`. Access tokens
  last ~1h; refresh tokens rotate.
- Every API call sends `Authorization: Bearer <token>` **and**
  `Ocp-Apim-Subscription-Key: <prod APIM key>` (mandatory on the `devsrv` command service).
- Writes are async: they return `202 {"TransactionId": ...}`; re-read state to confirm.
