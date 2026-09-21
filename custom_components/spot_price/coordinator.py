"""DataUpdateCoordinator: fetch Spot Price + FX rate, compute the sensor view."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import timedelta
from typing import Any, Optional

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import helper
from .const import (
    API_URL,
    API_V1_FORECAST_URL,
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
    FETCH_BACK_HOURS,
    FRANKFURTER_BASE,
    FX_MODE_FIXED,
    FX_MODE_LIVE,
    FX_URL,
    MAX_FORECAST_DAYS,
    MIN_FORECAST_DAYS,
    REQUEST_HEADERS,
)

_LOGGER = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 30


def resolve_options(entry: ConfigEntry) -> dict:
    """Resolve effective options from the config entry (data takes area)."""
    options = {
        CONF_AREA: DEFAULT_AREA,
        CONF_CURRENCY: DEFAULT_CURRENCY,
        CONF_FX_MODE: DEFAULT_FX_MODE,
        CONF_FIXED_FX: DEFAULT_FIXED_FX,
        CONF_VAT_PCT: DEFAULT_VAT_PCT,
        CONF_GRID_FEE: DEFAULT_GRID_FEE,
        CONF_WINDOW_HOURS: DEFAULT_WINDOW_HOURS,
        CONF_LOW_THRESHOLD: DEFAULT_LOW_THRESHOLD,
        CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL,
        CONF_FORECAST_DAYS: DEFAULT_FORECAST_DAYS,
    }
    options.update(entry.options)
    options[CONF_AREA] = entry.data.get(CONF_AREA, options.get(CONF_AREA, DEFAULT_AREA))
    options[CONF_CURRENCY] = entry.data.get(
        CONF_CURRENCY, options.get(CONF_CURRENCY, DEFAULT_CURRENCY)
    )
    options[CONF_API_KEY] = str(
        options.get(CONF_API_KEY) or entry.data.get(CONF_API_KEY) or ""
    ).strip()
    if CONF_FORECAST_DAYS not in options:
        options[CONF_FORECAST_DAYS] = entry.data.get(
            CONF_FORECAST_DAYS, DEFAULT_FORECAST_DAYS
        )
    return options


class EupowerpricesCoordinator(DataUpdateCoordinator):
    """Fetch and compute hourly electricity prices for one area."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._cache_file = hass.config.path(DOMAIN, "cache.json")
        self._last_error = ""
        self._fx_source = ""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(
                minutes=resolve_options(entry).get(
                    CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
                )
            ),
        )

    @property
    def options(self) -> dict:
        return resolve_options(self._entry)

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def fx_source(self) -> str:
        return self._fx_source

    # -- network -----------------------------------------------------------

    async def _async_fetch_json(self, url: str, **kwargs: Any) -> Any:
        session = aiohttp_client.async_get_clientsession(self.hass)
        try:
            async with asyncio.timeout(_TIMEOUT_SECONDS):
                async with session.get(url, **kwargs) as response:
                    response.raise_for_status()
                    return await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(f"{url} -> {err}") from err

    async def _async_fetch_fx(self) -> tuple[float, str]:
        """Return (EUR->currency rate, source label), with a fixed-rate fallback."""
        opts = self.options
        currency = str(
            opts.get(CONF_CURRENCY, DEFAULT_CURRENCY) or DEFAULT_CURRENCY
        ).upper()
        fixed = float(opts.get(CONF_FIXED_FX, DEFAULT_FIXED_FX))
        if opts.get(CONF_FX_MODE, DEFAULT_FX_MODE) != FX_MODE_LIVE:
            return fixed, "fixed"
        if currency == "EUR":
            return 1.0, "base (EUR)"
        try:
            payload = await self._async_fetch_json(
                FX_URL,
                params={"from": FRANKFURTER_BASE, "to": currency},
                headers=dict(REQUEST_HEADERS),
            )
            rate = float(payload["rates"][currency])
            return rate, "live (Frankfurter/ECB)"
        except (UpdateFailed, KeyError, TypeError, ValueError) as err:
            _LOGGER.warning(
                "Live EUR->%s fetch failed (%s); using fixed rate %.4f",
                currency,
                err,
                fixed,
            )
            self._last_error = f"FX live fetch failed: {err}"
            return fixed, "fallback (fixed)"

    # -- cache ---------------------------------------------------------------

    def _load_cached_payload(self, api_mode: str) -> Optional[dict]:
        try:
            with open(self._cache_file, "r", encoding="utf-8") as handle:
                cached = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        # Never parse a payload fetched through the other backend: the two
        # APIs serialize their series differently.
        if cached.get("api_mode") != api_mode:
            return None
        return cached.get("payload")

    def _save_cached_payload(self, payload: dict, fx_rate: float, api_mode: str) -> None:
        def _write() -> None:
            try:
                # /config/spot_price/ may not exist on a fresh install — create
                # it before the first write or the cache can never persist.
                os.makedirs(os.path.dirname(self._cache_file), exist_ok=True)
                with open(self._cache_file, "w", encoding="utf-8") as handle:
                    json.dump(
                        {
                            "fetched_at_utc": dt_util.utcnow().isoformat(),
                            "fx_rate": fx_rate,
                            "api_mode": api_mode,
                            "payload": payload,
                        },
                        handle,
                    )
            except OSError as err:
                _LOGGER.warning("Could not persist price cache: %s", err)

        self.hass.async_add_executor_job(_write)

    # -- update ---------------------------------------------------------------

    async def _async_update_data(self) -> dict:
        opts = self.options
        now = dt_util.utcnow()
        tz = dt_util.get_time_zone(self.hass.config.time_zone) or dt_util.UTC

        forecast_days = helper.clamp_forecast_days(
            opts.get(CONF_FORECAST_DAYS, DEFAULT_FORECAST_DAYS),
            MIN_FORECAST_DAYS,
            MAX_FORECAST_DAYS,
        )
        api_key = (opts.get(CONF_API_KEY) or "").strip()
        area = opts[CONF_AREA]
        if api_key:
            # Official keyed API: one rolling payload, no window parameters.
            # The API is still fronted by Cloudflare, so the browser-like
            # REQUEST_HEADERS must accompany the X-API-Key header (a bare
            # script signature gets HTTP 1010/403 even with a valid key).
            api_mode = "v1"
            url = f"{API_V1_FORECAST_URL}/{area}/latest"
            kwargs: dict[str, Any] = {
                "headers": {**dict(REQUEST_HEADERS), "X-API-Key": api_key}
            }
        else:
            # Public markets API: window must be hour-aligned or it 422s.
            api_mode = "presentation"
            from_iso, to_iso = helper.format_request_window(
                now, FETCH_BACK_HOURS, forecast_days
            )
            url = API_URL
            kwargs = {
                "params": {
                    "area": area,
                    "from": from_iso,
                    "to": to_iso,
                    "resolution": "hourly",
                },
                "headers": dict(REQUEST_HEADERS),
            }

        payload = None
        try:
            payload = await self._async_fetch_json(url, **kwargs)
            self._last_error = ""
        except UpdateFailed as err:
            _LOGGER.warning("Spot Price fetch failed: %s", err)
            self._last_error = str(err)
            payload = await self.hass.async_add_executor_job(
                self._load_cached_payload, api_mode
            )
            if payload is None:
                raise  # nothing to work from -> HA retries setup on its own

        fx_rate, self._fx_source = await self._async_fetch_fx()

        try:
            if api_mode == "v1":
                points = helper.parse_v1_forecast_payload(payload)
                points = helper.clamp_window(
                    points, now, FETCH_BACK_HOURS, forecast_days
                )
            else:
                points = helper.parse_markets_payload(payload)
        except (TypeError, ValueError, KeyError) as err:
            raise UpdateFailed(f"Could not parse API response: {err}") from err

        view = self._build_view(points, now, tz, fx_rate, opts)
        self._save_cached_payload(payload, fx_rate, api_mode)
        return view

    # -- view construction -----------------------------------------------------

    def _build_view(
        self,
        points: list[helper.PricePoint],
        now: datetime,
        tz,
        fx_rate: float,
        opts: dict,
    ) -> dict:
        vat_pct = float(opts.get(CONF_VAT_PCT, DEFAULT_VAT_PCT))
        grid_fee = float(opts.get(CONF_GRID_FEE, DEFAULT_GRID_FEE))
        window_hours = int(opts.get(CONF_WINDOW_HOURS, DEFAULT_WINDOW_HOURS))
        threshold = opts.get(CONF_LOW_THRESHOLD, DEFAULT_LOW_THRESHOLD)
        threshold = None if threshold in (None, "") else float(threshold)

        def price(eur_per_mwh: float) -> float:
            return helper.to_price_per_kwh(eur_per_mwh, fx_rate, vat_pct, grid_fee)

        today = helper.local_date_of(now, tz)
        tomorrow = today + timedelta(days=1)
        grouped_all = helper.group_by_local_date(points, tz)
        future = helper.filter_starting_at(points, now)
        future_by_day = helper.group_by_local_date(future, tz)
        forecast_days = helper.clamp_forecast_days(
            opts.get(CONF_FORECAST_DAYS, DEFAULT_FORECAST_DAYS),
            MIN_FORECAST_DAYS,
            MAX_FORECAST_DAYS,
        )
        forecast_points = helper.first_n_days(future, now, forecast_days)

        view: dict[str, Any] = {
            "area": opts[CONF_AREA],
            "currency": opts.get(CONF_CURRENCY, DEFAULT_CURRENCY),
            "fetched_at": now,
            "fx_rate": fx_rate,
            "fx_source": "",
            "vat_pct": vat_pct,
            "grid_fee_sek_kwh": grid_fee,
            "window_hours": window_hours,
            "threshold_sek": threshold,
            "hours_total": len(points),
            "hours_remaining": len(future),
            "current": None,
            "today": None,
            "tomorrow": None,
            "today_window": None,
            "tomorrow_window": None,
            "next_low": None,
            "forecast": None,
        }

        current = helper.lookup_current(points, now)
        if current is not None:
            view["current"] = {
                "start": current.start,
                "eur_mwh": current.price_eur_mwh,
                "sek_kwh": price(current.price_eur_mwh),
                "source": current.source,
            }

        for label, day in (("today", today), ("tomorrow", tomorrow)):
            day_points = grouped_all.get(day, [])
            if not day_points:
                view[label] = {"available": False, "date": day.isoformat()}
                continue
            stats = helper.summarize(day_points)
            view[label] = {
                "available": True,
                "date": day.isoformat(),
                "min_eur": stats["min"],
                "max_eur": stats["max"],
                "avg_eur": stats["avg"],
                "min_sek": price(stats["min"]),
                "max_sek": price(stats["max"]),
                "avg_sek": price(stats["avg"]),
                "min_at": stats["min_at"],
                "max_at": stats["max_at"],
                "hours_count": len(day_points),
                "hours": [
                    {
                        "start": p.start,
                        "eur_mwh": p.price_eur_mwh,
                        "sek_kwh": price(p.price_eur_mwh),
                        "source": p.source,
                    }
                    for p in day_points
                ],
            }

        for label, day in (("today_window", today), ("tomorrow_window", tomorrow)):
            win = helper.cheapest_window(future_by_day.get(day, []), window_hours)
            if win is not None:
                view[label] = {
                    "start": win["start"],
                    "end": win["end"],
                    "avg_eur_mwh": win["avg_eur_mwh"],
                    "avg_sek_kwh": price(win["avg_eur_mwh"]),
                    "hours": [
                        {
                            "start": p.start,
                            "eur_mwh": p.price_eur_mwh,
                            "sek_kwh": price(p.price_eur_mwh),
                        }
                        for p in win["points"]
                    ],
                }

        low = helper.next_low(future, threshold, fx_rate, vat_pct, grid_fee)
        if low is not None:
            view["next_low"] = {
                "start": low.start,
                "eur_mwh": low.price_eur_mwh,
                "sek_kwh": price(low.price_eur_mwh),
            }

        if forecast_points:
            avg_eur = sum(p.price_eur_mwh for p in forecast_points) / len(
                forecast_points
            )
            days_out = []
            for day, day_points in helper.group_by_local_date(
                forecast_points, tz
            ).items():
                stats = helper.summarize(day_points)
                days_out.append(
                    {
                        "date": day.isoformat(),
                        "min_eur": stats["min"],
                        "max_eur": stats["max"],
                        "avg_eur": stats["avg"],
                        "min_sek": price(stats["min"]),
                        "max_sek": price(stats["max"]),
                        "avg_sek": price(stats["avg"]),
                        "min_at": stats["min_at"],
                        "max_at": stats["max_at"],
                        "hours_count": len(day_points),
                    }
                )
            view["forecast"] = {
                "horizon_days": forecast_days,
                "hours_count": len(forecast_points),
                "days_count": len(days_out),
                "avg_sek_kwh": price(avg_eur),
                "days": days_out,
                "hours": [
                    {
                        "start": p.start,
                        "eur_mwh": p.price_eur_mwh,
                        "sek_kwh": price(p.price_eur_mwh),
                        "source": p.source,
                    }
                    for p in forecast_points
                ],
            }
        else:
            view["forecast"] = {"available": False}

        view["fx_source"] = self.fx_source
        return view