# Resideo / Honeywell Home — Home Assistant integration

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5)](https://hacs.xyz/docs/faq/custom_repositories/)
[![GitHub release](https://img.shields.io/github/v/release/sfcodes/ha-resideo)](https://github.com/sfcodes/ha-resideo/releases)
[![CI](https://github.com/sfcodes/ha-resideo/actions/workflows/ci.yml/badge.svg)](https://github.com/sfcodes/ha-resideo/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/sfcodes/ha-resideo)](LICENSE)

Control your **Resideo / Honeywell Home** thermostats from Home Assistant. Sign in with the
**same email and password** you use in the Resideo app — that's the whole setup. From then on
everything stays in sync in real time: change something on the thermostat or in the app, and
Home Assistant sees it a second later.

> [!NOTE]
> Home Assistant already ships with a [Lyric](https://www.home-assistant.io/integrations/lyric/)
> integration for these thermostats, but it requires a _developer account_ with OAuth API keys
> — a bit of a headache to set up — and it updates by _polling_. This one uses your regular
> credentials and **streams changes in real time**.

## What you get

Each thermostat becomes a Home Assistant device, and every wireless room sensor gets one too.
Entities only appear when your hardware actually supports them — no dead tiles.

### Thermostat

| Entity | Type | Notes |
| --- | --- | --- |
| Thermostat | `climate` | Current temperature & humidity; heat / cool / off (+ auto where supported); target temperature or range; fan **Auto / Circulate / On**; presets **None / Temporary hold / Permanent hold** |
| Indoor / Outdoor temperature | `sensor` | Follows the device's °F/°C setting |
| Indoor / Outdoor humidity | `sensor` | |
| Carbon dioxide, VOC | `sensor` | Air-quality models only |
| Connectivity | `binary_sensor` | Reports **Disconnected** when the thermostat drops offline |
| Fan, Circulation fan | `binary_sensor` | Whether air is moving, and why |
| Adaptive recovery active | `binary_sensor` | Pre-heating/cooling ahead of a schedule period |
| Air filter, Fault | `binary_sensor` | Complains when something's wrong |
| Feels Like | `switch` | Target the perceived (feels-like) temperature instead of the measured one |
| Adaptive recovery | `switch` | a.k.a. Smart Response |
| Schedule | `switch` | Follow or ignore the programmed schedule |
| Emergency heat | `switch` | Shown only when the thermostat reports emergency-heat support |
| Heat/Cool setpoint min & max | `number` | Guardrails for the setpoint range (°F models only) |
| Freeze protection floor | `number` | A 35–45 °F "pipes shall not freeze" floor (°F models only) |

<details>
<summary>The nerd drawer: ~45 diagnostic entities, if you're into that</summary>

Heat/Cool setpoint, Hold status, Equipment status, Demand, Current stage, Air / CO2 / VOC /
Humidity quality, Schedule period / day / type, Backlight, Room priority, Air filter remaining,
Firmware (+ status, last updated), Faults, Signal strength, Heat/cool mode, Fan reason, Demand
response, Adaptive recovery mode, Ventilation & ventilation-boost timers, Language, Registered,
Heating/Cooling system & stages, Temperature units, Matter status, Priority status — plus
Vacation hold, Freeze protection, Away mode, and Commercial mode binary sensors.

</details>

### Wireless room sensors

| Entity | Type | Notes |
| --- | --- | --- |
| Temperature, Humidity | `sensor` | Per-room readings the thermostat averages |
| Carbon dioxide, VOC | `sensor` | Air-quality sensor models only |
| Motion, Occupancy | `binary_sensor` | |
| Exclude motion / Exclude temperature | `switch` | Drop this room from occupancy/averaging |
| Occupancy sensitivity | `select` | Takes effect at the sensor's next check-in (battery life comes first) |
| Battery, Signal strength, Status | `sensor` | Diagnostic |

<img align="right" width="300" src="https://raw.githubusercontent.com/sfcodes/ha-resideo/main/docs/images/device-page.png" alt="Resideo thermostat device page in Home Assistant">

## Prerequisites

Two things, and you probably have both already:

- A **Resideo / Honeywell Home account** — the same email/password you use in the Resideo (or
  First Alert) mobile app.
- Thermostats that show up in that app. Built and tested against an **ElitePRO S1200 Smart /
  X8S Smart Thermostat** with wireless room sensors; T9/T10 and other models the app manages may
  work too — if yours does (or doesn't), let us know. Smoke detectors and other Resideo products
  aren't supported yet — if you own one and want to help wire it up, contributions are warmly
  welcome.

## Installation

The one-click way:

[![Open your Home Assistant instance and open this repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=sfcodes&repository=ha-resideo&category=integration)

The clicking-around way:

1. HACS → Integrations → ⋮ → **Custom repositories** → add
   `https://github.com/sfcodes/ha-resideo`, category **Integration**.
2. Install **Resideo**, then restart Home Assistant.

<details>
<summary>No HACS? The copy-paste classic</summary>

Copy `custom_components/resideo/` from the latest release into your Home Assistant
`config/custom_components/` directory and restart. The integration is fully self-contained —
the async API client (`aioresideo`) is vendored inside it, and the only runtime dependency is
`aiohttp`, which Home Assistant already provides.

</details>

## Configuration

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=resideo)

Or: Settings → Devices & Services → **Add Integration** → **Resideo**. Sign in however you
like:

- **Login** — your everyday Resideo email/password (sent only to Resideo's own sign-in
  endpoint; only the resulting refresh token gets stored).
- **Refresh token** — already have an Auth0 refresh token and prefer pasting to typing? Go
  for it.

One entry per account — adding the same account twice gets politely rejected. And if the token
ever dies (password change, revocation), you get a re-authentication prompt instead of a
silently broken integration.

## Data updates

This integration is `cloud_push`: after one REST bootstrap against Resideo's
native API at `api.resideo.com` (the same backend the app uses), it parks on a persistent
Azure SignalR stream — so thermostat, app, and schedule changes land in Home Assistant in
**~1–3 seconds**, including the ones someone makes on the wall. Your own commands show up
instantly (optimistically), get confirmed by the stream, then double-checked by a quiet
reconcile — no flicker, no stale values.

If the stream can't be established, setup fails and retries — there's no polling fallback;
this integration simply doesn't poll.

## Troubleshooting

**Something acting up? Grab a debug log.** Settings → Devices & Services → **Resideo** →
**Enable debug logging**. (Flip it off again and Home Assistant hands you the captured log.)
YAML fans:

```yaml
logger:
  logs:
    custom_components.resideo: debug
```

**Download diagnostics.** On the integration (or any device) page: ⋮ → **Download
diagnostics**. It's the raw device state with tokens, serial numbers, and MAC addresses
already scrubbed — perfect for bug reports.

**Filing an issue?** Bring receipts: the Home Assistant and integration versions, the
diagnostics file, and a debug log covering the moment things went sideways.

**"Cannot connect" during setup** usually means a firewall or proxy is eating outbound
WebSockets to `*.service.signalr.net`. The stream isn't optional, so un-block it and try
again.

## Known limitations

> [!WARNING]
> **Unofficial & reverse-engineered.** This integration mimics the mobile app against an
> undocumented API; Resideo may change or cut off access at any time. Use at your own risk.

- **Celsius-configured thermostats** hide the setpoint-limit and freeze-protection numbers:
  those writes are integer-°F under the hood, and we'd rather show you nothing than write
  garbage to your furnace.
- **Temporary hold** only exists while a schedule is enabled and followed; with the schedule
  off, setpoint changes are permanent holds — exactly like the app.
- **Vacation hold** and **Hold until** show up when the device reports them, but can't be
  started from Home Assistant yet.

## Removing the integration

Leaving? No hard feelings. Settings → Devices & Services → **Resideo** → ⋮ → **Delete**. If
you also delete the files, restart afterwards. Devices that vanished from your Resideo account
can be removed one-by-one from their device pages.

## Development

PRs and bug reports welcome — the setup is the usual:

```bash
python -m venv .venv && source .venv/bin/activate
pip install ruff -r requirements.txt -r requirements.test.txt
ruff check .
pytest   # tests/aioresideo (client) + tests/resideo (HA integration layer)
```

The vendored client has its own docs in [`docs/CLIENT.md`](docs/CLIENT.md). The repo doubles
as a local Home Assistant dev instance under `config/` (gitignored), with
`config/custom_components` symlinked to `custom_components/` so your edits load live.

**Releasing:** versioning is driven from `manifest.json` via `bump2version`
(`.bumpversion.cfg`). Run the **Release** GitHub Action (choose patch/minor/major) — it runs
the tests, bumps the version, pushes a `vX.Y.Z` tag, and drafts a GitHub Release. HACS installs
from releases.

## License

[MIT](LICENSE). Go build something cozy.
