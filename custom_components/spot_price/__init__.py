"""Spot Price custom integration for Home Assistant."""

from __future__ import annotations

import asyncio
import logging
import os

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_START
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, PLATFORMS
from .coordinator import EupowerpricesCoordinator

_LOGGER = logging.getLogger(__name__)

# Bundled Lovelace dashboard card. The card script lives in `frontend/` and is
# served at FRONTEND_URL_BASE/spot-price-card.js; add that URL as a dashboard
# resource to use `type: custom:spot-price-card` on any dashboard.
FRONTEND_URL_BASE = "/spot_price"
FRONTEND_CARD_FILENAME = "spot-price-card.js"
FRONTEND_DIRECTORY = os.path.join(os.path.dirname(__file__), "frontend")

# Guards against double registration (async_setup + async_setup_entry can both
# run, and Home Assistant >= 2024.12's async_register_static_paths raises when
# a route already exists).
_FRONTEND_REGISTERED = False
_FRONTEND_REGISTER_LOCK = asyncio.Lock()


async def _register_frontend_card(hass: HomeAssistant) -> None:
    """Serve the bundled Lovelace card script from the HA web interface.

    Uses the modern ``hass.http.async_register_static_paths`` API (available
    since HA 2024.12 and the only option since `register_static_path` was
    removed in 2025.7), with a fallback to the legacy method on older HA.
    """
    global _FRONTEND_REGISTERED
    if _FRONTEND_REGISTERED:
        return

    if not os.path.isdir(FRONTEND_DIRECTORY):
        _LOGGER.warning(
            "Spot Price frontend directory missing: %s", FRONTEND_DIRECTORY
        )
        return

    http = getattr(hass, "http", None)
    if http is None or not hasattr(http, "app"):
        # HTTP not available yet (early bootstrap) — retry once HA has started.
        hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_START,
            lambda _event: hass.async_create_task(_register_frontend_card(hass)),
        )
        return

    async with _FRONTEND_REGISTER_LOCK:
        if _FRONTEND_REGISTERED:
            return
        try:
            if hasattr(http, "async_register_static_paths"):
                from homeassistant.components.http import StaticPathConfig

                await http.async_register_static_paths(
                    [
                        StaticPathConfig(
                            FRONTEND_URL_BASE, FRONTEND_DIRECTORY, cache_headers=True
                        )
                    ]
                )
            else:
                http.register_static_path(
                    FRONTEND_URL_BASE, FRONTEND_DIRECTORY, cache_headers=True
                )
        except (ValueError, RuntimeError) as err:
            # Route already registered (concurrent setup paths) — not fatal.
            _LOGGER.debug("Spot Price card already registered: %s", err)
        except Exception:  # noqa: BLE001 - never take down entry setup over an asset
            _LOGGER.exception("Failed to register the Spot Price dashboard card")
            return
        else:
            _FRONTEND_REGISTERED = True
            _LOGGER.debug(
                "Serving Spot Price card at %s/%s",
                FRONTEND_URL_BASE,
                FRONTEND_CARD_FILENAME,
            )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Spot Price integration.

    Config-flow entries get their platforms forwarded in async_setup_entry
    below; this hook serves the bundled dashboard card over the web interface.
    """
    await _register_frontend_card(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Spot Price from a config entry."""
    # Belt and braces: make sure the dashboard card is served even on paths
    # where async_setup was skipped (e.g. entry reloads). Idempotent.
    await _register_frontend_card(hass)

    coordinator = EupowerpricesCoordinator(hass, entry)

    # First refresh. On API failure with an existing on-disk cache the
    # coordinator returns cached data and setup succeeds; only a total
    # failure raises and HA retries setup automatically (backoff).
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.setdefault(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok