"""Platform for sensor integration."""
from __future__ import annotations
import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfLength
from homeassistant.core import (
    HomeAssistant,
    callback,
)
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity import generate_entity_id

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
    """Add the height sensor for each desk in the config_entry"""
    _LOGGER.debug("Setting up entry for desk %s", config_entry.runtime_data.desk_info)

    async_add_entities([DeskHeightSensor(config_entry.runtime_data)])

class DeskHeightSensor(
    CoordinatorEntity[UpliftDeskBluetoothCoordinator], 
    SensorEntity):
    """Representation of a desk height Sensor."""

    _attr_should_poll = False

    entity_description = SensorEntityDescription(
        key="desk_height",
        translation_key="desk_height",
        has_entity_name=True,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfLength.INCHES,
        suggested_display_precision=1)

    def __init__(self, coordinator: UpliftDeskBluetoothCoordinator) -> None:
        """Initialize the sensor."""
        _LOGGER.debug("Initializing height sensor for desk %s", coordinator.desk_info)
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.desk_address}_{self.entity_description.key}"

    @property
    def device_info(self):
        """Return information to link this entity with the correct device."""
        return {"identifiers": {(DOMAIN, self.coordinator.desk_address)}, "name": self.coordinator.desk_name}

    @property
    def available(self) -> bool: #TODO: Update this to actually do something
        """Return True if the desk is available"""
        return True

    @property
    def native_value(self) -> float | None:
        """Return the current height."""
        return self.coordinator.data.height

    async def async_added_to_hass(self):
        """Run when this Entity has been added to HA."""
        await super().async_added_to_hass()

        await self.coordinator.async_connect()
        await self.coordinator.async_start_notify()

        # Read the height once to ensure that the data is up to date
        await self.coordinator.async_read_desk_height()
        self.coordinator.async_set_updated_data(self.coordinator._desk)


    async def async_will_remove_from_hass(self):
        """Entity being removed from hass."""
        await self.coordinator.async_stop_notify()
        await self.coordinator.async_disconnect()

        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._attr_native_value = self.coordinator.data.height
        self.async_write_ha_state()