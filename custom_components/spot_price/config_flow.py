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
    CONF_LOW_THRESHOLD,
    CONF_UPDATE_INTERVAL,
    CONF_VAT_PCT,
    CONF_WINDOW_HOURS,
    DEFAULT_AREA,
    DEFAULT_CURRENCY,
    DEFAULT_FIXED_FX,
    DEFAULT_FORECAST_DAYS,
    DEFAULT_FX_MODE,
    DEFAULT_GRID_FEE,
    DEFAULT_LOW_THRESHOLD,
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
)

# The API's area codes look like SE1–SE4, NO2, DK1, BE, AT, ... (2-6 chars).
AREA_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{1,5}$")


def normalize_area(value: Any) -> str:
    """Trim and uppercase a user-entered area code."""
    return str(value or "").strip().upper()


def _clamp_forecast_days(value: Any, default: int = DEFAULT_FORECAST_DAYS) -> int:
    """Coerce a forecast-period input to an int inside [MIN, MAX] days.

    The eupowerprices forecast never extends beyond ~14 days (see const.py),
    so anything larger is clamped to the ceiling instead of stored.
    """
    try:
        days = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(MIN_FORECAST_DAYS, min(MAX_FORECAST_DAYS, days))


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
            area = normalize_area(user_input.get(CONF_AREA))
            api_key = str(user_input.get(CONF_API_KEY) or "").strip()
            error = await async_area_error(self.hass, area, api_key)
            if error is not None:
                errors[CONF_AREA] = error
            else:
                return self.async_create_entry(
                    title=str(user_input.get(CONF_NAME, "")).strip() or NAME,
                    data={
                        CONF_AREA: area,
                        CONF_CURRENCY: user_input.get(CONF_CURRENCY, DEFAULT_CURRENCY),
                        CONF_API_KEY: api_key,
                        CONF_FORECAST_DAYS: _clamp_forecast_days(
                            user_input.get(CONF_FORECAST_DAYS)
                        ),
                    },
                )

        schema = vol.Schema(
            {
                # No preselection: the user must actively pick/type an area.
                vol.Required(CONF_AREA): selector.selector(
                    {
                        "select": {
                            "options": self._area_options,
                            "mode": "dropdown",
                            "custom_value": True,
                        }
                    }
                ),
                vol.Required(
                    CONF_CURRENCY, default=DEFAULT_CURRENCY
                ): selector.selector(
                    {
                        "select": {
                            "options": SUPPORTED_CURRENCIES,
                            "mode": "dropdown",
                        }
                    }
                ),
                vol.Optional(CONF_NAME, default=""): selector.selector(
                    {"text": {"type": "text"}}
                ),
                vol.Optional(CONF_API_KEY, default=""): selector.selector(
                    {"text": {"type": "password", "autocomplete": "off"}}
                ),
                vol.Optional(
                    CONF_FORECAST_DAYS, default=DEFAULT_FORECAST_DAYS
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
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors, last_step=True
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "EupowerpricesOptionsFlow":
        return EupowerpricesOptionsFlow(config_entry)


class EupowerpricesOptionsFlow(config_entries.OptionsFlow):
    """Options: area, FX mode, VAT, grid fee, cheapest-window size, threshold."""

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
        if user_input is not None:
            area = normalize_area(user_input.get(CONF_AREA))
            api_key = str(user_input.get(CONF_API_KEY) or "").strip()
            error = await async_area_error(self.hass, area, api_key)
            if error is not None:
                current = self._current_values()
                current[CONF_AREA] = area
                return self._show_form(current, {CONF_AREA: error})
            user_input[CONF_AREA] = area
            user_input[CONF_API_KEY] = api_key
            threshold = user_input.get(CONF_LOW_THRESHOLD)
            user_input[CONF_LOW_THRESHOLD] = (
                None if threshold in (None, "") else float(threshold)
            )
            user_input[CONF_FORECAST_DAYS] = _clamp_forecast_days(
                user_input.get(CONF_FORECAST_DAYS)
            )
            return self.async_create_entry(title="", data=user_input)
        return self._show_form(self._current_values(), {})

    def _current_values(self) -> dict[str, Any]:
        return {
            CONF_AREA: self._entry.options.get(
                CONF_AREA, self._entry.data.get(CONF_AREA, DEFAULT_AREA)
            ),
            CONF_CURRENCY: self._entry.options.get(
                CONF_CURRENCY,
                self._entry.data.get(CONF_CURRENCY, DEFAULT_CURRENCY),
            ),
            CONF_API_KEY: self._entry.options.get(
                CONF_API_KEY, self._entry.data.get(CONF_API_KEY, "")
            ),
            CONF_FX_MODE: self._entry.options.get(CONF_FX_MODE, DEFAULT_FX_MODE),
            CONF_FIXED_FX: self._entry.options.get(CONF_FIXED_FX, DEFAULT_FIXED_FX),
            CONF_VAT_PCT: self._entry.options.get(CONF_VAT_PCT, DEFAULT_VAT_PCT),
            CONF_GRID_FEE: self._entry.options.get(CONF_GRID_FEE, DEFAULT_GRID_FEE),
            CONF_WINDOW_HOURS: self._entry.options.get(
                CONF_WINDOW_HOURS, DEFAULT_WINDOW_HOURS
            ),
            CONF_LOW_THRESHOLD: self._entry.options.get(
                CONF_LOW_THRESHOLD, DEFAULT_LOW_THRESHOLD
            ),
            CONF_UPDATE_INTERVAL: self._entry.options.get(
                CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
            ),
            CONF_FORECAST_DAYS: _clamp_forecast_days(
                self._entry.options.get(CONF_FORECAST_DAYS)
            ),
        }

    def _show_form(self, current: dict[str, Any], errors: dict[str, str]) -> FlowResult:
        schema = vol.Schema(
            {
                vol.Required(CONF_AREA, default=current[CONF_AREA]): selector.selector(
                    {
                        "select": {
                            "options": self._area_options,
                            "mode": "dropdown",
                            "custom_value": True,
                        }
                    }
                ),
                vol.Required(
                    CONF_CURRENCY, default=current[CONF_CURRENCY]
                ): selector.selector(
                    {
                        "select": {
                            "options": SUPPORTED_CURRENCIES,
                            "mode": "dropdown",
                        }
                    }
                ),
                vol.Optional(CONF_API_KEY, default=current[CONF_API_KEY]): selector.selector(
                    {"text": {"type": "password", "autocomplete": "off"}}
                ),
                vol.Required(
                    CONF_FX_MODE, default=current[CONF_FX_MODE]
                ): selector.selector(
                    {
                        "select": {
                            "options": [FX_MODE_LIVE, FX_MODE_FIXED],
                            "mode": "dropdown",
                        }
                    }
                ),
                vol.Optional(
                    CONF_FIXED_FX, default=current[CONF_FIXED_FX]
                ): selector.selector(
                    {"number": {"mode": "box", "min": 0, "max": 30, "step": 0.01}}
                ),
                vol.Optional(
                    CONF_VAT_PCT, default=current[CONF_VAT_PCT]
                ): selector.selector(
                    {"number": {"mode": "box", "min": 0, "max": 100, "step": 0.1}}
                ),
                vol.Optional(
                    CONF_GRID_FEE, default=current[CONF_GRID_FEE]
                ): selector.selector(
                    {"number": {"mode": "box", "min": 0, "max": 5, "step": 0.01}}
                ),
                vol.Optional(
                    CONF_WINDOW_HOURS, default=current[CONF_WINDOW_HOURS]
                ): selector.selector(
                    {"number": {"mode": "box", "min": 1, "max": 12, "step": 1}}
                ),
                vol.Optional(
                    CONF_FORECAST_DAYS, default=current[CONF_FORECAST_DAYS]
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
                vol.Optional(
                    CONF_LOW_THRESHOLD, default=current[CONF_LOW_THRESHOLD]
                ): selector.selector(
                    {"number": {"mode": "box", "min": 0, "max": 20}}
                ),
                vol.Optional(
                    CONF_UPDATE_INTERVAL, default=current[CONF_UPDATE_INTERVAL]
                ): selector.selector(
                    {"number": {"mode": "box", "min": 30, "max": 1440, "step": 30}}
                ),
            }
        )
        return self.async_show_form(
            step_id="init", data_schema=schema, errors=errors, last_step=True
        )
