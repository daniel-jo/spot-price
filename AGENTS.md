# Spot Price — Home Assistant integration

Rules for agents working in this repository (the standalone, public **Spot Price**
HA integration). Source, tests and docs all live here; releases are
tags pushed to this repo (`v<version>`).

## Plan mode

- In **plan mode** the agent is strictly read-only: explore, analyze and
  present a plan only. Do **not** edit or create files and do **not** run any
  state-changing commands (including `git`).
- Stop after presenting the plan and wait for the user to switch to **act
  mode** before making changes.
- Read-only inspection and validation (reads, searches, `pytest`,
  `node --check`) are fine in plan mode; anything that writes is not.

## Layout

```
custom_components/spot_price/    # the HA integration
├── __init__.py                  # platform setup & Lovelace card serving
├── config_flow.py               # setup UI & options flow
├── const.py                     # constants, area/currency tables
├── coordinator.py               # data fetching, caching, view construction
├── helper.py                    # pure logic (parsing, grouping, windowing)
├── sensor.py                    # sensor entities
├── frontend/spot-price-card.js  # bundled Lovelace dashboard card
├── translations/                # localised strings (en.json, sv.json)
├── strings.json                 # config-flow translation placeholders
├── manifest.json                # HA/HACS metadata
dryrun.py                        # standalone live API check (pure stdlib)
tests/
├── test_helper.py               # unit tests for helper.py (pytest)
└── test_frontend.py             # layout/wiring tests for the card
examples/automations-electricity.yaml
.env.example                     # dry-run API key template
hacs.json                        # HACS metadata
README.md
```

## Commands

- Tests: `python3 -m pytest tests/ -v`
- Run tests standalone (no pytest): `python3 tests/test_helper.py`
- Validate the bundled JS card: `node --check custom_components/spot_price/frontend/spot-price-card.js`
- Dry-run against the live API:
  `python3 dryrun.py --area SE1 --vat 25 --grid-fee 0.30 --window 3`

## Git hygiene

- Never run state-changing git commands (commit, push, tag, rebase, force-push,
  branch, …) on your own initiative. Stop after the change is made and verified;
  committing and pushing are up to the user. Do it only when the user
  explicitly asks — the "Releasing" steps below are no exception.

## Releasing

1. Bump `version` in **both** `custom_components/spot_price/manifest.json` and
   `custom_components/spot_price/const.py` (`VERSION` constant) — the two must
   always match.
2. Commit, tag `v<version>` and push: `git push origin main --tags`.
3. Create a GitHub Release for the tag (gh CLI or the web UI).

## Contracts you must not break

- Sensor entity IDs: `sensor.spot_price_<AREA>_<name>`
  (e.g. `sensor.spot_price_<AREA>_current_price`).
- Day grouping by `Europe/Stockholm` calendar date, including DST; the local day
  boundary is 22:00 Z in summer (CEST) / 23:00 Z in winter (CET).
- API `from`/`to` timestamps must be hour-aligned (`HH:00:00`) — anything else
  returns HTTP 422; the integration rounds both down to the hour automatically.
- Pricing formula: `price/kWh = (EUR/MWh × FX ÷ 1000) × (1 + VAT) + grid fee`,
  where `FX` is the EUR → configured-currency rate; the configured currency
  (default EUR) is a config option and sensor units/attributes follow it.
- Bundled dashboard card: the card must stay at
  `custom_components/spot_price/frontend/spot-price-card.js`, be served by
  `__init__.py` at `/spot_price/spot-price-card.js` (via
  `hass.http.register_static_path`), and keep the registered custom element
  name `spot-price-card`.

## API etiquette

- Browser-like headers (Chrome user-agent + `Accept`, `Sec-Fetch-*`, …) are
  required because the API sits behind Cloudflare.
- Fetch sparingly: at most every 30 minutes (default 6 h), and cache the last
  good payload to `/config/spot_price/cache.json`.

## Style

- Pure stdlib, async patterns, type hints.
- Manifest stays `requirements: []` unless there is a real need — say so loudly
  if you ever need to add a dependency.
- Formatting follows HA core conventions: black (88‑column line length), no
  trailing whitespace. There is no formatter config in this repository — adhere
  to the implied style of the existing code.
- Prefer one clear responsibility per file; if a module passes roughly 400–500
  lines, consider extracting focused logic into a helper (e.g. `helper.py`)
  rather than letting the module keep growing.

## Secrets

- `.env` is gitignored and used only by `dryrun.py` (eupowerprices API key).
  Never read, open, or commit its contents; the HA component itself takes the
  key in the setup UI.

## Scope

- The agent must **never read, modify, or create files outside this repository's
  root** (`/Users/dane/projects/daniel-jo/spot-price/`). All operations —
  reading, searching, editing, creating files, running commands — are
  restricted to this directory and its subdirectories.