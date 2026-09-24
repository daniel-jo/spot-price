"""Sensors exposed by the Spot Price integration."""

from __future__ import annotations

import logging
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import helper
from .const import (
    CONF_CURRENCY,
    DEFAULT_CURRENCY,
    DOMAIN,
    NAME,
    UNIT_EUR_PER_MWH,
    VERSION,
    unit_eur_to,
    unit_price_per_kwh,
)
from .coordinator import EupowerpricesCoordinator

_LOGGER = logging.getLogger(__name__)


def _iso(value) -> str:
    return dt_util.as_local(value).isoformat()


def _nice_hours(hours: list[dict]) -> list[dict]:
    out = []
    for hour in hours:
        entry = {
            "start": _iso(hour["start"]),
            "eur_mwh": hour["eur_mwh"],
            "price_kwh": hour["price_kwh"],
        }
        if "source" in hour:
            entry["source"] = hour["source"]
        out.append(entry)
    return out


def _current(data: dict) -> dict:
    return (data.get("current") or {})


def _today(data: dict) -> dict:
    return (data.get("today") or {})


def _tomorrow(data: dict) -> dict:
    return (data.get("tomorrow") or {})


def _today_window(data: dict) -> dict:
    return (data.get("today_window") or {})


def _tomorrow_window(data: dict) -> dict:
    return (data.get("tomorrow_window") or {})


def _low(data: dict) -> dict:
    return (data.get("next_low") or {})


def _history(data: dict) -> dict:
    return (data.get("history") or {})


def _day_attrs(day: dict) -> dict:
    if not day.get("available"):
        return {"available": False}
    return {
        "available": True,
        "date": day.get("date"),
        "min_kwh": day.get("min_kwh"),
        "max_kwh": day.get("max_kwh"),
        "avg_kwh": day.get("avg_kwh"),
        "min_eur_mwh": day.get("min_eur"),
        "min_at": _iso(day["min_at"]),
        "max_at": _iso(day["max_at"]),
        "hours_count": day.get("hours_count"),
        "hours": _nice_hours(day.get("hours", [])),
    }


def _window_attrs(win: dict, data: dict) -> dict:
    if not win:
        return {"available": False}
    return {
        "available": True,
        "start": _iso(win["start"]),
        "end": _iso(win["end"]),
        "window_hours": data.get("window_hours"),
        "avg_eur_mwh": win.get("avg_eur_mwh"),
        "hours": _nice_hours(win.get("hours", [])),
    }


def _fx_attrs(data: dict) -> dict:
    return {
        "fx_source": data.get("fx_source"),
        "currency": data.get("currency"),
        "vat_pct": data.get("vat_pct"),
        "grid_fee_kwh": data.get("grid_fee_kwh"),
    }


class EupowerpricesSensor(CoordinatorEntity, SensorEntity):
    """Base sensor bound to the shared coordinator."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EupowerpricesCoordinator,
        key: str,
        name: str,
        value_fn: Callable[[dict], Any],
        attrs_fn: Callable[[dict], dict],
        unit: str | None = None,
        device_class: str | None = None,
        state_class: str | None = None,
        icon: str | None = None,
        entity_category: EntityCategory | None = None,
    ) -> None:
        super().__init__(coordinator)
        area = coordinator.options.get("area", "")
        self._attr_unique_id = f"{DOMAIN}_{area}_{key}"
        self._attr_name = name
        self._attr_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        self._attr_icon = icon
        self._attr_entity_category = entity_category
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, area)},
            name=f"{NAME} {area}",
            manufacturer=NAME,
            sw_version=VERSION,
        )
        self._value_fn = value_fn
        self._attrs_fn = attrs_fn

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        if data is None:
            return False
        try:
            return self._value_fn(data) is not None
        except (KeyError, TypeError):
            return False

    @property
    def native_value(self) -> Any:
        return self._value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._attrs_fn(self.coordinator.data)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Spot Price sensors from a config entry."""
    coordinator: EupowerpricesCoordinator = hass.data[DOMAIN][entry.entry_id]
    currency = str(
        coordinator.options.get(CONF_CURRENCY, DEFAULT_CURRENCY) or DEFAULT_CURRENCY
    ).upper()
    price_unit = unit_price_per_kwh(currency)
    fx_unit = unit_eur_to(currency)
    fx_name = f"FX rate (EUR→{currency})"

    def current_attrs(data: dict) -> dict:
        cur = _current(data)
        return {
            "start": _iso(cur["start"]) if "start" in cur else None,
            "eur_mwh": cur.get("eur_mwh"),
            "source": cur.get("source"),
            **_fx_attrs(data),
        }

    def low_attrs(data: dict) -> dict:
        low = _low(data)
        return {
            "start": _iso(low["start"]) if "start" in low else None,
            "eur_mwh": low.get("eur_mwh"),
            "threshold_kwh": data.get("threshold_kwh"),
        }

    def forecast_attrs(data: dict) -> dict:
        """Forecast attributes.

        `hours` uses the compact ``{s, p}`` shape (``helper.compact_hours``) so
        the whole configured horizon stays inside Home Assistant's 16,384-byte
        state-attribute limit; the per-day ``days`` list keeps the extended stats.
        """
        fc = data.get("forecast")
        if not fc:
            return {"available": False}
        return {
            "available": True,
            "horizon_days": fc.get("horizon_days"),
            "hours_count": fc.get("hours_count"),
            "days_count": fc.get("days_count"),
            "avg_kwh": fc.get("avg_kwh"),
            "days": [
                {
                    "date": d.get("date"),
                    "min_kwh": d.get("min_kwh"),
                    "max_kwh": d.get("max_kwh"),
                    "avg_kwh": d.get("avg_kwh"),
                    "min_eur_mwh": d.get("min_eur"),
                    "min_at": _iso(d["min_at"]) if d.get("min_at") else None,
                    "max_at": _iso(d["max_at"]) if d.get("max_at") else None,
                    "hours_count": d.get("hours_count"),
                }
                for d in fc.get("days", [])
            ],
            "hours": helper.compact_hours(fc.get("hours", [])),
            **_fx_attrs(data),
        }

    def history_attrs(data: dict) -> dict:
        """Attributes for the always-on 24 h history block."""
        hist = _history(data)
        if not hist:
            return {"available": False}
        return {
            "available": True,
            "window_hours": hist.get("hours_window"),
            "hours_count": hist.get("hours_count"),
            "min_kwh": hist.get("min_kwh"),
            "max_kwh": hist.get("max_kwh"),
            "avg_kwh": hist.get("avg_kwh"),
            "min_eur_mwh": hist.get("min_eur"),
            "min_at": _iso(hist["min_at"]) if hist.get("min_at") else None,
            "max_at": _iso(hist["max_at"]) if hist.get("max_at") else None,
            "hours": _nice_hours(hist.get("hours", [])),
            **_fx_attrs(data),
        }

    def update_attrs(data: dict) -> dict:
        return {
            "area": data.get("area"),
            "hours_total": data.get("hours_total"),
            "hours_remaining": data.get("hours_remaining"),
            "last_error": coordinator.last_error,
        }

    sensors: list[EupowerpricesSensor] = [
        EupowerpricesSensor(
            coordinator,
            "current_price",
            "Current price",
            lambda d: (_current(d) or {}).get("price_kwh"),
            current_attrs,
            unit=price_unit,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:meter-electric",
        ),
        EupowerpricesSensor(
            coordinator,
            "current_price_raw",
            "Current price (raw)",
            lambda d: (_current(d) or {}).get("eur_mwh"),
            current_attrs,
            unit=UNIT_EUR_PER_MWH,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:currency-eur",
        ),
        EupowerpricesSensor(
            coordinator,
            "today_min",
            "Today minimum",
            lambda d: (_today(d) or {}).get("min_kwh"),
            lambda d: _day_attrs(_today(d)),
            unit=price_unit,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:trending-down",
        ),
        EupowerpricesSensor(
            coordinator,
            "today_max",
            "Today maximum",
            lambda d: (_today(d) or {}).get("max_kwh"),
            lambda d: _day_attrs(_today(d)),
            unit=price_unit,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:trending-up",
        ),
        EupowerpricesSensor(
            coordinator,
            "today_average",
            "Today average",
            lambda d: (_today(d) or {}).get("avg_kwh"),
            lambda d: _day_attrs(_today(d)),
            unit=price_unit,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:chart-line",
        ),
        EupowerpricesSensor(
            coordinator,
            "tomorrow_min",
            "Tomorrow minimum",
            lambda d: (_tomorrow(d) or {}).get("min_kwh"),
            lambda d: _day_attrs(_tomorrow(d)),
            unit=price_unit,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:trending-down",
        ),
        EupowerpricesSensor(
            coordinator,
            "tomorrow_max",
            "Tomorrow maximum",
            lambda d: (_tomorrow(d) or {}).get("max_kwh"),
            lambda d: _day_attrs(_tomorrow(d)),
            unit=price_unit,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:trending-up",
        ),
        EupowerpricesSensor(
            coordinator,
            "tomorrow_average",
            "Tomorrow average",
            lambda d: (_tomorrow(d) or {}).get("avg_kwh"),
            lambda d: _day_attrs(_tomorrow(d)),
            unit=price_unit,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:chart-line",
        ),
        EupowerpricesSensor(
            coordinator,
            "cheapest_window_today",
            "Cheapest window today",
            lambda d: (_today_window(d) or {}).get("avg_kwh"),
            lambda d: _window_attrs(_today_window(d), d),
            unit=price_unit,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:clock-start",
        ),
        EupowerpricesSensor(
            coordinator,
            "cheapest_window_tomorrow",
            "Cheapest window tomorrow",
            lambda d: (_tomorrow_window(d) or {}).get("avg_kwh"),
            lambda d: _window_attrs(_tomorrow_window(d), d),
            unit=price_unit,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:clock-start",
        ),
        EupowerpricesSensor(
            coordinator,
            "forecast",
            "Forecast",
            lambda d: (d.get("forecast") or {}).get("avg_kwh"),
            forecast_attrs,
            unit=price_unit,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:chart-timeline-variant",
        ),
        EupowerpricesSensor(
            coordinator,
            "history",
            "History (24 h)",
            lambda d: (_history(d) or {}).get("avg_kwh"),
            history_attrs,
            unit=price_unit,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:history",
        ),
        EupowerpricesSensor(
            coordinator,
            "next_low_price",
            "Next low price",
            lambda d: (_low(d) or {}).get("price_kwh"),
            low_attrs,
            unit=price_unit,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:lightning-bolt",
        ),
        EupowerpricesSensor(
            coordinator,
            "fx_rate",
            fx_name,
            lambda d: d.get("fx_rate"),
            lambda d: {"fx_source": d.get("fx_source")},
            unit=fx_unit,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:cash-multiple",
        ),
        EupowerpricesSensor(
            coordinator,
            "last_update",
            "Last update",
            lambda d: d.get("fetched_at"),
            update_attrs,
            device_class=SensorDeviceClass.TIMESTAMP,
            icon="mdi:update",
        ),
        EupowerpricesSensor(
            coordinator,
            "last_error",
            "Last error",
            lambda d: coordinator.last_error or "none",
            lambda d: {},
            icon="mdi:alert",
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
    ]

    async_add_entities(sensors)