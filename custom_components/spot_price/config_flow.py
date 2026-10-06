"""Config flow for the Spot Price custom integration."""

from __future__ import annotations

import asyncio
import re
from typing import Any, Optional

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import aiohttp_client, selector

from .const import (
    API_AREAS_URL,
    API_STATUS_URL,
    API_V1_AREAS_URL,
    CONF_API_KEY,
    CONF_AREA,
    CONF_CURRENCY,
    CONF_FIXED_FX,
    CONF_FORECAST_DAYS,
    CONF_FX_MODE,
    CONF_GRID_FEE,
    CONF_UPDATE_INTERVAL,
    CONF_VAT_PCT,
    CONF_WINDOW_HOURS,
    DEFAULT_AREA,
    DEFAULT_CURRENCY,
    DEFAULT_FIXED_FX,
    DEFAULT_FORECAST_DAYS,
    DEFAULT_FX_MODE,
    DEFAULT_GRID_FEE,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_VAT_PCT,
    DEFAULT_WINDOW_HOURS,
    DOMAIN,
    FX_MODE_FIXED,
    FX_MODE_LIVE,
    MAX_FORECAST_DAYS,
    MIN_FORECAST_DAYS,
    NAME,
    REQUEST_HEADERS,
    SUPPORTED_AREAS,
    SUPPORTED_CURRENCIES,
    normalize_area,
    _num,
    _clamp_forecast_days,
)

# The API's area codes look like SE1–SE4, NO2, DK1, BE, AT, ... (2-6 chars).
AREA_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{1,5}$")










def _extract_area_codes(payload: Any) -> set[str]:
    """Best-effort extraction of area codes across API response shapes."""
    codes: set[str] = set()
    if not isinstance(payload, dict):
        return codes
    areas = payload.get("areas")
    if areas is None:
        data = payload.get("data")
        if isinstance(data, dict):
            areas = data.get("areas")
    if isinstance(areas, dict):
        areas = list(areas)  # e.g. {code: ...} -> codes are the keys
    if isinstance(areas, str):
        areas = [areas]
    if not isinstance(areas, list):
        return codes
    for entry in areas:
        if isinstance(entry, str):
            codes.add(entry.upper())
        elif isinstance(entry, dict):
            code = entry.get("code") or entry.get("area") or entry.get("name")
            if isinstance(code, str):
                codes.add(code.upper())
    return codes


async def _async_fetch_areas(session, url: str, headers: dict) -> Optional[Any]:
    """Fetch an areas endpoint; returns the JSON payload or None on any error."""
    try:
        async with asyncio.timeout(10):
            async with session.get(url, headers=headers) as response:
                response.raise_for_status()
                return await response.json(content_type=None)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - never block setup on the network
        return None


async def _async_fetch_area_codes(hass, api_key: str = "") -> Optional[set[str]]:
    """Fetch the live area list; keyed endpoints first when a key is set.

    Returns None when every endpoint fails or returns an unparseable shape,
    in which case callers fall back to the curated static list.
    """
    session = aiohttp_client.async_get_clientsession(hass)
    attempts: list[tuple[str, dict]] = [(API_AREAS_URL, dict(REQUEST_HEADERS))]
    if api_key:
        keyed = {**dict(REQUEST_HEADERS), "X-API-Key": api_key}
        attempts[0:0] = [(API_STATUS_URL, keyed), (API_V1_AREAS_URL, keyed)]
    for url, headers in attempts:
        payload = await _async_fetch_areas(session, url, headers)
        if payload is None:
            continue
        codes = _extract_area_codes(payload)
        if codes:
            return codes
    return None


def area_options(codes: Optional[set[str]], *, keyed: bool = False) -> list[str]:
    """Dropdown options for the area selector, merged and sorted A-z.

    A plain (public) list is merged with the curated static list so the menu
    stays complete if the endpoint returns a partial/stale set. A key-scoped
    list (``keyed=True``) is authoritative instead: only the areas the key can
    actually access are offered. Without any live list the curated list is the
    fallback. Either way the result is sorted alphabetically.
    """
    if not codes:
        return sorted(SUPPORTED_AREAS)
    if keyed:
        return sorted(codes)
    return sorted(codes | set(SUPPORTED_AREAS))


async def async_area_error(hass, area: str, api_key: str = "") -> Optional[str]:
    """Return an error translation key for an invalid area, else None.

    The check is best-effort: if the API cannot be reached the input is
    accepted rather than blocking the setup on a network failure. With an API
    key the official /v1/areas endpoint is tried first (it lists the areas the
    key can actually access); the public areas endpoint is the fallback.
    """
    if not AREA_PATTERN.match(area):
        return "invalid_area_code"
    session = aiohttp_client.async_get_clientsession(hass)
    attempts = (
        [
            (
                API_V1_AREAS_URL,
                {**dict(REQUEST_HEADERS), "X-API-Key": api_key},
            ),
            (API_AREAS_URL, dict(REQUEST_HEADERS)),
        ]
        if api_key
        else [(API_AREAS_URL, dict(REQUEST_HEADERS))]
    )
    for url, headers in attempts:
        payload = await _async_fetch_areas(session, url, headers)
        if payload is None:
            continue
        codes = _extract_area_codes(payload)
        if not codes:
            continue  # unparseable shape -> try the next endpoint
        return None if area in codes else "unknown_area"
    return None


def _setup_current_values() -> dict[str, Any]:
    """Default values used to pre-fill the setup form (no entry exists yet)."""
    return {
        CONF_AREA: DEFAULT_AREA,
        CONF_CURRENCY: DEFAULT_CURRENCY,
        CONF_NAME: "",
        CONF_API_KEY: "",
        CONF_FX_MODE: DEFAULT_FX_MODE,
        CONF_FIXED_FX: DEFAULT_FIXED_FX,
        CONF_VAT_PCT: DEFAULT_VAT_PCT,
        CONF_GRID_FEE: DEFAULT_GRID_FEE,
        CONF_WINDOW_HOURS: DEFAULT_WINDOW_HOURS,
        CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL,
        CONF_FORECAST_DAYS: DEFAULT_FORECAST_DAYS,
    }



def _shared_schema(current: dict[str, Any], area_options: list[str]) -> vol.Schema:
    """Build the config/options form schema from a single source of truth."""
    return vol.Schema(
        {
            vol.Required(CONF_AREA, default=current.get(CONF_AREA, DEFAULT_AREA)): selector.selector(
                {
                    "select": {
                        "options": area_options,
                        "mode": "dropdown",
                        "custom_value": True,
                    }
                }
            ),
            vol.Required(
                CONF_CURRENCY, default=current.get(CONF_CURRENCY, DEFAULT_CURRENCY)
            ): selector.selector(
                {"select": {"options": SUPPORTED_CURRENCIES, "mode": "dropdown"}}
            ),
            vol.Optional(CONF_NAME, default=current.get(CONF_NAME, "")): selector.selector(
                {"text": {"type": "text"}}
            ),
            vol.Optional(CONF_API_KEY, default=current.get(CONF_API_KEY, "")): selector.selector(
                {"text": {"type": "password", "autocomplete": "off"}}
            ),
            vol.Optional(CONF_FX_MODE, default=current.get(CONF_FX_MODE, DEFAULT_FX_MODE)): selector.selector(
                {"select": {"options": [FX_MODE_LIVE, FX_MODE_FIXED], "mode": "dropdown"}}
            ),
            vol.Optional(
                CONF_FIXED_FX, default=current.get(CONF_FIXED_FX, DEFAULT_FIXED_FX)
            ): selector.selector(
                {"number": {"mode": "box", "min": 0, "max": 30, "step": 0.01}}
            ),
            vol.Optional(
                CONF_VAT_PCT, default=current.get(CONF_VAT_PCT, DEFAULT_VAT_PCT)
            ): selector.selector(
                {"number": {"mode": "box", "min": 0, "max": 100, "step": 0.1}}
            ),
            vol.Optional(
                CONF_GRID_FEE, default=current.get(CONF_GRID_FEE, DEFAULT_GRID_FEE)
            ): selector.selector(
                {"number": {"mode": "box", "min": 0, "max": 5, "step": 0.01}}
            ),
            vol.Optional(
                CONF_WINDOW_HOURS,
                default=current.get(CONF_WINDOW_HOURS, DEFAULT_WINDOW_HOURS),
            ): selector.selector(
                {"number": {"mode": "box", "min": 1, "max": 12, "step": 1}}
            ),
            vol.Optional(
                CONF_UPDATE_INTERVAL,
                default=current.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
            ): selector.selector(
                {"number": {"mode": "box", "min": 30, "max": 1440, "step": 30}}
            ),
            vol.Optional(
                CONF_FORECAST_DAYS,
                default=current.get(CONF_FORECAST_DAYS, DEFAULT_FORECAST_DAYS),
            ): selector.selector(
                {
                    "number": {
                        "mode": "box",
                        "min": MIN_FORECAST_DAYS,
                        "max": MAX_FORECAST_DAYS,
                        "step": 1,
                    }
                }
            ),
        }
    )



def _normalize_config(
    user_input: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
    """Merge user input over fallbacks and normalise every config value."""
    config = dict(current)
    config.update(user_input)
    config[CONF_AREA] = normalize_area(config.get(CONF_AREA))
    config[CONF_CURRENCY] = str(config.get(CONF_CURRENCY) or DEFAULT_CURRENCY).upper()
    config[CONF_API_KEY] = str(config.get(CONF_API_KEY) or "").strip()
    config[CONF_FX_MODE] = config.get(CONF_FX_MODE) or DEFAULT_FX_MODE
    config[CONF_FIXED_FX] = _num(config.get(CONF_FIXED_FX), DEFAULT_FIXED_FX)
    config[CONF_VAT_PCT] = _num(config.get(CONF_VAT_PCT), DEFAULT_VAT_PCT)
    config[CONF_GRID_FEE] = _num(config.get(CONF_GRID_FEE), DEFAULT_GRID_FEE)
    config[CONF_WINDOW_HOURS] = int(_num(config.get(CONF_WINDOW_HOURS), DEFAULT_WINDOW_HOURS))
    config[CONF_UPDATE_INTERVAL] = int(
        _num(config.get(CONF_UPDATE_INTERVAL), DEFAULT_UPDATE_INTERVAL)
    )
    config[CONF_FORECAST_DAYS] = _clamp_forecast_days(config.get(CONF_FORECAST_DAYS))
    return config



class EupowerpricesConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle an initial configuration via the UI."""

    VERSION = 1

    async def async_step_user(
        self, user_input: Optional[dict[str, Any]] = None
    ) -> FlowResult:
        if getattr(self, "_area_options", None) is None:
            # The API key is entered on this same form, so the very first
            # dropdown can only be driven by the public areas endpoint. Custom
            # key-scoped codes can still be typed; submitting re-validates
            # against the keyed endpoints whenever a key is present.
            codes = await _async_fetch_area_codes(self.hass)
            self._area_options = area_options(codes)

        errors: dict[str, str] = {}
        if user_input is not None:
            config = _normalize_config(user_input, _setup_current_values())
            area = config[CONF_AREA]
            error = await async_area_error(self.hass, area, config.get(CONF_API_KEY, ""))
            if error is not None:
                current = _setup_current_values()
                current[CONF_AREA] = area
                errors[CONF_AREA] = error
                return self.async_show_form(
                    step_id="user",
                    data_schema=_shared_schema(current, self._area_options),
                    errors=errors,
                    last_step=True,
                )
            return self.async_create_entry(
                title=str(config.get(CONF_NAME, "")).strip() or NAME,
                data=config,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=_shared_schema(_setup_current_values(), self._area_options),
            errors=errors,
            last_step=True,
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "EupowerpricesOptionsFlow":
        return EupowerpricesOptionsFlow(config_entry)


class EupowerpricesOptionsFlow(config_entries.OptionsFlow):
    """Options: area, FX mode, VAT, grid fee, cheapest-window size, forecast period."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry
        self._area_options: Optional[list[str]] = None

    async def async_step_init(
        self, user_input: Optional[dict[str, Any]] = None
    ) -> FlowResult:
        if self._area_options is None:
            # The key is already known here, so drive the dropdown from the
            # key-scoped endpoints (the areas this key can actually access).
            api_key = str(
                self._entry.options.get(CONF_API_KEY)
                or self._entry.data.get(CONF_API_KEY)
                or ""
            ).strip()
            codes = await _async_fetch_area_codes(self.hass, api_key)
            self._area_options = area_options(codes, keyed=bool(api_key))

        errors: dict[str, str] = {}
        if user_input is not None:
            config = _normalize_config(user_input, self._current_values())
            area = config[CONF_AREA]
            error = await async_area_error(self.hass, area, config.get(CONF_API_KEY, ""))
            if error is not None:
                current = self._current_values()
                current[CONF_AREA] = area
                errors[CONF_AREA] = error
                return self.async_show_form(
                    step_id="init",
                    data_schema=_shared_schema(current, self._area_options),
                    errors=errors,
                    last_step=True,
                )
            # Reflect name changes in the config-entry title.
            new_title = str(config.get(CONF_NAME, "")).strip() or NAME
            if new_title != (self._entry.title or ""):
                self.hass.config_entries.async_update_entry(self._entry, title=new_title)
            return self.async_create_entry(
                title="",
                data=config,
            )

        return self.async_show_form(
            step_id="init",
            data_schema=_shared_schema(self._current_values(), self._area_options),
            errors=errors,
            last_step=True,
        )

    def _current_values(self) -> dict[str, Any]:
        return {
            CONF_AREA: self._entry.options.get(
                CONF_AREA, self._entry.data.get(CONF_AREA, DEFAULT_AREA)
            ),
            CONF_CURRENCY: self._entry.options.get(
                CONF_CURRENCY,
                self._entry.data.get(CONF_CURRENCY, DEFAULT_CURRENCY),
            ),
            CONF_NAME: self._entry.title or "",
            CONF_API_KEY: self._entry.options.get(
                CONF_API_KEY, self._entry.data.get(CONF_API_KEY, "")
            ),
            CONF_FX_MODE: self._entry.options.get(CONF_FX_MODE, DEFAULT_FX_MODE),
            CONF_FIXED_FX: _num(self._entry.options.get(CONF_FIXED_FX), DEFAULT_FIXED_FX),
            CONF_VAT_PCT: _num(self._entry.options.get(CONF_VAT_PCT), DEFAULT_VAT_PCT),
            CONF_GRID_FEE: _num(self._entry.options.get(CONF_GRID_FEE), DEFAULT_GRID_FEE),
            CONF_WINDOW_HOURS: _num(self._entry.options.get(CONF_WINDOW_HOURS), DEFAULT_WINDOW_HOURS),
            CONF_UPDATE_INTERVAL: _num(self._entry.options.get(CONF_UPDATE_INTERVAL), DEFAULT_UPDATE_INTERVAL),
            CONF_FORECAST_DAYS: _clamp_forecast_days(self._entry.options.get(CONF_FORECAST_DAYS)),
        }


