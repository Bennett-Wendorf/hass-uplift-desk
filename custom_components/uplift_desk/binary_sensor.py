"""Platform for binary sensor integration."""
from __future__ import annotations
import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import (
    HomeAssistant,
    callback,
)
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
    """Set up the moving sensor for each desk in the config_entry."""
    _LOGGER.debug("Setting up entry for desk %s", config_entry.runtime_data.desk_info)

    async_add_entities([DeskMovingSensor(config_entry.runtime_data)])

class DeskMovingSensor(
    CoordinatorEntity[UpliftDeskBluetoothCoordinator],
    BinarySensorEntity,
):
    """Representation of a moving sensor."""

    _attr_should_poll = False

    entity_description = BinarySensorEntityDescription(
        key="desk_moving",
        translation_key="desk_moving",
        has_entity_name=True,
        device_class=BinarySensorDeviceClass.MOVING,
    )

    def __init__(self, coordinator: UpliftDeskBluetoothCoordinator) -> None:
        """Initialize the sensor."""
        _LOGGER.debug("Initializing moving sensor for desk %s", coordinator.desk_info)
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.desk_address}_{self.entity_description.key}"

    @property
    def device_info(self):
        """Return information to link this entity with the correct device."""
        return {"identifiers": {(DOMAIN, self.coordinator.desk_address)}, "name": self.coordinator.desk_name}

    @property
    def available(self) -> bool:
        """Return True if the desk is available"""
        return self.coordinator.is_connected

    @property
    def is_on(self) -> bool | None:
        """Return True if the desk is moving"""
        return self.coordinator.data.moving

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._attr_is_on = self.coordinator.data.moving
        self.async_write_ha_state()