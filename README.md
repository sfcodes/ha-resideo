# Resideo (consumer API) — Home Assistant integration

Control **Resideo / Honeywell Home thermostats** in Home Assistant via the **private
consumer API** at `api.resideo.com` — the same backend the Resideo / First Alert mobile app
uses. You sign in with your normal Resideo email/password (Auth0), so **no
`developer.honeywellhome.com` account is required** (unlike the built-in `lyric` integration).

> ⚠️ Unofficial & reverse-engineered. The consumer API is undocumented and may change without
> notice. Use at your own risk.

The async API client is **vendored in-tree** at `custom_components/resideo/aioresideo/`, so the
integration is fully self-contained — there is no external library to install. See
[`docs/CLIENT.md`](docs/CLIENT.md) for the client API.

## Status: working (real-time push)

Functional integration. Reads are **real-time push** over the Resideo Azure SignalR stream
(`iot_class: cloud_push`) — external/wall-unit changes land in ~1–3 s with **no polling**. REST is
used only to bootstrap at setup and to resync on each (re)connect; if the stream can't be
established the integration reports an error (entities unavailable) rather than falling back to
polling. Climate (temperature, humidity, HVAC mode, fan mode) is verified end-to-end.

## Install (HACS custom repository)

1. HACS → Integrations → ⋮ → Custom repositories → add this repo, category **Integration**.
2. Install **Resideo**, restart Home Assistant.
3. Settings → Devices & Services → **Add Integration** → **Resideo**.
4. Sign in with your Resideo email/password, or paste a refresh token.

> No separate client package is required — `aioresideo` ships inside this repository. The only
> runtime dependency is `aiohttp`, which Home Assistant already provides.

## What it exposes

- `climate` — thermostat: current temp/humidity, HVAC mode, fan mode, setpoints (real-time push).
- `sensor` / `binary_sensor` — indoor/outdoor temperature & humidity, indoor air quality
  (TVOC / eCO₂), equipment status, demand/stage, schedule, faults, and per-room remote sensors —
  read off the same device shadow the stream keeps live.
- `switch` / `select` / `number` — feels-like, adaptive recovery, schedule enable, and related
  setpoint/mode controls.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install ruff -r requirements.txt -r requirements.test.txt
ruff check .
pytest   # tests/aioresideo (client) + tests/resideo (HA integration layer)
```

The repo doubles as a local Home Assistant dev instance under `config/` (gitignored), with
`config/custom_components` symlinked to `custom_components/` so the integration loads live.

## Releasing

Versioning is driven from `manifest.json` via `bump2version` (`.bumpversion.cfg`). Run the
**Release** GitHub Action (`workflow_dispatch`, choose patch/minor/major) — it runs the tests,
bumps the version, pushes a `vX.Y.Z` tag, and drafts a GitHub Release. HACS installs from releases.

## License

[MIT](LICENSE)
