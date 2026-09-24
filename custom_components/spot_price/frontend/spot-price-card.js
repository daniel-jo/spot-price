/*
 * Spot Price card – bundled Lovelace card for the "Spot Price" integration.
 *
 * Served by the integration at `/spot_price/spot-price-card.js` (registered in
 * __init__.py via `hass.http.register_static_path`). To use it:
 *
 *   1. Settings → Dashboards → ⋯ → Resources → Add resource
 *      URL: /spot_price/spot-price-card.js   Type: JavaScript module
 *   2. Add a card to any dashboard:
 *
 *        type: custom:spot-price-card
 *        entity: sensor.spot_price_SE3_forecast   # optional, auto-detected
 *        title: Spot price                      # optional
 *        show_past: true                           # merge today's elapsed hours
 *        threshold: 0.5                            # optional cheap-hour band
 *
 * Pure vanilla JS + SVG – no external chart library. Renders a line chart
 * (no area fill): solid yellow for the official day-ahead hours (today +
 * tomorrow), dashed yellow beyond that, a vertical "now" marker, the current
 * price + average in the header, and the unit along the y-axis.
 *
 * Data source: the forecast sensor's `hours` attribute in a compact shape
 * (`[{ s: unix_epoch, p: end-user price }, …]`, kept tiny so the whole
 * configured horizon fits Home Assistant's 16 kB state-attribute limit) plus,
 * when `show_past`, today's elapsed hours from
 * `sensor.spot_price_<AREA>_today_minimum` (`{ start, price_kwh, … }`).
 */
(function () {
  "use strict";

  const HOUR_MS = 3_600_000;

  const DEFAULTS = {
    entity: "",
    title: "",
    show_past: true,
    height: 280,
    threshold: null,
    show_avg_line: true,
    show_legend: true,
    labels: {
      actual: "Actual",
      past: "Past",
      now: "Now",
      forecast: "Forecast",
      now_price: "Now",
      cheap: "Cheap",
      average: "Average",
      price: "Price:",
      avg: "Avg",
      no_data: "No data yet",
    },
  };

  // --- tiny helpers ----------------------------------------------------------

  const fmtPrice = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[ch]));
  }

  function niceTicks(min, max, count) {
    const span = max - min || 1;
    const raw = span / count;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    let step;
    if (norm < 1.5) step = 1;
    else if (norm < 3) step = 2;
    else if (norm < 7) step = 5;
    else step = 10;
    step *= mag;
    const from = Math.floor(min / step) * step;
    const to = Math.ceil(max / step) * step;
    const out = [];
    for (let v = from; v <= to + step * 1e-6; v += step) out.push(v);
    return out;
  }
  // --- card -----------------------------------------------------------------

  class SpotPriceCard extends HTMLElement {
    constructor() {
      super();
      this._config = null;
      this._hass = null;
      this._root = null;
      this._bars = [];
      this._sizeKey = "";
      this._renderedKey = null;
      this._resizeHandler = () => this._render();
    }

    setConfig(config) {
      if (!config || typeof config !== "object" || Array.isArray(config)) {
        throw new Error("Spot Price card: config must be an object");
      }
      this._config = {
        ...DEFAULTS,
        ...config,
        labels: { ...DEFAULTS.labels, ...(config.labels || {}) },
      };
      this._renderedKey = null; // force a full re-render
    }

    set hass(hass) {
      this._hass = hass;
      if (!this._config) return;
      this._ensureDom();
      const key = this._renderKey();
      if (key !== this._renderedKey) {
        this._renderedKey = key;
        this._render();
      }
    }

    getCardSize() {
      return 3;
    }

    _renderKey() {
      const states = (this._hass && this._hass.states) || {};
      const ids = this._ids();
      const stamp = (id) => {
        const st = id ? states[id] : null;
        return st ? `${st.state}|${st.last_updated}` : "";
      };
      return JSON.stringify([
        this._config,
        this._sizeKey,
        stamp(ids.forecastId),
        stamp(ids.todayId),
      ]);
    }

    _ids() {
      const states = (this._hass && this._hass.states) || {};
      let forecastId = this._config.entity || "";
      if (!forecastId) {
        const found = Object.keys(states)
          .filter((eid) => /^sensor\.spot_price_.+_forecast$/i.test(eid))
          .sort();
        forecastId = found.length ? found[0] : "";
      }
      let todayId = "";
      if (forecastId.endsWith("_forecast")) {
        todayId = `${forecastId.slice(0, -"_forecast".length)}_today_minimum`;
        if (!states[todayId]) todayId = "";
      }
      return { forecastId, todayId };
    }

    _collect() {
      const states = (this._hass && this._hass.states) || {};
      const ids = this._ids();
      const byStart = new Map();

      // Today's full hourly list first (gives us the elapsed hours of today).
      if (this._config.show_past && ids.todayId && states[ids.todayId]) {
        const attrs = states[ids.todayId].attributes || {};
        if (Array.isArray(attrs.hours)) {
          for (const hour of attrs.hours) {
            if (!hour || !hour.start) continue;
            const ts = new Date(hour.start).getTime();
            if (!Number.isNaN(ts)) byStart.set(ts, hour);
          }
        }
      }

      // The forecast (running hour + future) second – wins on overlap.
      // Entries arrive in the compact `{ s, p }` shape (the whole 14-day
      // period must stay under HA's 16 kB attribute limit); normalise them
      // back to the extended `{ start, price_kwh }` form the renderer expects.
      // Both sources dedupe on the same UTC instant (via `Date.getTime()` /
      // `s * 1000`) so overlapping hours never get plotted twice.
      const forecastState = ids.forecastId ? states[ids.forecastId] : null;
      if (forecastState) {
        const attrs = forecastState.attributes || {};
        if (Array.isArray(attrs.hours)) {
          for (const hour of attrs.hours) {
            if (!hour) continue;
            let ts;
            let entry = hour;
            if (typeof hour.s === "number") {
              ts = hour.s * 1000;
              if (Number.isNaN(ts)) continue;
              entry = { start: new Date(ts).toISOString(), price_kwh: hour.p };
            } else if (hour.start) {
              ts = new Date(hour.start).getTime();
              if (Number.isNaN(ts)) continue;
            } else {
              continue;
            }
            byStart.set(ts, entry);
          }
        }
      }

      if (!byStart.size) {
        return {
          error: !forecastState
            ? this._config.labels.no_data
            : String(forecastState.state),
          hours: [],
          unit: "",
          avg: null,
        };
      }

      const nowMs = Date.now();
      const currentHourMs = Math.floor(nowMs / HOUR_MS) * HOUR_MS;

      const hours = [...byStart.values()]
        .map((hour) => {
          const start = new Date(hour.start);
          if (Number.isNaN(start.getTime())) return null;
          const price =
            typeof hour.price_kwh === "number" && Number.isFinite(hour.price_kwh)
              ? hour.price_kwh
              : null;
          if (price === null) return null;
          const eur =
            typeof hour.eur_mwh === "number" && Number.isFinite(hour.eur_mwh)
              ? hour.eur_mwh
              : null;
          const ts = start.getTime();
          return {
            start,
            ts,
            price,
            eur,
            source: hour.source || "forecast",
            bucket:
              ts < currentHourMs
                ? "past"
                : ts === currentHourMs
                  ? "now"
                  : "future",
          };
        })
        .filter((hour) => hour !== null)
        .sort((a, b) => a.ts - b.ts);

      const attrs = (forecastState && forecastState.attributes) || {};
      const currency = attrs.currency || "";
      const unit =
        attrs.unit_of_measurement || (currency ? `${currency}/kWh` : "");
      const avgAttr = attrs.avg_kwh;
      const avg =
        typeof avgAttr === "number" && Number.isFinite(avgAttr)
          ? avgAttr
          : hours.length
            ? hours.reduce((sum, hour) => sum + hour.price, 0) / hours.length
            : null;

      return { error: "", hours, unit, avg };
    }
_ensureDom() {
      if (this._root) return;
      this._root = this.attachShadow({ mode: "open" });
      this._root.innerHTML = `
        <style>
          :host { display: block; }
          .card {
            background: var(--ha-card-background, var(--card-background-color, #fff));
            border: 1px solid var(--ha-card-border-color, var(--divider-color, #e2e2e2));
            border-radius: var(--ha-card-border-radius, 12px);
            box-shadow: var(--ha-card-box-shadow, 0 2px 10px rgba(0, 0, 0, 0.12));
            color: var(--primary-text-color, #212121);
            font-family: var(--primary-font-family, "Roboto", "Helvetica", sans-serif);
            padding: 14px 16px 12px;
          }
          .header { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 8px; }
          .title { font-size: 16px; font-weight: 600; }
          .sub { font-size: 13px; color: var(--secondary-text-color, #666); white-space: nowrap; }
          .chart-wrap { position: relative; width: 100%; overflow: hidden; }
          .chart-wrap svg { display: block; width: 100%; }
          .axis-label { fill: var(--secondary-text-color, #666); font-family: inherit; font-size: 11px; }
          .grid-line { stroke: var(--divider-color, #e3e3e3); stroke-width: 1; }
          .day-label { fill: var(--secondary-text-color, #999); font-family: inherit; font-size: 10px; }
          .now-line { stroke: var(--secondary-text-color, #888); stroke-width: 1.5; stroke-dasharray: 4 3; }
          .avg-line { stroke: var(--state-info-color, #1e88e5); stroke-width: 1.5; stroke-dasharray: 5 3; }
          .line-actual { fill: none; stroke: #fbc02d; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
          .line-forecast { fill: none; stroke: #fbc02d; stroke-width: 2; stroke-dasharray: 8 5; stroke-linejoin: round; stroke-linecap: round; }
          .axis-unit { fill: var(--secondary-text-color, #666); font-family: inherit; font-size: 11px; }
          .dot-now { fill: #fbc02d; stroke: var(--ha-card-background, #fff); stroke-width: 1.2; }
          .dot-cheap { fill: var(--state-ok-color, #43a047); }
          .legend { display: flex; flex-wrap: wrap; gap: 14px; margin: 0 0 8px; font-size: 12px; color: var(--secondary-text-color, #666); justify-content: flex-end; }
          .legend .swatch { display: inline-block; width: 18px; height: 0; border-top: 3px solid #000; margin-right: 6px; vertical-align: -1px; }
          .legend .swatch.dashed { border-top-style: dashed; }
          .legend .swatch.vert { width: 0; height: 14px; border-top: none; border-left: 3px dashed #fbc02d; margin-right: 6px; vertical-align: -2px; }
          .tooltip {
            position: absolute; z-index: 5; pointer-events: none; white-space: nowrap;
            background: var(--ha-card-background, var(--card-background-color, #fff));
            color: var(--primary-text-color, #212121);
            border: 1px solid var(--divider-color, #ddd); border-radius: 8px;
            box-shadow: 0 4px 14px rgba(0, 0, 0, 0.18); padding: 8px 10px; font-size: 12px;
            line-height: 1.5;
          }
          .tooltip[hidden] { display: none; }
          .tt-title { font-weight: 600; }
        </style>
        <div class="card">
          <div class="header">
            <div class="title"></div>
            <div class="sub"></div>
          </div>
          <div class="legend"></div>
          <div class="chart-wrap">
            <div class="tooltip" hidden></div>
            <svg></svg>
          </div>
        </div>
      `;
      this._title = this._root.querySelector(".title");
      this._sub = this._root.querySelector(".sub");
      this._wrap = this._root.querySelector(".chart-wrap");
      this._svg = this._root.querySelector("svg");
      this._tooltip = this._root.querySelector(".tooltip");
      this._legend = this._root.querySelector(".legend");

      this._wrap.addEventListener("mousemove", (event) => this._onMove(event));
      this._wrap.addEventListener("mouseleave", () => {
        if (this._tooltip) this._tooltip.hidden = true;
      });
      window.addEventListener("resize", this._resizeHandler);
      if ("ResizeObserver" in window) {
        this._resizeObserver = new ResizeObserver(() => this._render());
        this._resizeObserver.observe(this._wrap);
      }
    }

    disconnectedCallback() {
      window.removeEventListener("resize", this._resizeHandler);
      if (this._resizeObserver) this._resizeObserver.disconnect();
    }

    _areaFromEntity(forecastId) {
      return forecastId
        .replace(/^sensor\.spot_price_/, "")
        .replace(/_forecast$/, "")
        .toUpperCase();
    }

    _render() {
      if (!this._root) return;
      this._sizeKey = `${this._wrap.clientWidth}x${this._config.height}`;
      const data = this._collect();
      const labels = this._config.labels;
      const area = this._areaFromEntity(this._ids().forecastId);
      this._lastUnit = data.unit;

      // Header + "price X <unit>, avg: Y <unit>" summary (upper right).
      this._title.textContent =
        this._config.title || (area ? `Spot price ${area}` : "Spot price");
      const current = data.hours.find((hour) => hour.bucket === "now");
      const unitTxt = data.unit ? ` ${data.unit}` : "";
      const summary = [];
      if (current) {
        summary.push(`${labels.price} ${fmtPrice.format(current.price)}${unitTxt}`);
      }
      if (data.avg != null) {
        summary.push(`${labels.avg}: ${fmtPrice.format(data.avg)}${unitTxt}`);
      }
      this._sub.innerHTML = escapeHtml(summary.join(", ") || unitTxt.trim());

      // Legend.
      const legendItems = [
        ["#fbc02d", labels.actual, false],
        ["#fbc02d", labels.forecast, true],
        ["var(--secondary-text-color, #888)", labels.now_price, "vert"],
      ];
      if (this._config.threshold != null) {
        legendItems.push(["var(--state-ok-color, #43a047)", labels.cheap, false]);
      }
      if (this._config.show_avg_line) {
        legendItems.push(["var(--state-info-color, #1e88e5)", labels.average, true]);
      }
      this._legend.innerHTML = this._config.show_legend
        ? legendItems
            .map(([color, text, kind]) => {
              const cls =
                kind === "vert"
                  ? "swatch vert"
                  : kind === true
                    ? "swatch dashed"
                    : "swatch";
              const style =
                kind === "vert"
                  ? `border-left-color:${color}`
                  : `border-top-color:${color}`;
              return `<span><span class="${cls}" style="${style}"></span>${escapeHtml(text)}</span>`;
            })
            .join("")
        : "";

      const width = Math.max(360, this._wrap.clientWidth);
      const height = this._config.height;
      const M = { top: 14, right: 14, bottom: 26, left: 48 };

      this._svg.setAttribute("width", width);
      this._svg.setAttribute("height", height);
      this._svg.setAttribute("viewBox", `0 0 ${width} ${height}`);

      if (data.error || !data.hours.length) {
        this._bars = [];
        this._svg.innerHTML = `<text x="${width / 2}" y="${(height + M.top - M.bottom) / 2}" text-anchor="middle" fill="var(--secondary-text-color, #888)" font-family="inherit" font-size="14">${escapeHtml(data.error || labels.no_data)}</text>`;
        return;
      }

      const hours = data.hours;

      // Y domain.
      let lo = Number.POSITIVE_INFINITY;
      let hi = Number.NEGATIVE_INFINITY;
      for (const hour of hours) {
        if (hour.price < lo) lo = hour.price;
        if (hour.price > hi) hi = hour.price;
      }
      if (data.avg !== null) {
        lo = Math.min(lo, data.avg);
        hi = Math.max(hi, data.avg);
      }
      if (!Number.isFinite(lo)) lo = 0;
      if (hi - lo < 1e-6) {
        lo -= 1;
        hi += 1;
      }
      const pad = (hi - lo) * 0.08 || 0.1;
      lo -= pad;
      hi += pad;

      const ticks = niceTicks(lo, hi, 5);
      const low = ticks[0];
      const high = ticks[ticks.length - 1];
      const plotH = height - M.top - M.bottom;
      const plotW = width - M.left - M.right;
      const y = (v) => M.top + ((high - v) / (high - low)) * plotH;
      const slot = plotW / hours.length;
      const { threshold } = this._config;
      const out = [];

      // Grid + Y labels.
      for (const tick of ticks) {
        const yy = y(tick);
        out.push(
          `<line class="grid-line" x1="${M.left}" y1="${yy.toFixed(1)}" x2="${width - M.right}" y2="${yy.toFixed(1)}"/>`,
          `<text class="axis-label" x="${M.left - 8}" y="${(yy + 4).toFixed(1)}" text-anchor="end">${fmtPrice.format(tick)}</text>`,
        );
      }

      // Unit label, rotated along the left edge of the y-axis, flush with the
      // left side of the chart (no gap).
      if (data.unit) {
        const ux = 8;
        const uy = M.top + plotH / 2;
        out.push(
          `<text class="axis-unit" x="${ux}" y="${uy.toFixed(1)}" text-anchor="middle" transform="rotate(-90 ${ux} ${uy.toFixed(1)})">${escapeHtml(data.unit)}</text>`,
        );
      }

      // Day labels (every day, or every other when long horizon).
      let dayCount = 0;
      const everyOther = hours.length > 24 * 7;
      for (let i = 0; i < hours.length; i += 1) {
        const start = hours[i].start;
        if (start.getHours() !== 0) continue;
        dayCount += 1;
        if (everyOther && dayCount % 2 === 0) continue;
        const xx = M.left + i * slot;
        const day = start.toLocaleDateString("en-GB", {
          day: "numeric",
          month: "numeric",
        });
        out.push(
          `<text class="day-label" x="${(xx + 4).toFixed(1)}" y="${height - M.bottom + 8}">${escapeHtml(day)}</text>`,
        );
      }

      // Hourly line chart. "Actual" is the first two distinct local dates present
      // in the data (today + the next day's day-ahead prices) – drawn as a
      // solid yellow line; everything after that is a dashed yellow overlay.
      // No area fill. The boundary is derived from the data itself so it stays
      // correct regardless of timezone or how fresh the browser clock is.
      const dateKey = (start) =>
        start.getFullYear() * 10000 + (start.getMonth() + 1) * 100 + start.getDate();
      const seenDates = [];
      for (const hour of hours) {
        const k = dateKey(hour.start);
        if (seenDates.indexOf(k) === -1) seenDates.push(k);
      }
      seenDates.sort((a, b) => a - b);
      const actualBoundary = seenDates.length > 1 ? seenDates[1] : seenDates[0];

      const points = [];
      for (let i = 0; i < hours.length; i += 1) {
        const hour = hours[i];
        points.push({
          x: M.left + i * slot + slot / 2,
          y: y(hour.price),
          hour,
          actual: dateKey(hour.start) <= actualBoundary,
        });
      }

      function pathD(pts) {
        let d = "";
        for (let k = 0; k < pts.length; k += 1) {
          const prev = pts[k - 1];
          const p = pts[k];
          d +=
            !prev || prev.hour.ts !== p.hour.ts - HOUR_MS
              ? `M ${p.x.toFixed(1)} ${p.y.toFixed(1)}`
              : `L ${p.x.toFixed(1)} ${p.y.toFixed(1)}`;
        }
        return d;
      }

      // Gray dashed vertical "now" marker – drawn before the price paths so it
      // sits behind the yellow lines.
      const nowPoint = points.find((p) => p.hour.bucket === "now");
      if (nowPoint) {
        out.push(
          `<line class="now-line" x1="${nowPoint.x.toFixed(1)}" y1="${M.top}" x2="${nowPoint.x.toFixed(1)}" y2="${height - M.bottom}"/>`,
        );
      }

      // Solid "actual" path only through the actual points (today + tomorrow's
      // day-ahead hours). The dashed forecast path below is a separate path for
      // the rest, so the same-colored solid line can never show through the
      // dash gaps and make the whole graph look solid.
      const actualPts = points.filter((p) => p.actual);
      if (actualPts.length) {
        out.push(`<path class="line-actual" d="${pathD(actualPts)}"/>`);
      }

      // Dashed "forecast" path for everything beyond the day-ahead horizon.
      // It starts at the last actual point so the line stays continuous across
      // the actual/forecast boundary.
      const forecastPts = points.filter((p) => !p.actual);
      if (forecastPts.length) {
        const bridge = (actualPts.length ? [actualPts[actualPts.length - 1]] : []).concat(
          forecastPts,
        );
        out.push(`<path class="line-forecast" d="${pathD(bridge)}"/>`);
      }

      // Invisible per-hour hover columns.
      this._bars = points.map((p) => ({
        x: p.x - slot / 2,
        w: slot,
        y: p.y,
        hour: p.hour,
      }));

      // Cheap hours below the threshold get a small green dot.
      for (const p of points) {
        if (threshold != null && p.hour.price <= threshold) {
          out.push(
            `<circle class="dot-cheap" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="2.6"/>`,
          );
        }
      }

      // Running hour: filled dot on top of the price lines.
      if (nowPoint) {
        out.push(
          `<circle class="dot-now" cx="${nowPoint.x.toFixed(1)}" cy="${nowPoint.y.toFixed(1)}" r="4"/>`,
        );
      }

      // Average line (no text – the value is shown in the header).
      if (this._config.show_avg_line && data.avg !== null && data.avg !== undefined) {
        const ay = y(data.avg);
        out.push(
          `<line class="avg-line" x1="${M.left}" y1="${ay.toFixed(1)}" x2="${width - M.right}" y2="${ay.toFixed(1)}"/>`,
        );
      }

      this._svg.setAttribute("width", plotW + M.left + M.right);
      this._svg.setAttribute("height", height);
      this._svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
      this._svg.innerHTML = out.join("");
    }

    _onMove(event) {
      if (!this._tooltip) return;
      const rect = this._wrap.getBoundingClientRect();
      const x = event.clientX - rect.left;
      const bar = this._bars.find((b) => x >= b.x - 1 && x <= b.x + b.w + 1);
      if (!bar) {
        this._tooltip.hidden = true;
        return;
      }
      const hour = bar.hour;
      const date = hour.start.toLocaleString("en-GB", {
        weekday: "short",
        day: "numeric",
        month: "short",
      });
      const clock = hour.start.toLocaleTimeString("en-GB", {
        hour: "2-digit",
        minute: "2-digit",
      });
      const parts = [
        `<div class="tt-title">${escapeHtml(date)} ${escapeHtml(clock)}</div>`,
        `<div>${fmtPrice.format(hour.price)} ${escapeHtml(this._lastUnit)}</div>`,
      ];
      this._tooltip.innerHTML = parts.join("");
      this._tooltip.hidden = false;
      const tw = this._tooltip.offsetWidth;
      const th = this._tooltip.offsetHeight;
      let left = bar.x + bar.w / 2 - tw / 2;
      left = Math.max(4, Math.min(rect.width - tw - 4, left));
      let top = bar.y - th - 6;
      top = Math.max(4, top);
      this._tooltip.style.left = `${left}px`;
      this._tooltip.style.top = `${top}px`;
    }
  }

  if (!customElements.get("spot-price-card")) {
    customElements.define("spot-price-card", SpotPriceCard);
  }
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "spot-price-card",
    name: "Spot Price",
    description: "Current and forecast hourly electricity prices",
  });
})();
