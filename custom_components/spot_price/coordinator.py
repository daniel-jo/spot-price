"""DataUpdateCoordinator: fetch Spot Price + FX rate, compute the sensor view."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import timedelta
from typing import Any, Callable, Optional

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.event import async_track_point_in_utc_time
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
    MARKET_TIMEZONE,
    MAX_FORECAST_DAYS,
    MIN_FORECAST_DAYS,
    REQUEST_HEADERS,
    SCHEDULE_ANCHOR_HOUR,
    SCHEDULE_ANCHOR_MINUTE,
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
        self._unsub_timer: Optional[Callable] = None
        self._initialized = False
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=None,  # schedule is 13:30-anchored (see start_schedule)
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

    def _load_cached_state(self, api_mode: str) -> Optional[dict]:
        """Return the cached ``{payload, fetched_at_utc, fx_rate}`` dict or None.

        Never parse a payload fetched through the other backend: the two APIs
        serialize their series differently.
        """
        try:
            with open(self._cache_file, "r", encoding="utf-8") as handle:
                cached = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        if cached.get("api_mode") != api_mode or cached.get("payload") is None:
            return None
        return {
            "payload": cached["payload"],
            "fetched_at_utc": cached.get("fetched_at_utc"),
            "fx_rate": cached.get("fx_rate"),
        }

    def _load_cached_payload(self, api_mode: str) -> Optional[dict]:
        state = self._load_cached_state(api_mode)
        return state["payload"] if state is not None else None

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

    # -- scheduling -----------------------------------------------------------

    def start_schedule(self) -> None:
        """Start the anchor-driven poll schedule after setup.

        The first poll waits for the next 13:30 *market* time — the day-ahead
        prices are published around 13:00 CET/CEST — then the configured
        interval is chained, snapping back to the daily anchor via
        ``helper.next_fetch_time``. Timezone-neutral: the anchor is resolved
        against MARKET_TIMEZONE (Europe/Stockholm, DST-aware), never the HA
        instance's own timezone.
        """
        self._async_schedule_next(dt_util.utcnow(), anchor_only=True)

    def cancel_schedule(self) -> None:
        """Stop the poll timer (e.g. on entry unload)."""
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None

    def _async_schedule_next(self, now: datetime, *, anchor_only: bool = False) -> None:
        """Register the one-shot timer for the next poll instant."""
        self.cancel_schedule()
        interval = int(self.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL))
        if anchor_only:
            when = helper.next_anchor_time(
                now,
                anchor_hour=SCHEDULE_ANCHOR_HOUR,
                anchor_minute=SCHEDULE_ANCHOR_MINUTE,
                tz_name=MARKET_TIMEZONE,
            )
        else:
            when = helper.next_fetch_time(
                now,
                interval,
                anchor_hour=SCHEDULE_ANCHOR_HOUR,
                anchor_minute=SCHEDULE_ANCHOR_MINUTE,
                tz_name=MARKET_TIMEZONE,
            )
        self._unsub_timer = async_track_point_in_utc_time(
            self.hass, self._async_timer_fired, when
        )
        _LOGGER.debug("Next Spot Price refresh scheduled for %s", when.isoformat())

    def _async_timer_fired(self, now: datetime) -> None:
        """Timer fired: reschedule first so a failed fetch never halts the cadence."""
        self._async_schedule_next(now)
        self.hass.async_create_task(self.async_request_refresh())

    # -- update ---------------------------------------------------------------

    def _parse_points(
        self, payload: dict, api_mode: str, now: datetime, forecast_days: int
    ) -> list[helper.PricePoint]:
        """Parse a payload into the same clamped window both backends share.

        The official API returns one rolling payload (roughly 48 h back to ~14
        days forward) with no `from`/`to` parameters, so it is trimmed to the
        exact same window the public markets API enforces server-side.
        """
        if api_mode == "v1":
            points = helper.parse_v1_forecast_payload(payload)
            return helper.clamp_window(points, now, FETCH_BACK_HOURS, forecast_days)
        return helper.parse_markets_payload(payload)

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

        # First poll after startup: when a useable cache already exists, serve
        # it immediately instead of hitting the network — the first *network*
        # fetch is deliberately anchored to the next 13:30 market time.
        if not self._initialized:
            cached = await self.hass.async_add_executor_job(
                self._load_cached_state, api_mode
            )
            if cached is not None:
                self._initialized = True
                try:
                    points = self._parse_points(
                        cached["payload"], api_mode, now, forecast_days
                    )
                except (TypeError, ValueError, KeyError):
                    points = []
                if points:
                    fx_rate = cached.get("fx_rate")
                    if not isinstance(fx_rate, (int, float)):
                        fx_rate = float(DEFAULT_FIXED_FX)
                    self._fx_source = "cached"
                    self._last_error = ""
                    try:
                        fetched_at = (
                            dt_util.parse_datetime(cached.get("fetched_at_utc"))
                            or now
                        )
                    except (TypeError, ValueError):
                        fetched_at = now
                    view = self._build_view(points, now, tz, fx_rate, opts)
                    view["fetched_at"] = fetched_at
                    return view

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
            points = self._parse_points(payload, api_mode, now, forecast_days)
        except (TypeError, ValueError, KeyError) as err:
            raise UpdateFailed(f"Could not parse API response: {err}") from err

        view = self._build_view(points, now, tz, fx_rate, opts)
        self._initialized = True
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
            "grid_fee_kwh": grid_fee,
            "window_hours": window_hours,
            "threshold_kwh": threshold,
            "hours_total": len(points),
            "hours_remaining": len(future),
            "current": None,
            "today": None,
            "tomorrow": None,
            "today_window": None,
            "tomorrow_window": None,
            "next_low": None,
            "forecast": None,
            "history": None,
        }

        current = helper.lookup_current(points, now)
        if current is not None:
            view["current"] = {
                "start": current.start,
                "eur_mwh": current.price_eur_mwh,
                "price_kwh": price(current.price_eur_mwh),
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
                "min_kwh": price(stats["min"]),
                "max_kwh": price(stats["max"]),
                "avg_kwh": price(stats["avg"]),
                "min_at": stats["min_at"],
                "max_at": stats["max_at"],
                "hours_count": len(day_points),
                "hours": [
                    {
                        "start": p.start,
                        "eur_mwh": p.price_eur_mwh,
                        "price_kwh": price(p.price_eur_mwh),
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
                    "avg_kwh": price(win["avg_eur_mwh"]),
                    "hours": [
                        {
                            "start": p.start,
                            "eur_mwh": p.price_eur_mwh,
                            "price_kwh": price(p.price_eur_mwh),
                        }
                        for p in win["points"]
                    ],
                }

        low = helper.next_low(future, threshold, fx_rate, vat_pct, grid_fee)
        if low is not None:
            view["next_low"] = {
                "start": low.start,
                "eur_mwh": low.price_eur_mwh,
                "price_kwh": price(low.price_eur_mwh),
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
                        "min_kwh": price(stats["min"]),
                        "max_kwh": price(stats["max"]),
                        "avg_kwh": price(stats["avg"]),
                        "min_at": stats["min_at"],
                        "max_at": stats["max_at"],
                        "hours_count": len(day_points),
                    }
                )
            view["forecast"] = {
                "horizon_days": forecast_days,
                "hours_count": len(forecast_points),
                "days_count": len(days_out),
                "avg_kwh": price(avg_eur),
                "days": days_out,
                "hours": [
                    {
                        "start": p.start,
                        "eur_mwh": p.price_eur_mwh,
                        "price_kwh": price(p.price_eur_mwh),
                        "source": p.source,
                    }
                    for p in forecast_points
                ],
            }
        else:
            view["forecast"] = {"available": False}

        # Always-on 24 h history: the last full hours of the fetched payload
        # (realized `actual` data whenever the API provides it — the fetch
        # window reaches FETCH_BACK_HOURS back). Filled only once enough hours
        # are actually in the payload; the running hour is excluded.
        history_points = helper.history_slice(points, now, hours=24)
        if history_points:
            stats = helper.summarize(history_points)
            view["history"] = {
                "available": True,
                "hours_window": 24,
                "hours_count": len(history_points),
                "min_eur": stats["min"],
                "max_eur": stats["max"],
                "avg_eur": stats["avg"],
                "min_kwh": price(stats["min"]),
                "max_kwh": price(stats["max"]),
                "avg_kwh": price(stats["avg"]),
                "min_at": stats["min_at"],
                "max_at": stats["max_at"],
                "hours": [
                    {
                        "start": p.start,
                        "eur_mwh": p.price_eur_mwh,
                        "price_kwh": price(p.price_eur_mwh),
                        "source": p.source,
                    }
                    for p in history_points
                ],
            }


        view["fx_source"] = self.fx_source
        return view