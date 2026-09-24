# Spot Price — hourly electricity prices for Home Assistant

> ⚠️ This integration is **not affiliated with, endorsed by, or sponsored by**
> eupowerprices.com. When you enter an eupowerprices API key it uses their
> official `/v1` API (access must be requested); without a key it falls back to
> their public market API.

Monitor hourly spot prices in Home Assistant and steer devices (EV charger,
heat pump, water heater, …) by price. You pick the bidding zone (any of the
supported areas) and the integration converts EUR/MWh into your end-user
**price/kWh in your chosen currency**:

`price/kWh = (EUR/MWh × FX ÷ 1000) × (1 + VAT) + grid fee`,
where **FX is the live ECB (Frankfurter) EUR→your-currency rate** (or a fixed
value you set) and VAT + grid fee are entered in your currency.

Prices are shown in your chosen currency — the default is **EUR**. The offered
currencies follow the supported areas (one per area's country): SEK, DKK, NOK,
EUR, PLN, CZK.

---

## Repository layout

```
custom_components/spot_price/   the Home Assistant integration
tests/test_helper.py            unit tests (pytest)
dryrun.py                       standalone live API check (pure stdlib)
examples/automations-electricity.yaml
brand/                          HACS listing icon + logo (icon.png, logo.png)
hacs.json                       HACS metadata
```

## Development & testing

From the repo root:

```bash
python3 -m pytest tests/ -v        # unit tests
python3 dryrun.py --area SE1 --currency SEK --vat 25 --grid-fee 0.30 --window 3 --days 14
```

The dry-run hits the live API (pure stdlib, no HA required). To exercise the
official keyed API, put your key in a gitignored `.env` file next to
`dryrun.py` (`EUPOWERPRICES_API_KEY=...`) — the key is never printed or logged.

Releases are tags: bump `version` in
`custom_components/spot_price/manifest.json`, commit, tag `v<version>`, push,
and create a GitHub Release.

## Installation via HACS

1. Install **HACS** if you do not have it yet.
2. **HACS → ⋯ (top right) → Custom repositories** → add
   `https://github.com/daniel-jo/spot-price` → category **Integration** → **Add**.
3. **HACS → Integrations → Spot Price → Download** (use _Redownload_ when
   updating) → **Restart Home Assistant**.
4. **Settings → Devices & Services → Add Integration → “Spot Price”**.
5. Pick your **area/bidding zone** (any supported area) — or type your own code —
   pick your **currency** (default EUR; the list follows the areas' countries),
   optionally paste your eupowerprices **API key** (request it from the data
   provider; without a key the integration uses the public market API), and
   give the instance an optional name.
6. **Options**: VAT %, grid fee, cheapest-window size, low-price threshold, FX
   mode, forecast period and update interval — the currency and the API key can
   be changed here too.

Updates arrive as HACS update notifications whenever a new release is tagged
in this repository.

## Sensors

End-user price in your chosen **currency/kWh** (default EUR); area is your
configured zone (`<AREA>` → `sensor.spot_price_<AREA>_…`).

| Sensor | State | Notes |
|---|---|---|
| `current_price` | currency/kWh | running hour; raw EUR/MWh + FX/VAT/fee in attributes |
| `current_price_raw` | EUR/MWh | market price, pre-tax |
| `today_minimum` / `_maximum` / `_average` | currency/kWh | attrs: full hourly list, hour of min/max |
| `tomorrow_minimum` / `_maximum` / `_average` | currency/kWh | same; accurate after ~13:00 |
| `cheapest_window_today` | currency/kWh avg | best N consecutive **remaining** hours; `start`/`end` attrs |
| `cheapest_window_tomorrow` | currency/kWh avg | best N consecutive hours of tomorrow |
| `next_low_price` | currency/kWh | first hour ≤ configured threshold (else unavailable) |
| `forecast` | currency/kWh avg | full configured period (default 14 days); compact `hours` (`[{s, p}]`: unix epoch, end-user price) + per-day `days` — under HA's 16 kB attribute limit |
| `history` | currency/kWh avg | always-on last 24 h of realized prices; full hourly `hours` + min/max/avg in attributes |
| `fx_rate` | `EUR/<currency>` | FX used (EUR→your currency) |
| `last_update` / `last_error` | — | diagnostics |

## Steering devices

- **Cheapest-window charging** — trigger on `cheapest_window_*` change, wait
  for the `start`/`end` attributes, toggle the device.
- **Cheap-hour gating** — a 5-minute `time_pattern` automation switches devices
  only while `current_price` is below e.g. today's average (+margin).
- **“Next low price” scheduling** — with a threshold configured, pre-arm longer
  jobs for the hour given by the `start` attribute.

For the Energy dashboard, add `current_price` (~your-currency/kWh) as the grid price
entity. If your dashboard requires EUR, add a template sensor
`{{ states('sensor.spot_price_<AREA>_current_price_raw') | float / 1000 }}`
with unit `EUR/kWh` and use that.

### Charting future prices

The integration ships a **built-in `spot-price-card`** Lovelace card that draws
today's elapsed hours, the running hour and the whole configured forecast as a
line chart (no area fill), with a vertical "now" marker, an average line,
cheap-hour dots, hover details and the unit along the y-axis. The line is
**solid yellow** for the official day-ahead hours (today + tomorrow,
"Actual") and **dashed yellow** beyond that ("Forecast"). The current price
and average are shown in the header; all card text is English (overridable
via `labels`).

1. After installing/updating, **restart Home Assistant**, then add the card
   script as a dashboard resource:
   **Settings → Dashboards → ⋯ (top right) → Resources → Add resource** with
   URL `/spot_price/spot-price-card.js` and type **JavaScript module**.
2. Add a card on any dashboard:

```yaml
type: custom:spot-price-card
entity: sensor.spot_price_SE3_forecast  # optional – auto-detected
title: Elpris                             # optional
show_past: true                           # include today's elapsed hours
threshold: 1.0                            # optional: mark cheap hours green
```

Options: `entity` (auto-detected when omitted), `title`, `show_past` (default
`true`), `height` (px, default 280), `threshold` (hours at/below this price get
a green dot on the line), `show_avg_line` / `show_legend` (default `true`),
and `labels` (override the English UI strings, e.g. `actual`, `forecast`,
`now_price`).

#### Alternative: any chart card (e.g. ApexCharts)

You can equally chart the `forecast` sensor's `hours` attribute with any
third-party chart card (e.g. **ApexCharts**):

```yaml
type: custom:apexcharts-card
graph_span: 14d
now:
  show: false
header:
  show: true
  title: EL-spotpris
series:
  - entity: sensor.spot_price_<AREA>_forecast
    attribute: hours
    type: column
    data_generator: |
      return entity.attributes.hours.map(h => ({
        x: new Date(h.s * 1000).getTime(),
        y: h.p
      }));
```

The `hours` attribute covers exactly the period you configured (default
14 days) in a compact shape (`s` = unix epoch seconds, `p` = end-user price) so
the whole horizon fits Home Assistant's 16 kB state-attribute limit; lower
`graph_span` if you shorten it.

## Data source notes

- Two backends: the **official keyed API**
  (`GET api.eupowerprices.com/v1/forecasts/<AREA>/latest` with header
  `X-API-Key`) when a key is configured, otherwise the **public markets API**
  (`/presentation/v1/markets`) as fallback.
- Prices are **EUR/MWh** and can be **negative**; the forecast horizon caps at
  **~14 days** (the integration clamps the “forecast period” option to 1–14).
- Areas: the dropdown is fetched live from the API (public
  `/presentation/v1/areas` at setup, key-scoped `/v1/status` in the options)
  and sorted A–Z; curated fallback: `DK1` `DK2` `FI` `NO1–NO5` `SE1–SE4`,
  `EE` `LT` `LV`, `AT` `BE` `CZ` `DE` `FR` `NL` `PL` `SK`, `ES` `PT`. Custom
  codes are checked live against the API.
- **Refresh schedule** — the first fetch after a restart waits for the next
  **13:30 Europe/Stockholm** (CET/CEST) market-time anchor — the day-ahead
  prices are published around 13:00 — then refreshes every hour (configurable
  30 min–24 h) while always snapping back to the 13:30 anchor. Timezone-neutral
  and DST-correct: the anchor is resolved against the market zone, never your
  HA instance's timezone. A fresh install with no data fetches immediately; a
  cache is served until the first anchored poll. Browser-like headers are
  presented (the API sits behind Cloudflare) and the last good payload is
  cached to `/config/spot_price/cache.json`.
- Prices shown are **market spot + VAT + grid fee**, not your full tariff —
  the supplier's fixed per-kWh price and monthly fee are added on the invoice.

## Support

Open an issue at <https://github.com/daniel-jo/spot-price/issues>.
