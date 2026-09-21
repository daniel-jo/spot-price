"""Unit tests for the pure logic in helper.py.

Run with pytest (preferred) or directly:  python3 tests/test_helper.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "custom_components",
        "spot_price",
    ),
)

import helper  # noqa: E402

UTC = timezone.utc
TZ_SE = ZoneInfo("Europe/Stockholm")

EPSILON = 1e-9


def _pts(epoch_hours: list[tuple[float, float]]) -> list[helper.PricePoint]:
    """Build PricePoints from (offset-in-hours, price) pairs."""
    base = datetime(2026, 9, 17, 0, 0, tzinfo=UTC)
    return [
        helper.PricePoint(base + timedelta(hours=h), price) for h, price in epoch_hours
    ]


# --- parsing ---------------------------------------------------------------

def test_parse_merges_series_forecast_preferred():
    payload = {
        "series": [
            {"key": "actual", "points": [[1_000, 10.0], [2_000, 20.0], [3_000, 30.0]]},
            {"key": "forecast", "points": [[2_000, 25.0], [4_000, 40.0]]},
        ]
    }
    points = helper.parse_markets_payload(payload)
    assert len(points) == 4
    assert points[0].start == datetime.fromtimestamp(1, tz=UTC)
    overlap = [p for p in points if p.start == datetime.fromtimestamp(2, tz=UTC)][0]
    assert overlap.price_eur_mwh == 25.0  # forecast wins
    assert overlap.source == "forecast"


def test_parse_skips_malformed_rows():
    payload = {
        "series": [
            {"key": "actual", "points": [[1_000, 10.0], ["bad", None], [3_000]]}
        ]
    }
    points = helper.parse_markets_payload(payload)
    assert len(points) == 1


def test_parse_v1_forecast_payload():
    payload = {
        "api_schema_version": "v1",
        "area": "SE4",
        "unit": "EUR/MWh",
        "series": [
            {
                "ts_utc": "2026-09-19T22:00:00Z",
                "ts_local": "2026-09-20T00:00:00+02:00",
                "price_eur_mwh": -5.28,
            },
            {
                "ts_utc": "2026-09-19T23:00:00Z",
                "ts_local": "2026-09-20T01:00:00+02:00",
                "price_eur_mwh": -8.06,
            },
            {
                "ts_utc": "2026-09-20T00:00:00Z",
                "ts_local": "2026-09-20T02:00:00+02:00",
                "price_eur_mwh": -7.98,
            },
        ],
    }
    points = helper.parse_v1_forecast_payload(payload)
    assert len(points) == 3
    assert points[0].start == datetime(2026, 9, 19, 22, 0, tzinfo=UTC)
    assert points[2].price_eur_mwh == -7.98  # negative prices are kept
    assert all(p.source == "forecast" for p in points)
    assert points == sorted(points, key=lambda p: p.start)


def test_parse_v1_skips_malformed_and_sorts():
    payload = {
        "series": [
            {"ts_utc": "2026-09-20T02:00:00Z", "price_eur_mwh": 1.0},
            {"ts_utc": "not-a-date", "price_eur_mwh": 2.0},
            {"ts_utc": "2026-09-20T01:00:00Z"},  # missing price
            {"price_eur_mwh": 3.0},  # missing ts
            {"ts_utc": "2026-09-20T00:00:00+02:00", "price_eur_mwh": 4.0},
            "junk",
        ]
    }
    points = helper.parse_v1_forecast_payload(payload)
    assert len(points) == 2
    # sorted ascending; the +02:00 offset normalizes to UTC 2026-09-19T22:00Z
    assert points[0].start == datetime(2026, 9, 19, 22, 0, tzinfo=UTC)
    assert points[1].start == datetime(2026, 9, 20, 2, 0, tzinfo=UTC)


def test_filter_starting_at_includes_running_hour():
    points = _pts([(12, 1.0), (13, 2.0), (14, 3.0)])
    now = datetime(2026, 9, 17, 13, 3, 7, tzinfo=UTC)
    future = helper.filter_starting_at(points, now)
    assert [p.start.hour for p in future] == [13, 14]


# --- conversion -------------------------------------------------------------

def test_price_conversion():
    # (100 EUR/MWh * 11.0 / 1000) * 1.25 + 0.30 = 1.675
    assert abs(helper.to_price_per_kwh(100.0, 11.0, 25.0, 0.30) - 1.675) < EPSILON
    # no VAT, no fee
    assert abs(helper.to_price_per_kwh(1000.0, 10.0, 0.0, 0.0) - 10.0) < EPSILON
    # negative prices stay negative
    assert helper.to_price_per_kwh(-20.0, 11.0, 25.0, 0.0) < 0


# --- local-day grouping -----------------------------------------------------

def test_summer_day_boundary_is_2200z():
    point = helper.PricePoint(datetime(2026, 7, 1, 22, 0, tzinfo=UTC), 10.0)
    assert helper.local_date_of(point.start, TZ_SE) == datetime(2026, 7, 2).date()


def test_winter_day_boundary_is_2300z():
    point = helper.PricePoint(datetime(2026, 2, 15, 23, 0, tzinfo=UTC), 10.0)
    assert helper.local_date_of(point.start, TZ_SE) == datetime(2026, 2, 16).date()


def test_dst_transition_day_has_25_hours():
    # CEST -> CET on 2026-10-25: the local day starts 2026-10-24T22:00Z
    # and runs through 2026-10-25T22:00Z (25 hourly slots).
    start = datetime(2026, 10, 24, 22, 0, tzinfo=UTC)
    points = [
        helper.PricePoint(start + timedelta(hours=i), float(i))
        for i in range(26)
    ]
    grouped = helper.group_by_local_date(points, TZ_SE)
    oct25 = grouped.get(datetime(2026, 10, 25).date(), [])
    assert len(oct25) == 25
    # The last point (2026-10-25T23:00Z) belongs to the 26th.
    assert datetime(2026, 10, 26).date() in grouped


def test_summarize():
    points = _pts([(0, 10.0), (1, 30.0), (2, 20.0)])
    summary = helper.summarize(points)
    assert summary["min"] == 10.0
    assert summary["max"] == 30.0
    assert abs(summary["avg"] - 20.0) < EPSILON
    assert summary["min_at"].hour == 0
    assert summary["max_at"].hour == 1


def test_summarize_empty_is_none():
    assert helper.summarize([]) is None


# --- windows / thresholds ---------------------------------------------------

def test_cheapest_window():
    points = _pts([(0, 10.0), (1, 1.0), (2, 5.0), (3, 1.0), (4, 10.0)])
    win = helper.cheapest_window(points, 2)
    assert win is not None
    assert abs(win["avg_eur_mwh"] - 3.0) < EPSILON
    assert win["start"].hour in (1, 3)


def test_cheapest_window_longer_than_data():
    points = _pts([(0, 10.0), (1, 20.0)])
    win = helper.cheapest_window(points, 5)
    assert win is not None
    assert len(win["points"]) == 2


def test_cheapest_window_edge_cases():
    assert helper.cheapest_window([], 3) is None
    assert helper.cheapest_window(_pts([(0, 1.0)]), 0) is None
    assert helper.cheapest_window(_pts([(0, 1.0)]), -1) is None


def test_lookup_current():
    points = _pts([(13, 5.0), (14, 6.0)])
    now = datetime(2026, 9, 17, 13, 30, tzinfo=UTC)
    assert helper.lookup_current(points, now).price_eur_mwh == 5.0
    assert helper.lookup_current(points, datetime(2026, 9, 17, 16, 0, tzinfo=UTC)) is None


def test_next_low():
    points = _pts([(13, 100.0), (14, 20.0), (15, 5.0)])
    # threshold in SEK/kWh with fx=10, vat=0, fee=0:
    #   14:00 -> 0.20 SEK/kWh ; 15:00 -> 0.05 SEK/kWh
    low = helper.next_low(points, 0.10, 10.0, 0.0, 0.0)
    assert low is not None
    assert low.start.hour == 15
    assert helper.next_low(points, None, 10.0, 0.0, 0.0) is None


def test_format_request_window_is_hour_aligned():
    now = datetime(2026, 9, 17, 13, 35, 41, tzinfo=UTC)
    from_iso, to_iso = helper.format_request_window(now, 36, 14)
    assert from_iso == "2026-09-16T01:00:00.000Z"
    assert to_iso == "2026-10-01T13:00:00.000Z"
    assert from_iso.endswith(":00:00.000Z")
    assert to_iso.endswith(":00:00.000Z")


# --- forecast period --------------------------------------------------------

def test_clamp_forecast_days():
    assert helper.clamp_forecast_days(14, 1, 14) == 14
    assert helper.clamp_forecast_days(30, 1, 14) == 14  # API caps at ~14 d
    assert helper.clamp_forecast_days(180, 1, 14) == 14
    assert helper.clamp_forecast_days(0, 1, 14) == 1
    assert helper.clamp_forecast_days(-5, 1, 14) == 1
    assert helper.clamp_forecast_days("7", 1, 14) == 7
    assert helper.clamp_forecast_days(6.9, 1, 14) == 7  # rounds
    assert helper.clamp_forecast_days(None, 1, 14) == 1
    assert helper.clamp_forecast_days("abc", 1, 14) == 1


def test_first_n_days_bounds_to_window():
    points = _pts([(0, 10.0), (1, 11.0), (2, 12.0)])
    # 00:30 during the 00:00 hour; window = [00:00, +1 day): all 3 hours are in it
    now = datetime(2026, 9, 17, 0, 30, tzinfo=UTC)
    assert [p.start.hour for p in helper.first_n_days(points, now, 1)] == [0, 1, 2]
    # 0/negative day bounds produce an empty window
    assert helper.first_n_days(points, now, 0) == []
    assert helper.first_n_days(points, now, -1) == []


def test_first_n_days_excludes_points_before_the_hour():
    points = _pts([(11, 0.0), (12, 1.0), (13, 2.0)])
    now = datetime(2026, 9, 17, 12, 45, tzinfo=UTC)
    assert [p.start.hour for p in helper.first_n_days(points, now, 2)] == [12, 13]


def test_clamp_window_trims_to_request_window():
    points = _pts([(0, 1.0), (12, 2.0), (24, 3.0), (48, 4.0), (72, 5.0)])
    now = datetime(2026, 9, 17, 13, 30, tzinfo=UTC)
    clamped = helper.clamp_window(points, now, 36, 2)
    # window = [2026-09-16T01:00Z, 2026-09-19T13:00Z) -> drops offset 72
    assert len(clamped) == 4
    assert clamped[-1].start == datetime(2026, 9, 19, 0, 0, tzinfo=UTC)
    # zero/negative forward_days end the window at the current hour (defensive;
    # the coordinator clamps forecast_days to [1, 14] before calling this)
    assert [p.start for p in helper.clamp_window(points, now, 36, -1)] == [
        datetime(2026, 9, 17, 0, 0, tzinfo=UTC),
        datetime(2026, 9, 17, 12, 0, tzinfo=UTC),
    ]


def test_load_simple_env():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        env_file = os.path.join(tmp, ".env")
        with open(env_file, "w", encoding="utf-8") as handle:
            handle.write(
                "# eupowerprices key for the dry-run (never commit)\n\n"
                "EUPOWERPRICES_API_KEY=abc123def\n"
                'QUOTED="some value"\n'
                "EMPTY=\n"
                "not-a-pair-line\n"
            )
        values = helper.load_simple_env(env_file)
        assert values["EUPOWERPRICES_API_KEY"] == "abc123def"
        assert values["QUOTED"] == "some value"
        assert values["EMPTY"] == ""
        assert "not-a-pair-line" not in values
        assert helper.load_simple_env(os.path.join(tmp, "missing.env")) == {}


# --- area/currency consistency ----------------------------------------------

def test_supported_currencies_derive_from_areas():
    import const  # noqa: E402 - same package dir as helper

    # the area list is offered A->Z in the dropdown
    assert const.SUPPORTED_AREAS == sorted(const.SUPPORTED_AREAS)
    # every curated area maps to a local currency
    assert set(const.SUPPORTED_AREAS) <= set(const.AREA_CURRENCIES)
    # the offered currencies are exactly the areas' currencies: no more, no fewer
    assert const.SUPPORTED_CURRENCIES == sorted(
        {const.AREA_CURRENCIES[a] for a in const.SUPPORTED_AREAS}
    )
    assert const.SUPPORTED_CURRENCIES == ["CZK", "DKK", "EUR", "NOK", "PLN", "SEK"]


def test_area_currency_spot_checks():
    import const  # noqa: E402 - same package dir as helper

    assert const.AREA_CURRENCIES["SE1"] == "SEK"
    assert const.AREA_CURRENCIES["DK2"] == "DKK"
    assert const.AREA_CURRENCIES["NO5"] == "NOK"
    assert const.AREA_CURRENCIES["CZ"] == "CZK"
    assert const.AREA_CURRENCIES["PL"] == "PLN"
    assert const.AREA_CURRENCIES["FI"] == "EUR"


# --- compact forecast attributes ---------------------------------------------

def test_compact_hours_shape_and_values():
    # 2026-03-29 01:00 UTC = Europe/Stockholm spring-forward instant (CEST),
    # when the local hour 02:00-03:00 does not exist — epoch round-trips must
    # still be exact.
    base = datetime(2026, 3, 29, 0, 0, tzinfo=UTC)
    hours = [
        {
            "start": base + timedelta(hours=i),
            "eur_mwh": 10.0 + i,
            "sek_kwh": 1.2 + i / 10,
            "source": "forecast",
        }
        for i in range(4)
    ]
    compact = helper.compact_hours(hours)
    assert len(compact) == len(hours)
    for orig, slim in zip(hours, compact):
        assert set(slim) == {"s", "p"}
        assert slim["s"] == int(orig["start"].timestamp())
        assert datetime.fromtimestamp(slim["s"], tz=UTC) == orig["start"]
        assert slim["p"] == orig["sek_kwh"]
    # order preserved
    assert [c["s"] for c in compact] == sorted(c["s"] for c in compact)


def test_forecast_attrs_under_recorder_limit():
    # Home Assistant's recorder drops state attributes above 16,384 bytes.
    # Replicates sensor.forecast_attrs for a worst-case 14-day horizon.
    base = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
    hours = [
        {
            "start": base + timedelta(hours=i),
            "eur_mwh": -8.0 + (i % 7),
            "sek_kwh": -1.2 + (i % 7),
            "source": "forecast",
        }
        for i in range(14 * 24)
    ]
    fc = {
        "available": True,
        "horizon_days": 14,
        "hours_count": len(hours),
        "days_count": 14,
        "avg_sek_kwh": 0.55,
        "days": [
            {
                "date": (base + timedelta(days=d)).date().isoformat(),
                "min_sek_kwh": -2.1,
                "max_sek_kwh": 3.4,
                "avg_sek_kwh": 0.55,
                "min_eur_mwh": -9.5,
                "min_at": (base + timedelta(days=d, hours=3)).isoformat(),
                "max_at": (base + timedelta(days=d, hours=19)).isoformat(),
                "hours_count": 24,
            }
            for d in range(14)
        ],
        "hours": helper.compact_hours(hours),
        "currency": "SEK",
        "fx_source": "live (Frankfurter/ECB)",
        "vat_pct": 25.0,
        "grid_fee_sek_kwh": 0.3,
    }
    size = len(json.dumps(fc, separators=(",", ":")).encode("utf-8"))
    assert size < 16384


if __name__ == "__main__":
    import traceback

    failures = 0
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
                print(f"PASS  {name}")
            except Exception:  # noqa: BLE001
                failures += 1
                print(f"FAIL  {name}")
                traceback.print_exc()
    print(f"\n{passed} passed, {failures} failed")
    sys.exit(1 if failures else 0)