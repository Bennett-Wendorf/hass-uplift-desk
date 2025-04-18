"""Platform for button integration."""
from __future__ import annotations
import logging

from homeassistant.components.button import (
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import Uplift_Desk_DeskConfigEntry
from .coordinator import UpliftDeskBluetoothCoordinator
from .const import DOMAIN

from uplift import Desk

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: Uplift_Desk_DeskConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the button platform."""
    _LOGGER.debug("Setting up button platform for desk %s", config_entry.runtime_data.desk_info)
    async_add_entities([
        UpliftDeskSitButton(config_entry.runtime_data),
        UpliftDeskStandButton(config_entry.runtime_data),
    ])

class UpliftDeskSitButton(
    CoordinatorEntity[UpliftDeskBluetoothCoordinator],
    ButtonEntity):
    """Representation of a sit button."""

    entity_description = ButtonEntityDescription(
        key="desk_sit",
        translation_key="desk_sit",
        has_entity_name=True,
    )

    def __init__(self, coordinator: UpliftDeskBluetoothCoordinator) -> None:
        """Initialize the button."""
        _LOGGER.debug("Initializing sit button for desk %s", coordinator.desk_info)
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.desk_address}_{self.entity_description.key}"

    @property
    def device_info(self):
        """Return information to link this entity with the correct device."""
        return {"identifiers": {(DOMAIN, self.coordinator.desk_address)}, "name": self.coordinator.desk_name}

    async def async_press(self) -> None:
        """Handle the button press."""
        await self.coordinator.async_sit()

class UpliftDeskStandButton(
    CoordinatorEntity[UpliftDeskBluetoothCoordinator],
    ButtonEntity):
    """Representation of a stand button."""

    entity_description = ButtonEntityDescription(
        key="desk_stand",
        translation_key="desk_stand",
        has_entity_name=True,
    )

    def __init__(self, coordinator: UpliftDeskBluetoothCoordinator) -> None:
        """Initialize the button."""
        _LOGGER.debug("Initializing stand button for desk %s", coordinator.desk_info)
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.desk_address}_{self.entity_description.key}"

    @property
    def device_info(self):
        """Return information to link this entity with the correct device."""
        return {"identifiers": {(DOMAIN, self.coordinator.desk_address)}, "name": self.coordinator.desk_name}

    async def async_press(self) -> None:
        """Handle the button press."""
        await self.coordinator.async_stand()