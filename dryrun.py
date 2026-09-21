#!/usr/bin/env python3
"""Standalone dry-run against the live Spot Price API.

Validates parsing + EUR->currency conversion + day/window math without Home
Assistant. Requires only the Python standard library.

Usage:
    python3 dryrun.py [--area SE4] [--currency EUR] [--fx 1.0] [--vat 25]
                      [--grid-fee 0.0] [--window 3]

With an eupowerprices API key set via the environment or a gitignored
`home-assistant/spot-price/.env` file (EUPOWERPRICES_API_KEY=...), the dry-run
uses the official /v1/forecasts/<AREA>/latest endpoint. Without a key it falls
back to the public markets endpoint. The key itself is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "custom_components", "spot_price"))

import helper  # noqa: E402

API_URL = "https://api.eupowerprices.com/presentation/v1/markets"
API_V1_FORECAST_URL = "https://api.eupowerprices.com/v1/forecasts"
FX_URL_TEMPLATE = "https://api.frankfurter.app/latest?from=EUR&to={currency}"
ENV_FILE = os.path.join(HERE, ".env")


def _ssl_context() -> ssl.SSLContext:
    """Use certifi's CA bundle when available (fixes macOS system-Python
    'unable to get local issuer certificate' errors with Cloudflare certs)."""
    try:
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()


def load_api_key() -> str:
    """API key from the environment or a gitignored .env next to this script."""
    env = dict(os.environ)
    env.update(helper.load_simple_env(ENV_FILE))
    return (env.get("EUPOWERPRICES_API_KEY") or "").strip()


def fetch_json(url: str, api_key: str = "") -> dict:
    # The whole API sits behind Cloudflare: a bare Python-urllib signature is
    # rejected (HTTP 1010/403) regardless of the X-API-Key header, so send
    # browser-like headers on every request (keyed or not).
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9,sv-SE;q=0.8,sv;q=0.7",
    }
    if api_key:
        headers["X-API-Key"] = api_key
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30, context=_ssl_context()) as response:
            # urllib does not auto-decompress, unlike aiohttp in HA.
            encoding = response.headers.get("Content-Encoding", "").lower()
            raw = response.read()
            if encoding == "gzip":
                import gzip

                raw = gzip.decompress(raw)
            return json.loads(raw)
    except urllib.error.HTTPError as err:
        if err.code in (401, 403):
            hint = (
                f"Set EUPOWERPRICES_API_KEY in {ENV_FILE} or the environment."
                if not api_key
                else "Check that the key in .env/environment is valid and "
                "that your key has access to this area."
            )
            raise SystemExit(
                f"The eupowerprices API rejected the request (HTTP {err.code}). "
                f"{hint}"
            ) from err
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--area", default="SE4")
    parser.add_argument(
        "--currency", default="EUR", help="output currency (default: EUR)"
    )
    parser.add_argument(
        "--fx", type=float, default=None, help="EUR->currency (default: live)"
    )
    parser.add_argument("--vat", type=float, default=25.0, help="VAT percent")
    parser.add_argument("--grid-fee", type=float, default=0.0, dest="grid_fee")
    parser.add_argument("--window", type=int, default=3, dest="window_hours")
    parser.add_argument(
        "--days",
        type=int,
        default=14,
        dest="forecast_days",
        help="forecast period in days (clamped to 1..14)",
    )
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    forecast_days = helper.clamp_forecast_days(args.forecast_days, 1, 14)
    api_key = load_api_key()
    if api_key:
        url = f"{API_V1_FORECAST_URL}/{args.area}/latest"
        print(f"GET {url}  [official v1 API, key from .env/env]\n")
        payload = fetch_json(url, api_key=api_key)
        points = helper.parse_v1_forecast_payload(payload)
        points = helper.clamp_window(points, now, 36, forecast_days)
    else:
        from_iso, to_iso = helper.format_request_window(now, 36, forecast_days)
        url = (
            f"{API_URL}?area={args.area}"
            f"&from={from_iso}&to={to_iso}"
            "&resolution=hourly"
        )
        print(f"GET {url}  [public API, no key]\n")
        payload = fetch_json(url)
        points = helper.parse_markets_payload(payload)
    print(
        f"Parsed {len(points)} hourly points: "
        f"{points[0].start.isoformat()} -> {points[-1].start.isoformat()} (UTC)"
    )

    currency = args.currency.upper()
    fx, fx_source = args.fx, "fixed (argument)"
    if fx is None:
        if currency == "EUR":
            fx, fx_source = 1.0, "base (EUR)"
        else:
            fx_data = fetch_json(FX_URL_TEMPLATE.format(currency=currency))
            fx, fx_source = (
                float(fx_data["rates"][currency]),
                "live (Frankfurter/ECB)",
            )
    print(f"EUR->{currency} {fx:.4f}  [{fx_source}]")
    print(f"VAT {args.vat}%  grid fee {args.grid_fee} {currency}/kWh\n")

    tz = ZoneInfo("Europe/Stockholm")
    grouped = helper.group_by_local_date(points, tz)
    today = helper.local_date_of(now, tz)
    tomorrow = today + timedelta(days=1)
    future_by_day = helper.group_by_local_date(helper.filter_starting_at(points, now), tz)

    def price(eur_per_mwh: float) -> float:
        return helper.to_price_per_kwh(eur_per_mwh, fx, args.vat, args.grid_fee)

    forecast_points = helper.first_n_days(
        helper.filter_starting_at(points, now), now, forecast_days
    )
    if forecast_points:
        avg_eur = sum(p.price_eur_mwh for p in forecast_points) / len(forecast_points)
        print(
            f"FORECAST             {len(forecast_points):3d}h "
            f"{forecast_points[0].start.astimezone(tz).isoformat()} -> "
            f"{forecast_points[-1].start.astimezone(tz).isoformat()}  "
            f"avg={price(avg_eur):.3f} {currency}/kWh"
        )

    for label, day in (("TODAY", today), ("TOMORROW", tomorrow)):
        day_points = grouped.get(day, [])
        if not day_points:
            print(f"{label}: no data")
            continue
        summary = helper.summarize(day_points)
        print(
            f"{label} {day}  ({len(day_points)}h)  "
            f"min={price(summary['min']):.3f} {currency}/kWh "
            f"({summary['min_at'].astimezone(tz):%H:%M})  "
            f"max={price(summary['max']):.3f} "
            f"({summary['max_at'].astimezone(tz):%H:%M})  "
            f"avg={price(summary['avg']):.3f}"
        )

    current = helper.lookup_current(points, now)
    if current is not None:
        print(
            f"CURRENT                {price(current.price_eur_mwh):.3f} {currency}/kWh "
            f"({current.price_eur_mwh} EUR/MWh)"
        )

    for label, day in (("TODAY", today), ("TOMORROW", tomorrow)):
        window = helper.cheapest_window(future_by_day.get(day, []), args.window_hours)
        if window is not None:
            print(
                f"CHEAPEST {args.window_hours}h {label.lower():9s}"
                f"{window['start'].astimezone(tz).isoformat()} -> "
                f"{window['end'].astimezone(tz).isoformat()}  "
                f"avg={price(window['avg_eur_mwh']):.3f} {currency}/kWh"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())