"""Constants for the Spot Price custom integration."""

DOMAIN = "spot_price"
NAME = "Spot Price"
VERSION = "1.0.0"
PLATFORMS = ["sensor"]

# --- remote APIs -----------------------------------------------------------
API_URL = "https://api.eupowerprices.com/presentation/v1/markets"
API_AREAS_URL = "https://api.eupowerprices.com/presentation/v1/areas"
# Official keyed API (access must be requested; authenticate with X-API-Key).
API_V1_FORECAST_URL = "https://api.eupowerprices.com/v1/forecasts"
API_V1_AREAS_URL = "https://api.eupowerprices.com/v1/areas"
# Official keyed status endpoint: returns API status + the areas for the key.
API_STATUS_URL = "https://api.eupowerprices.com/v1/status"
FX_URL = "https://api.frankfurter.app/latest"
FRANKFURTER_BASE = "EUR"

# --- config keys ------------------------------------------------------------
CONF_API_KEY = "api_key"
CONF_AREA = "area"
CONF_CURRENCY = "currency"
CONF_FX_MODE = "fx_mode"
CONF_FIXED_FX = "fixed_fx"
CONF_VAT_PCT = "vat_pct"
CONF_GRID_FEE = "grid_fee_sek_kwh"
CONF_WINDOW_HOURS = "window_hours"
CONF_LOW_THRESHOLD = "low_price_threshold_sek"
CONF_UPDATE_INTERVAL = "update_interval_minutes"
CONF_FORECAST_DAYS = "forecast_days"

FX_MODE_LIVE = "live"
FX_MODE_FIXED = "fixed"

# --- defaults ---------------------------------------------------------------
DEFAULT_AREA = "SE4"
DEFAULT_CURRENCY = "EUR"
DEFAULT_FX_MODE = FX_MODE_LIVE
DEFAULT_FIXED_FX = 1.0
DEFAULT_VAT_PCT = 25.0
DEFAULT_GRID_FEE = 0.0
DEFAULT_WINDOW_HOURS = 3
DEFAULT_LOW_THRESHOLD = None
DEFAULT_UPDATE_INTERVAL = 360  # minutes between background refreshes
DEFAULT_FORECAST_DAYS = 14  # days of future prices to fetch and expose

# --- areas ------------------------------------------------------------------
# Area codes exposed by the site (Nordics / Baltics / Core / Iberia). The
# config flow tries to fetch the live list from the API first (public endpoint
# at setup, key-scoped /v1/status in the options) and falls back to this
# curated list, sorted A-z, only when the API cannot be reached. Users can
# always type a custom code; it is validated live against the API on submit.
SUPPORTED_AREAS = [
    "AT", "BE", "CZ", "DE", "DK1", "DK2", "EE", "ES", "FI", "FR",
    "LT", "LV", "NL", "NO1", "NO2", "NO3", "NO4", "NO5", "PL", "PT",
    "SE1", "SE2", "SE3", "SE4", "SK",
]

# --- currencies --------------------------------------------------------------
# End-user price currency. The offered list is derived from the areas above —
# every area's local currency, nothing more, nothing less — so it always
# matches the bidding zones the integration supports. EUR is the default; any
# ECB (Frankfurter) reference currency that appears here works.
AREA_CURRENCIES = {
    "SE1": "SEK", "SE2": "SEK", "SE3": "SEK", "SE4": "SEK",
    "DK1": "DKK", "DK2": "DKK",
    "FI": "EUR",
    "NO1": "NOK", "NO2": "NOK", "NO3": "NOK", "NO4": "NOK", "NO5": "NOK",
    "EE": "EUR", "LT": "EUR", "LV": "EUR",
    "AT": "EUR", "BE": "EUR", "CZ": "CZK", "DE": "EUR", "ES": "EUR",
    "FR": "EUR", "NL": "EUR", "PL": "PLN", "PT": "EUR", "SK": "EUR",
}

SUPPORTED_CURRENCIES = sorted({AREA_CURRENCIES[a] for a in SUPPORTED_AREAS})

# --- request window ---------------------------------------------------------
# The eupowerprices API serves a rolling forecast of at most ~14 days. Probing
# with `to` = now + 7/14/21/30/45/60/90/120/180 days shows the forecast series
# is silently capped ~13 days past now; every window beyond ~14 days returns
# the *identical* payload and never errors, so longer requests are pure waste.
# MAX_FORECAST_DAYS is the hard ceiling enforced on the "forecast period"
# user option (see config_flow / coordinator clamping).
MIN_FORECAST_DAYS = 1
MAX_FORECAST_DAYS = 14
FORECAST_RANGE_DAYS = MAX_FORECAST_DAYS  # default/fallback fetch horizon
FETCH_BACK_HOURS = 36  # ensures the whole current local day is covered by `actual`

# Home Assistant's recorder refuses to persist state attributes beyond 16,384
# bytes (serialized JSON) and logs "State attributes ... exceed maximum size".
# The pretty per-hour shape (`start`, `eur_mwh`, `sek_kwh`, `source`) measures
# ~33 kB for a full 14-day forecast, so the `forecast` sensor exposes a compact
# `hours` shape instead — `[{s: unix_epoch, p: end-user price}, ...]` — which
# stays at ~10 kB with the per-day `days` summary appended
# (see helper.compact_hours).

# --- units ------------------------------------------------------------------
UNIT_EUR_PER_MWH = "EUR/MWh"


def unit_price_per_kwh(currency: str) -> str:
    """Unit string for end-user prices in the chosen currency."""
    return f"{currency}/kWh"


def unit_eur_to(currency: str) -> str:
    """Unit string for the EUR->currency FX rate sensor."""
    return f"EUR/{currency}"

# --- HTTP -------------------------------------------------------------------
# Browser-like request headers. The eupowerprices site is fronted by
# Cloudflare; presenting a normal Chrome session keeps our fetches from
# being mistaken for a script/falling into bot filtering. Keep the request
# volume low (a few times a day is plenty - the API exposes no rate limits).
#
# Note: "Accept-Encoding" deliberately omits "br" so aiohttp can transparently
# decompress the response without extra dependencies.
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,sv-SE;q=0.8,sv;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", '
    '"Google Chrome";v="126"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}