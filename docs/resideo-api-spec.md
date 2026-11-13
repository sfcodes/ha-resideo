# Resideo Consumer API Specification (`api.ha.resideo.com`)

> [!WARNING]
> **Unofficial & Reverse-Engineered Specification.**
> This document describes the private Resideo consumer cloud API (`api.ha.resideo.com`) used by the **First Alert** mobile app (`com.resideo.firstalert`) for Denali / Titan platform thermostats (ElitePRO S1200, X8S, T9/T10, and First Alert-migrated T5/T6 units) and RedLINK accessories (Equipment Interface Module `Model 4353` and Wireless Indoor Air Sensor `Model 4354`).
> It is **not** the public Honeywell Home developer API (`api.honeywellhome.com`) used for legacy Lyric (LCC) thermostats.

---

## Table of Contents

1. [Authentication & Token Lifecycle (Auth0 PKCE)](#1-authentication--token-lifecycle-auth0-pkce)
2. [Base URLs, Required Headers & API Versioning](#2-base-urls-required-headers--api-versioning)
3. [REST Read Endpoints](#3-rest-read-endpoints)
4. [REST Write Endpoints (Thermostat & Accessory Commands)](#4-rest-write-endpoints-thermostat--accessory-commands)
5. [Device Shadow Schema (`/v2/devices/thermostats/{mac}`)](#5-device-shadow-schema-v2devicesthermostatsmac)
6. [Device Capabilities Schema (`/v2/devices/thermostats/{mac}/configuration`)](#6-device-capabilities-schema-v2devicesthermostatsmacconfiguration)
7. [Temperature Units & Regional Quirks](#7-temperature-units--regional-quirks)
8. [Error Responses & Cloud Deprecation Detection](#8-error-responses--cloud-deprecation-detection)
9. [Real-Time Push Streaming (Azure SignalR)](#9-real-time-push-streaming-azure-signalr)
10. [Rooms, EIM & Wireless Room Sensors (`Model 4354`)](#10-rooms-eim--wireless-room-sensors-model-4354)

---

## 1. Authentication & Token Lifecycle (Auth0 PKCE)

The First Alert mobile app authenticates against Resideo's production Auth0 tenant (`login.resideo.com`) using OAuth 2.0 Authorization Code Flow with PKCE. No developer portal registration or client secret is required.

### 1.1 Auth0 Client Parameters

| Parameter | Value |
| --- | --- |
| **Auth0 Base URL** | `https://login.resideo.com` |
| **Tenant** | `resideo-prod` |
| **Client ID** | `SRmiA7CaYi1JgivDZdzzoZu4X5VBogGt` |
| **Audience** | `https://resideo-prod.auth0.com/api/v2/` |
| **Redirect URI** | `com.resideo.firstalert://login.resideo.com/ios/com.resideo.firstalert/callback` |
| **Scope** | `openid profile email offline_access` |
| **Connection** | `Username-Password-Authentication` |
| **Sign-Up URL** | `https://myid.resideo.com/sign-up?userType=consumer` |
| **`Auth0-Client` (App)** | `eyJ2ZXJzaW9uIjoiMS4xNC4wIiwibmFtZSI6ImF1dGgwLWZsdXR0ZXIiLCJlbnYiOnsiY29yZSI6IjIuMTAuMCIsImlPUyI6IjI2LjEiLCJzd2lmdCI6IjUueCJ9fQ` (`{"version":"1.14.0","name":"auth0-flutter",...}`) |
| **`Auth0-Client` (Browser)** | `eyJuYW1lIjoiYXV0aDAuanMtdWxwIiwidmVyc2lvbiI6IjkuMTMuMiJ9` (`{"name":"auth0.js-ulp","version":"9.13.2"}`) |
| **Web User-Agent** | `Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)` |

### 1.2 Six-Step Headless Login Flow

When bot detection / CAPTCHA is not triggered, direct email + password authentication succeeds in 6 HTTP steps using a single cookie-jar session:

1. **`GET /authorize` (No Redirects)**:
   - Query parameters: `state`, `scope`, `signUpUrl`, `client_id`, `code_challenge_method=S256`, `response_type=code`, `max_age=0`, `audience`, `redirect_uri`, `code_challenge`, `prompt=login`, `auth0Client=<AUTH0_CLIENT_APP>`.
   - Returns `302 Found` with `Location: /login?state=<auth0_state>&...`. Extract the server-generated `auth0_state`.
2. **`GET /login?state=<auth0_state>`**:
   - Returns `200 OK` and sets the `_csrf` cookie in the session cookie jar.
3. **`POST /usernamepassword/login`**:
   - Headers: `Auth0-Client: <AUTH0_CLIENT_BROWSER>`, `Origin: https://login.resideo.com`, `Content-Type: application/json`.
   - JSON Body:
     ```json
     {
       "client_id": "SRmiA7CaYi1JgivDZdzzoZu4X5VBogGt",
       "redirect_uri": "com.resideo.firstalert://login.resideo.com/ios/com.resideo.firstalert/callback",
       "tenant": "resideo-prod",
       "response_type": "code",
       "scope": "openid profile email offline_access",
       "audience": "https://resideo-prod.auth0.com/api/v2/",
       "_csrf": "<_csrf_cookie>",
       "state": "<auth0_state>",
       "_intstate": "deprecated",
       "username": "<email>",
       "password": "<password>",
       "connection": "Username-Password-Authentication"
     }
     ```
   - Returns `200 OK` containing an HTML `<form>` with hidden inputs `name="wresult"` and `name="wctx"` (WS-Federation token response).
   - *Error handling*: If Auth0 bot detection triggers prior to password verification, it returns `400`/`401` JSON `{"code": "invalid_captcha", "description": "Invalid captcha value"}` or `{"code": "too_many_attempts", ...}`. Bad credentials return `invalid_user_password` / `invalid_grant` / `Wrong email or password`.
4. **`POST /login/callback` (No Redirects)**:
   - Form-encoded body (`application/x-www-form-urlencoded`): `wa=wsignin1.0&wresult=<unescaped_wresult>&wctx=<unescaped_wctx>`.
   - Returns `302 Found` with `Location: /authorize/resume?state=...`.
5. **`GET /authorize/resume?state=...` (No Redirects)**:
   - Returns `302 Found` with `Location: com.resideo.firstalert://login.resideo.com/ios/com.resideo.firstalert/callback?code=<auth_code>&state=<initial_state>`.
6. **`POST /oauth/token`**:
   - Headers: `Auth0-Client: <AUTH0_CLIENT_APP>`, `Content-Type: application/json`, `User-Agent: <WEB_USER_AGENT>`.
   - JSON Body:
     ```json
     {
       "client_id": "SRmiA7CaYi1JgivDZdzzoZu4X5VBogGt",
       "code": "<auth_code>",
       "redirect_uri": "com.resideo.firstalert://login.resideo.com/ios/com.resideo.firstalert/callback",
       "code_verifier": "<pkce_code_verifier>",
       "grant_type": "authorization_code"
     }
     ```
   - Returns `200 OK`:
     ```json
     {
       "access_token": "eyJhbGciOiJSUzI1NiIs...",
       "refresh_token": "v1.M...",
       "id_token": "eyJhbGciOiJSUzI1NiIs...",
       "scope": "openid profile email offline_access",
       "expires_in": 86400,
       "token_type": "Bearer"
     }
     ```

### 1.3 Browser Fallback Flow (Bypassing CAPTCHA)

When step 3 is blocked by `invalid_captcha`, the user can open the step 1 `/authorize` URL directly in a browser, complete the login interactively, and copy the final `com.resideo.firstalert://.../?code=...&state=...` redirect URL from the browser's DevTools Network tab into step 6 (`POST /oauth/token` with the stored `code_verifier`).

### 1.4 Token Refresh & Refresh Token Rotation (RTR)

- **Endpoint**: `POST https://login.resideo.com/oauth/token`
- **Body**:
  ```json
  {
    "grant_type": "refresh_token",
    "refresh_token": "<current_refresh_token>",
    "client_id": "SRmiA7CaYi1JgivDZdzzoZu4X5VBogGt"
  }
  ```
- **Critical Rotation Constraint**: Auth0 enforces **Refresh Token Rotation (RTR) with reuse detection**. Refreshing a token returns a new `refresh_token` and invalidates the previous one. If two processes or concurrent tasks attempt to refresh using the same `refresh_token`, Auth0 revokes the entire grant family (`401 invalid_grant`). Clients **must** guard token refreshes with a single-flight lock (`asyncio.Lock`) and persist rotated refresh tokens immediately.

---

## 2. Base URLs, Required Headers & API Versioning

### 2.1 Host Migration (September 2026)

In September 2026 (First Alert app v2.26.0, configuration bundle `flutter_assets/asset_bundles/secure/config/device_api_v2.23.0.json`), Resideo migrated the consumer API and SignalR negotiate endpoint to `https://api.ha.resideo.com`.
- **Current Production Base URL**: `https://api.ha.resideo.com`
- **Retired Base URL**: `https://api.resideo.com` (and `https://ds-notification-service.prod.titans.cloud`) — permanently returns HTTP `503` with `{"message": "The API is temporarily down for planned maintenance."}`.

### 2.2 Mandatory HTTP Headers

Every request to `api.ha.resideo.com` must carry both the Auth0 Bearer token and the Azure API Management (APIM) subscription key:

```http
Authorization: Bearer <access_token>
Ocp-Apim-Subscription-Key: b60885e8a9b44680a29ea1f03452878a
Accept: application/json
Content-Type: application/json
User-Agent: First Alert/2440 CFNetwork/3860.600.12 Darwin/25.5.0
```

### 2.3 `v1` vs `v2` Endpoint Split

Under `/ris-public-api/api`, endpoints are split across `v1` and `v2` with different response shapes:

| Version | Base Path | Endpoints & Behavior |
| --- | --- | --- |
| **`v2`** | `/ris-public-api/api/v2/devices/thermostats/{mac}` | **Device Shadow (`GET /`)** and **Capabilities (`GET /configuration`)**. Returns the twin wrapper `{DeviceId, Reported: {...}, Desired: {...}}`. (`v1` returns a flattened structure without the `Reported` wrapper.) |
| **`v1`** | `/ris-public-api/api/v1/devices/thermostats/{mac}` | **Room Priority (`GET /priority`)**, **Rooms & Accessories (`GET /group/0/rooms`)**, and **all `PUT` write commands**. (`/priority` and `/group/0/rooms` return `404` under `v2`.) |
| **`v1`** | `/ris-public-api/api/v1/accounts` | **Consumer Account Graph (`GET /accounts`)**. |

---

## 3. REST Read Endpoints

### 3.1 `GET /ris-public-api/api/v1/accounts`

Returns the authenticated user's account graph, regional metadata, locations, and registered devices.

```json
{
  "data": {
    "firstName": "Jane",
    "lastName": "Doe",
    "contactEmail": "jane@example.com",
    "countryCode": "US",
    "locale": "en-US",
    "consumerUsers": [
      {
        "consumerAccount": {
          "id": "ConsumerAccount:00000000-0000-0000-0000-000000000000",
          "locations": [
            {
              "id": "Q29uc3VtZXJEZXZpY2VMb2NhdGlvbjowMDAwMDAwMC0wMDAwLTAwMDAtMDAwMC0wMDAwMDAwMDAwMDA=",
              "name": "Home",
              "consumerAccountId": "Q29uc3VtZXJBY2NvdW50Oj...",
              "consumerDevices": [
                {
                  "id": "ConsumerDevice:11111111-1111-1111-1111-111111111111",
                  "name": "Kitchen",
                  "consumerDeviceLocationId": "Q29uc3VtZXJEZXZpY2VMb2NhdGlvbjow...",
                  "device": {
                    "deviceId": "AABBCCDDEEFF",
                    "globalDeviceType": "Denali_S1200",
                    "id": "THlyaWNUaGVybW9zdGF0RGV2aWNlOkFBQkJDQ0RERUVGRg=="
                  }
                }
              ]
            }
          ]
        }
      }
    ]
  }
}
```

- **Location `id`**: Base64-encoded GraphQL node ID (`ConsumerDeviceLocation:<uuid>`). Passed directly to `SubscribeSignalRV2` on the SignalR hub.
- **Device `deviceId`**: Raw 12-hex-character MAC address (`{mac}`) used in all thermostat endpoints.
- **Device `id` (`globalId`)**: Base64-encoded `<Type>:<deviceId>`, e.g. `base64("LyricThermostatDevice:AABBCCDDEEFF")`. Decoding `<Type>` is the authoritative way to distinguish thermostats (`LyricThermostatDevice`) from other products (`SmokeDetectorDevice`), because `globalDeviceType` contains model codenames like `Denali_S1200` that omit the word `"Thermostat"`.

### 3.2 `GET /ris-public-api/api/v2/devices/thermostats/{mac}`

Returns the full device shadow (`{DeviceId, Reported, Desired}`). See [§5](#5-device-shadow-schema-v2devicesthermostatsmac) for the complete schema.

### 3.3 `GET /ris-public-api/api/v2/devices/thermostats/{mac}/configuration`

Returns hardware capabilities, equipment stage configuration, setpoint limits, and allowed enums (`{DeviceId, Reported, Desired}`). See [§6](#6-device-capabilities-schema-v2devicesthermostatsmacconfiguration) for the complete schema.

### 3.4 `GET /ris-public-api/api/v1/devices/thermostats/{mac}/priority`

Returns the active room priority mode and selected rooms, **and acts as the server-side trigger that activates `LiveFeedEvent` streaming on SignalR** (see [§9.2](#92-two-step-negotiate--feed-activation)).

```json
{
  "DeviceId": "AABBCCDDEEFF",
  "PriorityStatus": "PermanentHold",
  "Priority": {
    "PriorityType": "PickARoom",
    "SelectedRooms": [0],
    "Rooms": [
      {
        "Id": 0,
        "RoomName": "Kitchen",
        "RoomAvgTemp": 71.0,
        "RoomAvgHumidity": 36,
        "OverallMotion": false,
        "Accessories": [
          {
            "Id": 0,
            "Type": "Thermostat",
            "ExcludeTemp": false,
            "ExcludeMotion": false,
            "Temperature": 71.0,
            "Status": "Ok",
            "DetectMotion": false
          }
        ]
      }
    ]
  }
}
```

### 3.5 `GET /ris-public-api/api/v1/devices/thermostats/{mac}/group/0/rooms`

Returns all rooms in group `0` (`default`) along with per-accessory metadata (`AccessoryAttribute`) and live sensor telemetry (`AccessoryValue`). See [§10](#10-rooms-eim--wireless-room-sensors-model-4354) for details.

---

## 4. REST Write Endpoints (Thermostat & Accessory Commands)

All write commands use `PUT` under the **`v1`** path:
`PUT https://api.ha.resideo.com/ris-public-api/api/v1/devices/thermostats/{mac}/{command}`

### 4.1 General Command Mechanics

- **Mandatory `ChannelId`**: Every `PUT` body must include `"ChannelId": "ds-notification-service"`.
- **Asynchronous Acceptance (`202 Accepted`)**: Commands do not block until the thermostat applies the change. They return HTTP `202 Accepted` with a transaction identifier:
  ```json
  {
    "TransactionId": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
  }
  ```
- **Confirmation & Read-Back**: Within ~1–3 seconds, SignalR broadcasts `ChangeRequestSuccess` (matching `TransactionId`) and/or a `LiveFeedEvent` delta, and the REST shadow updates.
- **⛔ Hazard Warning — Never Send Empty Probes**: Never probe a command endpoint with a bare `{"ChannelId": "ds-notification-service"}` payload. Missing fields default to `0`/`false`/first-enum in C# model binding—doing this on `systemSwitch` switches a live thermostat **Off**, and doing it on `feelsLike` disables Feels Like. Always test routes by echoing the device's current value as a no-op.

### 4.2 Verified Command Reference

| Command Path (`PUT .../v1/devices/thermostats/{mac}/...`) | Request JSON Payload (`+ "ChannelId": "ds-notification-service"`) | Notes & Constraints |
| --- | --- | --- |
| `coolSetpoint` | `{"SetpointStatus": "PermanentHold", "SetpointValue": 74.0}` | `SetpointStatus`: `"PermanentHold"` or `"TemporaryHold"` (when schedule is enabled). Value in °F. |
| `heatSetpoint` | `{"SetpointStatus": "PermanentHold", "SetpointValue": 68.0}` | Same `SetpointStatus` rules as `coolSetpoint`. Value in °F. |
| `systemSwitch` | `{"SystemSwitchValue": "Cool"}` | Allowed values from `SystemSwitchCapabilities`: `"Off"`, `"Heat"`, `"Cool"`, `"Auto"`, `"EmergencyHeat"`. |
| `fanSwitch` | `{"FanSwitch": {"Position": "Auto", "Speed": 1}}` | `Position` from `FanSwitchCapabilities.Positions`: `"Auto"`, `"On"`, `"Circulate"`. `Speed` is `1`. |
| `hold` | `{"Status": "PermanentHold"}` | Changes hold state without altering setpoints. Settable via status-only payload: `"NoHold"`, `"TemporaryHold"`, `"PermanentHold"`. (Requires `ScheduleEnabled == true` for `NoHold`/`TemporaryHold`; if schedule is off, device stays in `PermanentHold`.) |
| `humidifier/setpoint` | `{"SetPointValue": 40}` | **Note capital `P` in `SetPointValue`** (differs from `SetpointValue` on heat/cool). Integer % RH (`10`–`90`, step `5`). |
| `dehumidifier/setpoint` | `{"SetPointValue": 60}` | **Note capital `P` in `SetPointValue`**. Integer % RH (`10`–`90`, step `5`). |
| `priority` | `{"PriorityStatus": "PermanentHold", "CurrentPriority": {"PriorityType": "PickARoom", "SelectedRooms": [0, 1], "Rooms": null}}` | `PriorityType`: `"PickARoom"` or `"FollowMe"`. `SelectedRooms` must be a non-empty list of integer room IDs (e.g. `[0]`). Full body required. |
| `feelsLike` | `{"FeelsLikeEnabled": true}` | Non-nullable boolean. Updates `Reported.FeelsLikeEnabled`. |
| `adaptiveIntelligentRecovery` | `{"Mode": "AdaptiveIntelligentRecovery"}` | Enum: `"None"` (off), `"AdaptiveIntelligentRecovery"`, or `"DelayedStartRecovery"`. Updates `Reported.ActiveAdaptiveRecoveryMode`. |
| `schedule/enabled` | `{"ScheduleEnabled": true}` | Enabling immediately transitions `SetpointStatus` to `"NoHold"` and jumps setpoints to the active schedule period. Disabling leaves the last setpoints in place under `"PermanentHold"`. |
| `freezeProtection` | `{"LowLimitDegrees": 40}` | **Must be an integer** (`35`–`45` °F). Sending a float (`40.0`) returns HTTP `400`. Updates `/configuration` `Reported.FreezeProtection.LowLimitDegrees`. |
| `setPointCapabilities` | `{"minHeatSetpoint": 50, "maxHeatSetpoint": 90, "minCoolSetpoint": 50, "maxCoolSetpoint": 90}` | **LowerCamelCase keys** (differ from read keys). **Requires all 4 fields** as integers (a partial body returns `202` but is silently ignored). Device enforces `minHeat <= minCool` and `maxHeat <= maxCool`. |
| `accessories/{accessory_id}/accessoryValue` | `{"sensitivity": "Medium", "excludeMotion": false, "excludeTemp": false}` | **LowerCamelCase keys** (differ from read keys). **No per-field merge**—always send all 3 fields. `excludeMotion`/`excludeTemp` apply in ~10s; `sensitivity` (`"Off"`, `"Low"`, `"Medium"`, `"High"`) is queued until the wireless battery sensor's next wake-up check-in. |

### 4.3 Additional Discovered APIM Routes

Probing the Azure APIM route table on `api.ha.resideo.com` confirmed these additional endpoints exist on the command service (`401` when unauthenticated vs `404 Resource not found` for nonexistent paths):
- `PUT /ris-public-api/api/v1/devices/thermostats/{mac}/dehumAway`
- `PUT /ris-public-api/api/v1/devices/thermostats/{mac}/setpoint`
- `PUT /ris-public-api/api/v1/devices/thermostats/{mac}/faultThresholds`

---

## 5. Device Shadow Schema (`/v2/devices/thermostats/{mac}`)

`GET /ris-public-api/api/v2/devices/thermostats/{mac}` returns `{ "DeviceId": "<mac>", "Reported": { ... }, "Desired": { ... } }`.

Key fields in `Reported`:

| Path in `Reported` | Type / Example | Description |
| --- | --- | --- |
| `DeviceName` | `"Kitchen"` | User-assigned thermostat name |
| `Online` | `true` | Cloud connectivity status |
| `LastConnectivityStatusChange` | `"2026-11-01T14:22:10.123+00:00"` | ISO-8601 timestamp of last online/offline transition |
| `CurrentLanguage` | `"English"` | Faceplate UI language |
| `RegistrationDate` | `"2026-06-12T01:23:24.129+00:00"` | Initial cloud registration timestamp |
| `SystemSwitch` | `"Cool"` | Configured HVAC mode: `"Off"`, `"Heat"`, `"Cool"`, `"Auto"`, `"EmergencyHeat"` |
| `HeatCoolMode` | `"Cool"` | Active heat/cool sub-mode (especially useful when `SystemSwitch` is `"Auto"`) |
| `Setpoint.SetpointStatus` | `"PermanentHold"` | Hold state: `"NoHold"`, `"TemporaryHold"`, `"PermanentHold"`, `"HoldUntil"`, `"VacationHold"` |
| `Setpoint.HeatSetpoint` | `68.0` | Target heat setpoint (°F) |
| `Setpoint.CoolSetpoint` | `74.0` | Target cool setpoint (°F) |
| `FanSwitch.Position` | `"Auto"` | Configured fan mode (`"Auto"`, `"On"`, `"Circulate"`). Also mirrored at `Setpoint.FanSwitch.Position`. |
| `FanReq` | `1` / `0` | Raw fan request integer |
| `FanReqReasons` | `["HeatingOrCoolingStagesOn"]` | Array of reasons the blower is currently running |
| `OperationStatus.Mode` | `"EquipmentOff"` | Current equipment action: `"EquipmentOff"`, `"Off"`, `"Heat"`, `"Cool"` |
| `OperationStatus.FanRequest` | `false` | `true` when the main blower fan is running |
| `OperationStatus.CirculationFanRequest` | `false` | `true` when running in circulation duty cycle |
| `HeatAndCoolDemand.Mode` | `"Heat"` / `"Cool"` / `"Off"` | Active demand mode |
| `HeatAndCoolDemand.Demand` | `68` | Current call-for-heat/cool demand percentage (`0`–`100`) |
| `HeatAndCoolDemand.CurrentStage` | `1` | Active equipment stage (`0` when idle, `1`–`4` when active) |
| `HeatAndCoolDemand.StagesOn` | `[true, false, false, false]` | Per-stage relay states (Stages 1–4) |
| `DisplayedIndoorTemperature` | `71.0` | Priority-weighted, Feels-Like-adjusted control temperature shown on the thermostat faceplate (°F) |
| `DisplayedIndoorHumidity` | `36` | Displayed indoor relative humidity (%) |
| `DisplayedOutdoorTemperature` | `52.0` | Weather-service outdoor temperature (in the account country's unit — see [§7](#7-temperature-units--regional-quirks)) |
| `DisplayedOutdoorHumidity` | `64` | Weather-service outdoor relative humidity (%) |
| `HumidifierSetpoint` | `40` | Target humidifier setpoint (% RH) |
| `HumidifierRequest` | `0` / `1` | `1` when calling for humidification, `0` when idle |
| `DehumidifierSetpoint` | `60` | Target dehumidifier setpoint (% RH) |
| `DehumidifierRequest` | `0` / `1` | `1` when calling for dehumidification, `0` when idle |
| `WindowsProtection` | `4` | Window frost/condensation protection setting (`1`–`10`) |
| `WindowsProtectionPercentage` | `83.0` | Calculated window protection percentage |
| `DehumAway` | `{"Active": false, "DehumSetPoint": 60, "CoolSetPoint": 72.0}` | Away dehumidification state and setpoints |
| `DuctTemperature` | `{"DischargeTemperature": 95.4, "ReturnTemperature": 70.9}` | Supply (discharge) and return air duct thermistor readings (°F) when wired to an EIM (`Model 4353`) |
| `FeelsLikeEnabled` | `false` | Whether humidity-adjusted Feels Like control is active |
| `ActiveAdaptiveRecoveryMode` | `"AdaptiveIntelligentRecovery"` | Configured Smart Response mode (`"None"` when disabled) |
| `IsAdaptiveRecoveryActive` | `false` | `true` when actively pre-conditioning ahead of a schedule transition |
| `ScheduleEnabled` | `false` | Whether the programmed schedule is active |
| `CurrentSchedulePeriod` | `{"Day": "Thursday", "Period": "Away"}` | Current schedule day and period name |
| `ScheduleType` | `{"ScheduleType": "Timed", "ScheduleSubtype": "NA"}` | Configured schedule type |
| `CurrentPriorityType` | `"PickARoom"` | Active room priority mode (`"PickARoom"` or `"FollowMe"`) |
| `VacationHold` | `{"Enabled": false}` | Vacation hold status |
| `VentilationTimer` / `VentilationBoostTimer` | `0` | Ventilation timer minutes remaining |
| `BackLightState` | `"Active"` | Current faceplate backlight state |
| `IndoorAirQuality` | `{"TotalIndoorAirQuality": {"Quality": "VeryGood"}, "EstimatedCarbonDioxide": {"Quality": "VeryGood"}, "TotalVolatileOrganicCompounds": {"Quality": "VeryGood"}, "HumidityQuality": "Ideal"}` | Qualitative IAQ buckets (numeric CO₂ ppm and TVOC come from `/group/0/rooms`) |
| `AirFilterReminders.1` | `{"RemainingDays": 142, "IsAlertRaised": false}` | Air filter life remaining and alert flag |
| `FirmwareSystem` | `{"SwPackage": "01.3605.740", "LastUpdated": "2026-06-12T01:29:18.000+00:00"}` | Installed firmware version and update timestamp |
| `FirmwareUpdate` | `{"JobId": "", "SwPackage": "01.3605.740", "Status": "Success"}` | OTA firmware job status |
| `ThermostatFaults` | `[]` | Array of active fault objects |
| `DemandResponseState` | `{"EventId": -1, "OptedOut": false, "CurrentDrEventState": "NotStarted"}` | Utility demand-response state |
| `InbuiltSensorState` | `{"RoomId": 0, "RoomName": "Kitchen"}` | Room assignment for the thermostat's built-in sensor |
| `ZoneAccessories.ZoneAccessories` | `[...]` | Hardware inventory array (`Thermostat`, `EquipmentInterfaceModule`, `IndoorAirSensor`) with `HardwareRevision` (e.g. `"DENALI_TSTAT-001"`), `SoftwareRevision`, `SerialNumber`, and `RssiAverage` |

---

## 6. Device Capabilities Schema (`/v2/devices/thermostats/{mac}/configuration`)

`GET /ris-public-api/api/v2/devices/thermostats/{mac}/configuration` reports hardware capabilities, limits, and installed equipment under `Reported`:

| Path in `Reported` | Type / Example | Description |
| --- | --- | --- |
| `SystemSwitchCapabilities` | `["EmergencyHeat", "Heat", "Off", "Cool", "Auto"]` | Supported HVAC modes |
| `FanSwitchCapabilities` | `{"Positions": ["On", "Auto", "Circulate"], "MaxSpeed": 1}` | Supported fan positions and speeds |
| `MinimumHeatSetpointAllowed` | `50.0` | User-configured heat setpoint floor (°F) |
| `MaximumHeatSetpointAllowed` | `90.0` | User-configured heat setpoint ceiling (°F) |
| `MinimumCoolSetpointAllowed` | `50.0` | User-configured cool setpoint floor (°F) |
| `MaximumCoolSetpointAllowed` | `90.0` | User-configured cool setpoint ceiling (°F) |
| `SetpointDeadband` | `0.0` / `2.0` | Minimum deadband between heat and cool setpoints in Auto mode (°F) |
| `TemperatureUnits` | `"F"` / `"C"` | Provisioning-time faceplate unit snapshot (see [§7](#7-temperature-units--regional-quirks); `TemperatureUnitsWritable` is `false`) |
| `SupportedCapabilities` | `["AccessoryValues", "Fan", "FeelsLike", ...]` | Feature capability flags |
| `SystemConfiguration` | `{"HeatingSystem": "CompressorHeat", "HeatingStages": 3, "CoolingSystem": "CompressorCool", "CoolingStages": 2}` | Configured HVAC equipment type and stage counts |
| `StageConfigurationIndoorAirQuality` | `{"Humidifier": true, "Dehumidifier": true, "Ventilator": false}` | IAQ equipment relay configuration |
| `HumidifierEnabled` | `true` | Whether a whole-home humidifier is enabled |
| `HumidificationMinimumSetpoint` | `0` | Minimum humidifier setpoint (% RH; app clamps slider to `10%`) |
| `HumidificationMaximumSetpoint` | `90` | Maximum humidifier setpoint (% RH) |
| `HumidifierDeadbandEnabled` / `HumidifierDeadband` | `true` / `15` | Humidifier deadband (%) |
| `DehumidifierEnabled` | `true` | Whether a whole-home dehumidifier is enabled |
| `DehumidificationMinimumSetpoint` | `10` | Minimum dehumidifier setpoint (% RH) |
| `DehumidificationMaximumSetpoint` | `100` | Maximum dehumidifier setpoint (% RH; app clamps slider to `90%`) |
| `FreezeProtection` | `{"Configured": true, "Active": false, "LowLimitDegrees": 40}` | Freeze protection availability, active status, and floor (`35`–`45` °F) |
| `AllowedAdaptiveRecoveryModes` | `["None", "AdaptiveIntelligentRecovery"]` | Supported Smart Response modes |
| `ScheduleCapabilities` | `{"AvailableScheduleTypes": ["None", "Geofenced", "GeofencedWithSleep", "TimedNorthAmerica"], "SchedulableFan": true}` | Supported schedule types |
| `BackLightIntensity` / `MinimumBackLightIntensity` / `MaximumBackLightIntensity` | `10` / `0` / `10` | Backlight brightness settings |
| `AirFilterReminders.1` | `{"IsCalendarDays": false, "IsReminderEnabled": true, "RestartDays": 180}` | Filter reminder life setting |
| `FaultThresholds` | `{"TemperatureHighThreshold": 90.0, "TemperatureLowThreshold": 50.0, "HumidityHighThreshold": 70, "HumidityLowThreshold": 20}` | Alert threshold configuration |
| `Matter` | `{"CommissioningStatus": "Commissioned", "VendorId": 4890, "ProductId": 4352, "Fabrics": [...]}` | Matter commissioning status and paired fabrics |
| `CommercialConfiguration` | `false` | Whether the thermostat is in commercial mode |
| `AwayModeOverride` | `false` | Away mode override flag |

---

## 7. Temperature Units & Regional Quirks

1. **All Indoor Temperatures, Duct Temperatures, Setpoints & Limits Are Always `°F`**:
   - Regardless of `Reported.TemperatureUnits` (`"F"` or `"C"`), the user's app preference, or the thermostat faceplate setting, every indoor temperature, accessory temperature, duct temperature, setpoint, and setpoint limit in REST responses, `PUT` writes, and SignalR `LiveFeedEvent` pushes is **always in Fahrenheit (`°F`)**.
   - Switching a thermostat faceplate and the First Alert app to `°C` changes **no payload value** (and does not even update `/configuration` `Reported.TemperatureUnits`, which is a provisioning-time snapshot with `TemperatureUnitsWritable: false`).
2. **The One Exception — `DisplayedOutdoorTemperature`**:
   - `DisplayedOutdoorTemperature` is populated by Resideo's weather service in the unit local to the account's `data.countryCode` from `GET /ris-public-api/api/v1/accounts` (**not** `TemperatureUnits` and **not** `locale`).
   - Accounts with `countryCode` in `{"US", "PR", "GU", "VI", "AS", "MP", "UM", "BS", "BZ", "KY", "LR", "PW", "FM", "MH"}` receive `DisplayedOutdoorTemperature` in **`°F`**; all other countries (e.g., `"CA"` for Canada, even with `locale: "en-US"`) receive `DisplayedOutdoorTemperature` in **`°C`**.

---

## 8. Error Responses & Cloud Deprecation Detection

- **`400 Bad Request`**: Returned when a command payload fails validation. ASP.NET / APIM returns structured JSON validation errors:
  ```json
  [
    {
      "ErrorCode": "ParameterIsMissing",
      "Message": "The SetPointValue field is required.",
      "ParamName": "SetPointValue",
      "ErrorId": "DS0004"
    }
  ]
  ```
- **`401 Unauthorized`**: Access token expired or invalid. Refresh the token once and retry.
- **`404 Resource Not Found`**: Returned by Azure APIM (`{"statusCode": 404, "message": "Resource not found"}`) when calling a `v1`-only path under `v2` or an unmapped command route.
- **`503 Service Unavailable`**:
  - Resideo's edge returns `503` with `{"message": "The API is temporarily down for planned maintenance."}` both during real cloud outages **and** permanently when an API host or path is retired (as happened to `api.resideo.com` in September 2026).

---

## 9. Real-Time Push Streaming (Azure SignalR)

The consumer API streams real-time state updates over Azure SignalR WebSockets (`json` protocol v1, frames terminated by ASCII Record Separator `0x1e`).

### 9.1 Two-Step Negotiate & Feed Activation

Establishing a working stream requires **three mandatory steps** per location:

1. **Step 1 — Titans Hub Negotiate (`POST https://api.ha.resideo.com/ds-notification-service/Hub/negotiate?negotiateVersion=1`)**:
   - Authenticated with the standard Auth0 `Bearer` token + `Ocp-Apim-Subscription-Key`.
   - Returns the Azure SignalR service URL and a short-lived Azure JWT:
     ```json
     {
       "url": "https://resideo-prod.service.signalr.net/client/?hub=datasynchub&...",
       "accessToken": "eyJhbGciOiJIUzI1NiIs..."
     }
     ```
2. **Step 2 — Azure Client Negotiate (`POST https://resideo-prod.service.signalr.net/client/negotiate?hub=datasynchub&...`)**:
   - Insert `negotiate` before the query string in `url`.
   - Authenticated with `Authorization: Bearer <azure_jwt>` **only** (no Auth0 token, no APIM key).
   - Returns `{"connectionId": "...", "connectionToken": "..."}`.
   - **Why Step 2 is mandatory**: Connecting to the WebSocket with only `access_token=<azure_jwt>` (skipping `/client/negotiate` and `&id=<connectionToken>`) still accepts `SubscribeSignalRV2` and receives `ChangeRequestSuccess` events, but **silently never delivers `LiveFeedEvent`s**.
3. **Step 3 — WebSocket Handshake, Subscribe & `/priority` Activation**:
   - Connect to `wss://resideo-prod.service.signalr.net/client/?hub=datasynchub&...&id=<url_encoded_connectionToken>&access_token=<azure_jwt>`.
   - Send handshake frame: `{"protocol":"json","version":1}\x1e` and await `{}\x1e`.
   - Send hub invocation `SubscribeSignalRV2` with the location's base64 `node_id`:
     ```json
     {"type":1,"invocationId":"1","target":"SubscribeSignalRV2","arguments":["Q29uc3VtZXJEZXZpY2VMb2NhdGlvbjow..."]}\x1e
     ```
   - **Activate the live feed**: Perform `GET /ris-public-api/api/v1/devices/thermostats/{mac}/priority` for each thermostat at the location. Reading `/priority` triggers the cloud data-sync service to begin pushing `LiveFeedEvent` frames for that thermostat.

### 9.2 Keepalive & ~12-Minute Feed Lifetime Renewal

- **WebSocket Ping**: Send `{"type":6}\x1e` every `15` seconds. If no inbound frames (including server `{"type":6}` pings) arrive within `45` seconds, treat the socket as stalled and reconnect.
- **Fixed ~12-Minute Feed Window (`SubscriptionExpiration`)**:
  - Each `LiveFeedEvent` carries `Body.SubscriptionExpiration` (ISO-8601 timestamp ~12 minutes after activation).
  - Neither `HeartbeatV2` hub invocations nor re-reading `/priority` on an existing socket extends `SubscriptionExpiration`.
  - To maintain an uninterrupted stream, proactively close and reconnect (`negotiate` → `SubscribeSignalRV2` → `GET /priority`) `120` seconds before `SubscriptionExpiration` (or after `600` seconds if no plausible future `SubscriptionExpiration` has been stamped).
  - *Quirk — Stale `SubscriptionExpiration` Stamps*: The cloud occasionally stamps every push with a `SubscriptionExpiration` that is already several minutes in the past. Ignore any `SubscriptionExpiration` that is `<= now + 120s` so the client does not enter a 15-second reconnect loop.

### 9.3 SignalR Event Envelope (`target: "events"`)

Inbound SignalR invocations (`"type": 1, "target": "events"`) carry a JSON-encoded string in `arguments[0]`:

#### A. Command Confirmation (`ChangeRequestSuccess` / `ChangeRequestFailure`)
Carries **no values**; confirms whether a `202` REST command succeeded or failed. Settings that do not emit a `LiveFeedEvent` (such as `FeelsLike`, `AdaptiveIntelligentRecovery`, `ScheduleEnabled`, `SetPointCapabilities`) rely on a debounced REST resync (`~3s`) after `ChangeRequestSuccess`.
```json
{
  "DeviceId": "AABBCCDDEEFF",
  "NotificationType": "ChangeRequestSuccess",
  "Body": {
    "TransactionId": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "ChangeName": "CoolSetpoint",
    "ChangeDirection": "CloudToDevice"
  }
}
```

#### B. Live State Delta (`LiveFeedEvent`)
Carries real-time state deltas in `Body.PropertyName` and `Body.Value`. Note that `LiveFeedEvent` payload shapes often differ from the REST shadow and must be mapped into the cached shadow/rooms state:

| `Body.PropertyName` | `Body.Value` Shape | Shadow / Rooms Mapping |
| --- | --- | --- |
| `Setpoint` | `{"Status": "PermanentHold", "HeatSetpoint": 64, "CoolSetpoint": 77, "FanSwitch": {"Position": "On"}}` | `Status` → `Reported.Setpoint.SetpointStatus`; `HeatSetpoint`/`CoolSetpoint` → `Reported.Setpoint.*`; `FanSwitch.Position` → both `Reported.FanSwitch.Position` and `Reported.Setpoint.FanSwitch.Position`. |
| `OperationStatus` | `{"curStg": 2, "Mode": "Heat", "Fan": false, "CircFan": true}` | `Mode` → `Reported.OperationStatus.Mode`; `Fan` → `Reported.OperationStatus.FanRequest`; `CircFan` → `Reported.OperationStatus.CirculationFanRequest`; `curStg` → `Reported.HeatAndCoolDemand.CurrentStage`. |
| `SystemSwitch` | `{"SystemSwitch": "Heat", "HeatCoolMode": "Heat"}` | `Reported.SystemSwitch` and `Reported.HeatCoolMode`. |
| `FanSwitch` | `{"Position": "On", "Speed": 1}` | `Reported.FanSwitch.{Position, Speed}` and `Reported.Setpoint.FanSwitch.Position`. |
| `DisplayedIndoorTemperature` | `{"Value": 72.0, "Sensor": "Ok"}` | `Reported.DisplayedIndoorTemperature = Value`. |
| `DisplayedIndoorHumidity` | `{"Value": 54, "Sensor": "Ok"}` | `Reported.DisplayedIndoorHumidity = Value`. |
| `DisplayedOutdoorTemperature` | `{"Value": 90, "Sensor": "Ok"}` | `Reported.DisplayedOutdoorTemperature = Value` (pushed every ~30 min). |
| `DisplayedOutdoorHumidity` | `{"Value": 65, "Sensor": "Ok"}` | `Reported.DisplayedOutdoorHumidity = Value`. |
| `DuctTemperature` | `{"DischargeTemp": 84.5, "ReturnTemp": 72.5}` (or wrapped in `{"Value": ...}`) | `DischargeTemp` / `DischargeTemperature` → `Reported.DuctTemperature.DischargeTemperature`; `ReturnTemp` / `ReturnTemperature` → `Reported.DuctTemperature.ReturnTemperature`. |
| `Humidification` | `{"HumidifierStatus": "On", "DehumidifierStatus": "Off", "HumidifierSetpoint": 42, "DehumidifierSetpoint": 58, "WindowsProtection": 5, "WindowsProtectionPercentage": 85.0}` | Merges setpoints/window protection into `Reported.*` and maps `HumidifierStatus` / `DehumidifierStatus` (`"On"` → `1`, `"Off"` → `0`) into `Reported.HumidifierRequest` / `Reported.DehumidifierRequest`. |
| `Rooms` | `{"PropertyName": "0", "Value": {"Id": 0, "Name": "Kitchen", "AvgTemperature": 79, "AvgHumidity": 49, "OverallMotion": false}}` | Updates aggregate fields on the matching `Rooms[i]` entry without overwriting `Rooms[i].Accessories`. |
| `Sensor` | `{"RoomId": 0, "AccessoryId": 0, "AccessoryAttribute": {...}, "AccessoryValue": {...}}` | Per-accessory telemetry delta. **Warning: Header is off-by-one** (see [§10.2](#102-the-signalr-sensor-off-by-one-header-bug--eim-routing)). |

---

## 10. Rooms, EIM & Wireless Room Sensors (`Model 4354`)

### 10.1 Accessory Inventory in `/group/0/rooms`

`GET /ris-public-api/api/v1/devices/thermostats/{mac}/group/0/rooms` returns an array of `Rooms`, each containing an `Accessories` array. Three accessory types appear across `Rooms[].Accessories[]`:

1. **`Thermostat` (`AccessoryId: 0`, `ConnectionMethod: "WI"`)**:
   - The built-in wall unit sensor in Room `0`.
   - Carries local `IndoorTemperature`, `TemperatureActual` (3-decimal thermistor reading, e.g. `71.093`), `IndoorHumidity`, `MotionDet`, `OccupancyDet`, `RssiAverage` (Wi-Fi dBm), and (on IAQ-capable models) `CarbonDioxide: {"Measurement": 425.0, "Displayed": true}` and `TotalVolatileOrganicCompounds: {"Measurement": 5.0, "Displayed": true}`.
2. **`EquipmentInterfaceModule` (`AccessoryId: 1`, `Model: "4353"`, `ConnectionMethod: "RL"`)**:
   - Present when the thermostat is wired through a RedLINK Equipment Interface Module (EIM). Appears in Room `0` with `AccessoryId: 1` and `AccessoryValue: {"Status": "Ok"}`.
3. **`IndoorAirSensor` (`Model: "4354"`, `ConnectionMethod: "RL"`)**:
   - Battery-powered RedLINK wireless room sensors (e.g. C7189R3002 / Model 4354).
   - Assigned sequential `AccessoryId`s starting after the thermostat and EIM (e.g. `AccessoryId: 1` without an EIM, or `AccessoryId: 2` when an EIM is `AccessoryId: 1`).
   - `AccessoryValue` fields:
     ```json
     {
       "IndoorHumidity": 36,
       "MotionDet": false,
       "OccupancyDet": false,
       "IndoorTemperature": 67.0,
       "ExcludeTemp": false,
       "ExcludeMotion": false,
       "OccupancySensitivity": "High",
       "OccupancyTimeout": 0,
       "Status": "Ok",
       "BatteryStatus": "Ok",
       "RssiAverage": -48,
       "PollutantLevels": [],
       "TemperatureActual": 66.852
     }
     ```

### 10.2 The SignalR `Sensor` Off-By-One Header Bug & EIM Routing

Resideo's cloud has a critical bug in `LiveFeedEvent` notifications where `PropertyName == "Sensor"`: **every push's header (`RoomId`, `AccessoryId`, `AccessoryAttribute`, and `AccessoryValue.Id`) is off by one**, describing the accessory *immediately preceding* the one whose `AccessoryValue` readings are actually in the payload.

- **Without an EIM (`Accessories` = `[0: Thermostat, 1: Remote1, 2: Remote2]`)**:
  - Push with header `AccessoryId: 0` (`Type: "TS"`) + **no** `BatteryStatus` in `AccessoryValue` → **Thermostat (`0`)**.
  - Push with header `AccessoryId: 0` (`Type: "TS"`) + **`BatteryStatus` present** in `AccessoryValue` → **First Remote Sensor (`1`)**.
  - Push with header `AccessoryId: 1` (`Type: "IS"`, first remote's serial/name) + `BatteryStatus` → **Second Remote Sensor (`2`)**.
- **With an EIM (`Accessories` = `[0: Thermostat, 1: EquipmentInterfaceModule, 2: Remote1, ...]`)**:
  - Because the EIM occupies `AccessoryId: 1` in sorted `AccessoryId` order, the first wireless remote sensor (`AccessoryId: 2`) arrives under header **`AccessoryId: 1` (`Type: "EI"`, `Name: "Other"`)**.
- **Deterministic Routing Rule**:
  1. Sort all cached accessories across all rooms by `AccessoryId`.
  2. If `"BatteryStatus"` is **not** in `push["AccessoryValue"]`, route to the unique `Thermostat` accessory (`AccessoryId: 0`).
  3. If `"BatteryStatus"` **is** in `push["AccessoryValue"]`, find the pair `(header, successor)` in `pairwise(sorted_accessories)` where `header["AccessoryId"] == push["AccessoryId"]` and route to `successor` (verifying `successor` is an `IndoorAirSensor`). Strip `AccessoryValue.Id` before merging.
  4. When merging nested `{Measurement, Displayed}` dicts (`CarbonDioxide`, `TotalVolatileOrganicCompounds`), preserve or default `"Displayed": True` because SignalR `Sensor` pushes omit the `"Displayed"` key.

### 10.3 Wireless Sensor Motion & Occupancy Behavior (`Model 4354`)

Live walk-test telemetry on `Model 4354` wireless room sensors established the following cloud behavior:
- **`AccessoryValue.MotionDet`**: Remains `false` on `Model 4354` wireless sensors in both REST `/group/0/rooms` and SignalR `Sensor` pushes.
- **`AccessoryValue.OccupancyDet`**: Updates in real time via SignalR `Sensor` pushes when motion is detected (`true`) and clears after the sensor's occupancy hold window.
- **`Rooms[i].OverallMotion` (and `/priority` `DetectMotion`)**: Triggers quickly (~30–40 seconds) via a `LiveFeedEvent` `Rooms` push when motion begins, **but latches `true` in Resideo's cloud for 45+ minutes** after motion ceases. Do **not** use `Rooms[i].OverallMotion` to drive a real-time motion binary sensor, as it will stick `on` long after the room is vacated.
