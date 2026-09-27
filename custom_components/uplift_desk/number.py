"""Platform for number integration."""
from __future__ import annotations
import logging

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import UnitOfLength
from homeassistant.core import (
    HomeAssistant,
    callback,
)
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import Uplift_Desk_DeskConfigEntry
from .coordinator import UpliftDeskBluetoothCoordinator
from .const import (
    DEFAULT_HEIGHT_LIMIT_MAX_MM,
    DEFAULT_HEIGHT_LIMIT_MIN_MM,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: Uplift_Desk_DeskConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the height setpoint number for each desk in the config_entry"""
    _LOGGER.debug("Setting up entry for desk %s", config_entry.runtime_data.desk_info)

    async_add_entities([DeskHeightSetpointNumber(config_entry.runtime_data)])

class DeskHeightSetpointNumber(
    CoordinatorEntity[UpliftDeskBluetoothCoordinator], 
    NumberEntity):
    """Representation of a desk height setpoint number.

    Shows the commanded *target* height (the setpoint tracked by the
    coordinator): unknown when the desk is at rest or no move is in
    progress, and the commanded height while the desk is moving toward it.
    The desk's live position is reported by the separate Height sensor.
    """

    _attr_should_poll = False

    def __init__(self, coordinator: UpliftDeskBluetoothCoordinator) -> None:
        """Initialize the number."""
        _LOGGER.debug("Initializing height setpoint number for desk %s", coordinator.desk_info)
        super().__init__(coordinator)
        self.entity_description = NumberEntityDescription(
            key="desk_height_setpoint",
            translation_key="desk_height_setpoint",
            has_entity_name=True,
            device_class=NumberDeviceClass.DISTANCE,
            native_unit_of_measurement=UnitOfLength.MILLIMETERS,
            native_step=1,
            mode=NumberMode.BOX,
        )
        self._attr_unique_id = f"{coordinator.desk_address}_{self.entity_description.key}"
        self._attr_native_min_value, self._attr_native_max_value = self._effective_limits()
        # No setpoint is active at setup time: the entity starts unknown.
        self._attr_native_value = None

    @property
    def device_info(self):
        """Return information to link this entity with the correct device."""
        return {"identifiers": {(DOMAIN, self.coordinator.desk_address)}, "name": self.coordinator.desk_name}

    @property
    def available(self) -> bool:
        """Return True if the desk is available"""
        return self.coordinator.is_connected

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._attr_native_value = self.coordinator.height_setpoint_mm
        self._attr_native_min_value, self._attr_native_max_value = self._effective_limits()
        self.async_write_ha_state()

    def _effective_limits(self) -> tuple[int, int]:
        """Resolve the effective min/max (mm) from the desk's reported limits.

        A reported limit wins over the fallback default; partial knowledge
        is fine (the unreported side stays at its fallback). A misreported
        inverted range (min >= max) falls back to the full default range.
        """
        min_mm = self.coordinator.height_limit_min_mm
        if min_mm is None:
            min_mm = DEFAULT_HEIGHT_LIMIT_MIN_MM
        max_mm = self.coordinator.height_limit_max_mm
        if max_mm is None:
            max_mm = DEFAULT_HEIGHT_LIMIT_MAX_MM
        if min_mm >= max_mm:
            _LOGGER.debug(
                "Desk %s reported inverted height limits (min %d >= max %d); using fallback range",
                self.coordinator.desk_info,
                min_mm,
                max_mm,
            )
            return DEFAULT_HEIGHT_LIMIT_MIN_MM, DEFAULT_HEIGHT_LIMIT_MAX_MM
        return min_mm, max_mm

    async def async_set_native_value(self, value: float) -> None:
        """Command the desk to move to the given height (mm)."""
        await self.coordinator.async_move_to_height(value)
