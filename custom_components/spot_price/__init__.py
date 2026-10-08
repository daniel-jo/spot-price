"""Spot Price custom integration for Home Assistant."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, PLATFORMS
from .coordinator import EupowerpricesCoordinator


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Spot Price integration.

    Every entity comes from a config-flow entry, which async_setup_entry below
    forwards to the sensor platform; this hook only needs to return True.
    """
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Spot Price from a config entry."""
    coordinator = EupowerpricesCoordinator(hass, entry)

    # First refresh. With an existing on-disk cache the coordinator serves the
    # cached view immediately (no network) and the first *network* fetch waits
    # for the next 13:30 market-time anchor; without any data it fetches right
    # away. Only a total failure (no data anywhere) raises and HA retries setup
    # automatically (backoff).
    await coordinator.async_config_entry_first_refresh()
    coordinator.start_schedule()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None:
        coordinator.cancel_schedule()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.setdefault(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok