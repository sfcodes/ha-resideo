"""Constants for aioresideo — the private Resideo consumer API (api.resideo.com).

These values are reverse-engineered from the Resideo / First Alert mobile app and
verified live (see ``resideo-api-spec.md`` §1). They use the app's own public Auth0
client, so **no developer.honeywellhome.com account is required**.
"""

from __future__ import annotations

# --- Auth0 (login.resideo.com) -----------------------------------------------
OAUTH_CLIENT_ID = "SRmiA7CaYi1JgivDZdzzoZu4X5VBogGt"

AUTH0_BASE_URL = "https://login.resideo.com"
AUTH0_AUTHORIZE_URL = f"{AUTH0_BASE_URL}/authorize"
AUTH0_LOGIN_PAGE_URL = f"{AUTH0_BASE_URL}/login"
AUTH0_LOGIN_URL = f"{AUTH0_BASE_URL}/usernamepassword/login"
AUTH0_CALLBACK_URL = f"{AUTH0_BASE_URL}/login/callback"
OAUTH_TOKEN_URL = f"{AUTH0_BASE_URL}/oauth/token"

REDIRECT_URI = "com.resideo.firstalert://login.resideo.com/ios/com.resideo.firstalert/callback"
AUDIENCE = "https://resideo-prod.auth0.com/api/v2/"
SCOPE = "openid profile email offline_access"
TENANT = "resideo-prod"
CONNECTION = "Username-Password-Authentication"
SIGN_UP_URL = "https://myid.resideo.com/sign-up?userType=consumer"

# Auth0 client identifiers (base64 JSON), taken verbatim from the app.
AUTH0_CLIENT_BROWSER = "eyJuYW1lIjoiYXV0aDAuanMtdWxwIiwidmVyc2lvbiI6IjkuMTMuMiJ9"
AUTH0_CLIENT_APP = (
    "eyJ2ZXJzaW9uIjoiMS4xNC4wIiwibmFtZSI6ImF1dGgwLWZsdXR0ZXIiLCJlbnYiOnsiY29yZSI6"
    "IjIuMTAuMCIsImlPUyI6IjI2LjEiLCJzd2lmdCI6IjUueCJ9fQ"
)

# --- api.resideo.com ---------------------------------------------------------
API_BASE_URL = "https://api.resideo.com"

# Azure APIM subscription key (prod) — mandatory on the devsrv command service.
OCP_APIM_SUBSCRIPTION_KEY = "b60885e8a9b44680a29ea1f03452878a"

# Service bases (see resideo-api-spec.md §2). ``{mac}`` = raw device MAC, e.g. 5CFCE1B7F5BA.
DEVSRV_DEVICE = "/devsrv/api/v2/device/{mac}"  # thermostat state + commands (primary)
RIS_PUBLIC_API = "/ris-public-api/api/v1"
ACCOUNTS_ENDPOINT = f"{RIS_PUBLIC_API}/accounts"

# Every write body carries this channel id; writes return ``202 {"TransactionId": ...}``.
DEFAULT_CHANNEL_ID = "ds-notification-service"

# User agents: browser-ish for the Auth0 web flow, the real app UA for API calls.
WEB_USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)"
APP_USER_AGENT = "First Alert/2440 CFNetwork/3860.600.12 Darwin/25.5.0"

REQUEST_TIMEOUT = 30  # seconds
TOKEN_REFRESH_MARGIN = 300  # refresh when <5 min to access-token expiry

# --- real-time push (Azure SignalR; see resideo-api-spec.md §9) ---------------
SIGNALR_NEGOTIATE_URL = (
    "https://ds-notification-service.prod.titans.cloud/Hub/negotiate?negotiateVersion=1"
)
SIGNALR_RECORD_SEPARATOR = "\x1e"  # SignalR frame delimiter
SIGNALR_HANDSHAKE = {"protocol": "json", "version": 1}
SIGNALR_PING_INTERVAL = 15  # seconds — own keepalive ping {"type":6}
# The values feed has a fixed ~12-min lifetime per activation and is NOT extended by HeartbeatV2 /
# re-reading /priority (verified live). To stay live, reconnect (fresh negotiate+subscribe+activate)
# shortly before the feed's SubscriptionExpiration — that starts a fresh window.
SIGNALR_FEED_RECONNECT_MARGIN = 120  # reconnect this many sec before SubscriptionExpiration
SIGNALR_FEED_TTL_FALLBACK = 600  # assume this feed lifetime (sec) until a SubscriptionExpiration is seen
SIGNALR_STALL_TIMEOUT = 45  # no frames for this long -> force a reconnect (watchdog)

# --- value sets (from GET .../configuration; see spec §4) --------------------
# SystemSwitchValue (mode)
SYSTEM_SWITCH_HEAT = "Heat"
SYSTEM_SWITCH_COOL = "Cool"
SYSTEM_SWITCH_OFF = "Off"
SYSTEM_SWITCH_AUTO = "Auto"
SYSTEM_SWITCH_EMERGENCY_HEAT = "EmergencyHeat"

# FanSwitch.Position
FAN_AUTO = "Auto"
FAN_ON = "On"
FAN_CIRCULATE = "Circulate"

# SetpointStatus (hold)
SETPOINT_NO_HOLD = "NoHold"
SETPOINT_TEMPORARY_HOLD = "TemporaryHold"
SETPOINT_PERMANENT_HOLD = "PermanentHold"
SETPOINT_HOLD_UNTIL = "HoldUntil"
SETPOINT_VACATION_HOLD = "VacationHold"

# PriorityType
PRIORITY_PICK_A_ROOM = "PickARoom"
PRIORITY_FOLLOW_ME = "FollowMe"

# adaptiveIntelligentRecovery.Mode (enum AdaptiveRecoveryModeEnumRequest; see spec §4)
ADAPTIVE_RECOVERY_NONE = "None"  # off
ADAPTIVE_RECOVERY_INTELLIGENT = "AdaptiveIntelligentRecovery"
ADAPTIVE_RECOVERY_DELAYED_START = "DelayedStartRecovery"

# accessories/{id}/accessoryValue "sensitivity" (read key OccupancySensitivity; stored 0-3; spec §10)
OCCUPANCY_SENSITIVITY_OFF = "Off"
OCCUPANCY_SENSITIVITY_LOW = "Low"
OCCUPANCY_SENSITIVITY_MEDIUM = "Medium"
OCCUPANCY_SENSITIVITY_HIGH = "High"
OCCUPANCY_SENSITIVITY_OPTIONS = (
    OCCUPANCY_SENSITIVITY_OFF,
    OCCUPANCY_SENSITIVITY_LOW,
    OCCUPANCY_SENSITIVITY_MEDIUM,
    OCCUPANCY_SENSITIVITY_HIGH,
)
