"""Pure parsing/conversion logic for the Spot Price integration.

This module intentionally has NO Home Assistant imports so it can be unit
tested and dry-run outside of HA (see `tests/test_helper.py` and
`dryrun.py`).
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

UTC = timezone.utc


@dataclass(frozen=True)
class PricePoint:
    """One hourly price. `start` is a timezone-aware UTC datetime, on the hour."""

    start: datetime
    price_eur_mwh: float
    source: str = "forecast"


def _point_from_row(row: Any, source: str) -> PricePoint:
    """Build a PricePoint from an API row [epoch_ms, price, ...]."""
    ts_ms, price = row[0], row[1]
    return PricePoint(
        start=datetime.fromtimestamp(float(ts_ms) / 1000.0, tz=UTC),
        price_eur_mwh=float(price),
        source=source,
    )


def parse_markets_payload(payload: dict) -> list[PricePoint]:
    """Merge `actual` and `forecast` series into one sorted hourly list.

    The API always returns both series in `payload["series"]`. The `forecast`
    series is preferred wherever the two overlap; `actual` fills the gaps
    (e.g. the day-ahead hours of the current day that the extended forecast
    series does not cover yet).
    """
    by_key: dict[str, list] = {
        s.get("key"): s.get("points", []) for s in payload.get("series", [])
    }
    merged: dict[int, PricePoint] = {}
    # `actual` first, `forecast` second so forecast wins on identical timestamps.
    for key in ("actual", "forecast"):
        for row in by_key.get(key, []):
            if (
                isinstance(row, (list, tuple))
                and len(row) >= 2
                and isinstance(row[1], (int, float))
            ):
                merged[int(row[0])] = _point_from_row(row, key)
    return sorted(merged.values(), key=lambda p: p.start)


def parse_v1_forecast_payload(payload: dict) -> list[PricePoint]:
    """Parse the official /v1/forecasts/<AREA>/latest JSON into PricePoints.

    Unlike the public markets endpoint this has no `actual`/`forecast` split:
    `series[]` is one flat list of objects like
    ``{"ts_utc": "2026-09-19T22:00:00Z", "ts_local": "...", "price_eur_mwh": -5.28}``.
    Prices are EUR/MWh and can be negative. `ts_utc` is authoritative; the `Z`
    suffix is normalized so ``fromisoformat`` also works on Python < 3.11.
    Every point is tagged ``source="forecast"`` (the endpoint's own semantics).
    """
    points: list[PricePoint] = []
    for row in payload.get("series", []):
        if not isinstance(row, dict):
            continue
        ts_utc = row.get("ts_utc")
        price = row.get("price_eur_mwh")
        if not isinstance(ts_utc, str) or not isinstance(price, (int, float)):
            continue
        try:
            start = datetime.fromisoformat(ts_utc.replace("Z", "+00:00"))
        except ValueError:
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        start = start.astimezone(UTC)  # always store UTC (mixing offsets breaks sorting)
        points.append(
            PricePoint(
                start=start, price_eur_mwh=float(price), source="forecast"
            )
        )
    return sorted(points, key=lambda p: p.start)


def floor_hour(dt: datetime) -> datetime:
    """Round a datetime down to the start of its hour."""
    return dt.replace(minute=0, second=0, microsecond=0)


def filter_starting_at(points: list[PricePoint], start_dt: datetime) -> list[PricePoint]:
    """Return points at or after `start_dt` (including the running hour)."""
    cutoff = floor_hour(start_dt)
    return [p for p in points if p.start >= cutoff]


def to_price_per_kwh(
    price_eur_mwh: float,
    fx_eur_to_currency: float,
    vat_pct: float,
    grid_fee_per_kwh: float,
) -> float:
    """Convert EUR/MWh to an end-user price in the chosen currency.

    (EUR/MWh * fx / 1000) * (1 + VAT/100) + grid fee, where `fx` is the
    EUR->currency rate.
    """
    raw_per_kwh = (price_eur_mwh * fx_eur_to_currency) / 1000.0
    return round(raw_per_kwh * (1.0 + vat_pct / 100.0) + grid_fee_per_kwh, 4)


def local_date_of(dt: datetime, tz) -> date:
    """Local calendar date of a datetime in the given timezone."""
    return dt.astimezone(tz).date()


def group_by_local_date(
    points: list[PricePoint], tz
) -> "OrderedDict[date, list[PricePoint]]":
    """Group hourly points by local calendar date in `tz`."""
    grouped: OrderedDict[date, list[PricePoint]] = OrderedDict()
    for point in points:
        grouped.setdefault(local_date_of(point.start, tz), []).append(point)
    return grouped


def summarize(points: list[PricePoint]) -> Optional[dict]:
    """min/max/avg over the raw EUR/MWh values of a day's points."""
    if not points:
        return None
    values = [p.price_eur_mwh for p in points]
    low = min(points, key=lambda p: p.price_eur_mwh)
    high = max(points, key=lambda p: p.price_eur_mwh)
    return {
        "min": low.price_eur_mwh,
        "max": high.price_eur_mwh,
        "avg": sum(values) / len(values),
        "min_at": low.start,
        "max_at": high.start,
    }


def cheapest_window(points: list[PricePoint], n_hours: int) -> Optional[dict]:
    """Find the n consecutive hours with the lowest average EUR/MWh price."""
    if not points or n_hours is None or n_hours <= 0:
        return None
    if len(points) <= n_hours:
        window = points
    else:
        best_start = min(
            range(len(points) - n_hours + 1),
            key=lambda i: sum(p.price_eur_mwh for p in points[i : i + n_hours])
            / n_hours,
        )
        window = points[best_start : best_start + n_hours]
    return {
        "start": window[0].start,
        "end": window[-1].start + timedelta(hours=1),
        "avg_eur_mwh": round(sum(p.price_eur_mwh for p in window) / len(window), 4),
        "points": list(window),
    }


def lookup_current(points: list[PricePoint], now: datetime) -> Optional[PricePoint]:
    """Return the price point covering `now` (may be None, e.g. at DST jumps)."""
    for point in points:
        if point.start <= now < point.start + timedelta(hours=1):
            return point
    return None


def next_low(
    points: list[PricePoint],
    threshold_per_kwh: Optional[float],
    fx_eur_to_currency: float,
    vat_pct: float,
    grid_fee_per_kwh: float,
) -> Optional[PricePoint]:
    """First upcoming hour whose end-user price (chosen currency) is <= threshold."""
    if threshold_per_kwh is None or not points:
        return None
    for point in points:
        if (
            to_price_per_kwh(
                point.price_eur_mwh, fx_eur_to_currency, vat_pct, grid_fee_per_kwh
            )
            <= threshold_per_kwh
        ):
            return point
    return None


def format_request_window(
    now: datetime, back_hours: int, forward_days: int
) -> tuple[str, str]:
    """Hour-aligned ISO `from`/`to` parameters accepted by the API.

    The API rejects (HTTP 422) any timestamp that is not aligned to the hour,
    so the window must be rounded down to the hour before building the URL.
    """
    from_dt = floor_hour(now - timedelta(hours=back_hours))
    to_dt = floor_hour(now + timedelta(days=forward_days))
    return (
        from_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        to_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    )


def clamp_forecast_days(days: Any, lo: int, hi: int) -> int:
    """Clamp the requested forecast period (in days) into [lo, hi] inclusive."""
    try:
        value = int(round(float(days)))
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, value))


def first_n_days(
    points: list[PricePoint], start_dt: datetime, n_days: int
) -> list[PricePoint]:
    """Keep only points from the hour holding `start_dt` through `n_days` ahead.

    Mirrors the hour-aligned request window: `start` is floored to the hour,
    `end` is the hour exactly `n_days` later, and the range is half-open
    [start, end).
    """
    start = floor_hour(start_dt)
    end = floor_hour(start_dt + timedelta(days=max(0, n_days)))
    return [p for p in points if start <= p.start < end]


def clamp_window(
    points: list[PricePoint], now: datetime, back_hours: int, forward_days: int
) -> list[PricePoint]:
    """Keep points inside ``[floor_hour(now-back_hours), floor_hour(now+forward_days))``.

    The official API returns one rolling payload (roughly 36 h back to ~14 days
    forward) with no `from`/`to` parameters; this trims it to the same window
    the public markets API enforces server-side so day/window math stays
    identical between the two backends.
    """
    start = floor_hour(now - timedelta(hours=back_hours))
    end = floor_hour(now + timedelta(days=max(0, forward_days)))
    return [p for p in points if start <= p.start < end]


def compact_hours(hours: list[dict]) -> list[dict[str, Any]]:
    """Shrink the forecast's hourly list to ``[{s, p}, ...]``.

    `s` is the unix epoch (seconds, UTC) of the hour and `p` the end-user
    price in the configured currency — the same instant/value the extended
    shape carried, in a fraction of the bytes. Home Assistant's recorder
    refuses to persist state attributes above 16,384 bytes (serialized JSON);
    the extended per-hour shape (`start`, `eur_mwh`, `sek_kwh`, `source`)
    measures ~33 kB for a 14-day forecast while this one stays at ~10 kB even
    with the per-day `days` summary appended. Malformed rows are skipped.
    """
    out: list[dict[str, Any]] = []
    for hour in hours:
        try:
            out.append({"s": int(hour["start"].timestamp()), "p": hour["sek_kwh"]})
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return out


def load_simple_env(path: str) -> dict:
    """Minimal KEY=VALUE .env parser (stdlib only).

    Supports blank lines, ``#`` comments and double/single-quoted values. Used
    by the standalone dry-run tool so the eupowerprices API key can live in a
    gitignored ``.env`` file instead of shell history/commits. Missing or
    unreadable files yield an empty dict.
    """
    values: dict = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        values[key] = value.strip().strip("\"'")
    return values