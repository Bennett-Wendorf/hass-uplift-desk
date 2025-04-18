"""The Uplift Desk integration."""

from __future__ import annotations
import logging

from .const import DOMAIN

from uplift import Desk

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import (
    UpliftDeskBluetoothCoordinator,
    Uplift_Desk_DeskConfigEntry,
)

_PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON]

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: Uplift_Desk_DeskConfigEntry) -> bool:
    """Set up Uplift Desk from a config entry."""

    desk: Desk = Desk(entry.data["address"], entry.title)
    coordinator: UpliftDeskBluetoothCoordinator = UpliftDeskBluetoothCoordinator(hass, entry, desk)
    entry.runtime_data = coordinator

    await coordinator.async_connect()
    await coordinator.async_start_notify()

    await coordinator.async_read_desk_height()
    coordinator.async_set_updated_data(coordinator._desk)

    _LOGGER.debug("Initializing Uplift Desk for desk %s: %s", entry.title, entry.data["address"])

    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)

    return True

async def async_unload_entry(hass: HomeAssistant, entry: Uplift_Desk_DeskConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: UpliftDeskBluetoothCoordinator = entry.runtime_data

    await coordinator.async_stop_notify()
    await coordinator.async_disconnect()

    return await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)
