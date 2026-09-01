"""The Uplift Desk integration."""

from __future__ import annotations

from collections.abc import Callable
import logging
import asyncio

from uplift_ble.desk_controller import DeskController
from uplift_ble.desk_configs import DeskVariant
from uplift_ble.desk_validator import DeskValidator
from uplift_ble.models import DiscoveredDesk as ValidatedDesk
from uplift_ble.desk_enums import (
    DeskEventType,
    DeskUnit,
)
from uplift_ble.ble_protos import (
    BLEClientProtocol,
    BLEDeviceProtocol
)

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .const import DOMAIN, BLEAK_TIMEOUT_SECONDS
from .models import DiscoveredDesk

_LOGGER: logging.Logger = logging.getLogger(__name__)

_EXTENDED_PRESET_VARIANTS = {
    DeskVariant.JIECANG_0x00FF,
    DeskVariant.JIECANG_0xFE60,
}

def _generate_existing_client_factory(bleak_client: BleakClient) -> Callable[..., BLEClientProtocol]:
    def _existing_client_factory(
        device: BLEDeviceProtocol, timeout: float
    ) -> BLEClientProtocol:
        return bleak_client

    return _existing_client_factory

class UpliftDeskBluetoothCoordinator(DataUpdateCoordinator):
    """Define the Update Coordinator."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: Uplift_Desk_DeskConfigEntry,
        desk_ble_device: BLEDevice
    ) -> None:
        """Initialize the Data Coordinator."""
        super().__init__(hass, _LOGGER, name="Uplift Desk", config_entry=config_entry)
        _LOGGER.debug("Initializing coordinator for desk %s:%s with config entry %s", config_entry.title, desk_ble_device.address, config_entry)

        self._discovered_desk = DiscoveredDesk(name=config_entry.title, address=desk_ble_device.address)
        self._desk_ble_device = desk_ble_device
        self._desk = None
        self._desk_variant: DeskVariant | None = None
        self.height_mm: float | None = None
        self.keypad_display_units = None
        self._reconnect_task: "asyncio.Future | None" = None
        self._intentional_disconnect: bool = False

    async def _get_desk_controller(self):
        _LOGGER.debug("Getting desk controller for %s", self.desk_info)
        if self._desk is None or not self.is_connected:
            bleak_client = await establish_connection(
                BleakClientWithServiceCache,
                self._desk_ble_device, 
                self._desk_ble_device.name or self.desk_name or "Unknown",
                max_attempts=3
            )

            bleak_client_factory: Callable[..., BLEClientProtocol] = _generate_existing_client_factory(bleak_client)
            
            validated_desk: ValidatedDesk = await DeskValidator(bleak_client_factory).validate_device(self._discovered_desk, timeout=BLEAK_TIMEOUT_SECONDS)
            self._desk_variant = validated_desk.desk_config.desk_variant

            bleak_client = await establish_connection(
                BleakClientWithServiceCache,
                self._desk_ble_device, 
                self._desk_ble_device.name or self.desk_name or "Unknown",
                max_attempts=3
            )
            self._desk = validated_desk.create_controller(bleak_client)
            self._desk.on(DeskEventType.HEIGHT, self._async_height_notify_callback)

        return self._desk

    @property
    def desk_name(self):
        return self._discovered_desk.name

    @property
    def desk_address(self):
        return self._discovered_desk.address

    @property
    def desk_info(self):
        return f"{self.desk_name} - {self.desk_address}"

    @property
    def is_connected(self):
        return self._desk is not None and self._desk.client is not None and self._desk.client.is_connected

    @property
    def supports_extended_presets(self):
        return self._desk_variant in _EXTENDED_PRESET_VARIANTS

    async def async_connect(self):
        await (await self._get_desk_controller()).start()

    async def async_disconnect(self):
        controller = await self._get_desk_controller()
        await controller.stop()
        try:
            await controller.client.disconnect()
        finally:
            self._desk.client = None

    async def async_read_desk_height(self):
        controller = await self._get_desk_controller()
        await controller.request_height_limits()
        self.height_mm = controller.height_mm
        return self.height_mm

    async def async_read_desk_units(self):
        controller = await self._get_desk_controller()
        await controller.request_units()
        retrieved_unit = controller.unit
        if retrieved_unit is None:
            _LOGGER.warning("Could not retrieve units from desk, defaulting to centimeters")
            retrieved_unit = DeskUnit.CENTIMETERS
            controller._unit = DeskUnit.CENTIMETERS
        self.keypad_display_units = retrieved_unit
        return self.keypad_display_units

    async def async_preset_1(self):
        await self.async_wake()
        await (await self._get_desk_controller()).move_to_height_preset_1()

    async def async_preset_2(self):
        await self.async_wake()
        await (await self._get_desk_controller()).move_to_height_preset_2()

    async def async_preset_3(self):
        await self.async_wake()
        await (await self._get_desk_controller()).move_to_height_preset_3()

    async def async_preset_4(self):
        await self.async_wake()
        await (await self._get_desk_controller()).move_to_height_preset_4()

    async def async_wake(self):
        await (await self._get_desk_controller()).wake()

    def _async_height_notify_callback(self, height_mm: int):
        self.height_mm: int =  height_mm
        _LOGGER.debug("Height notify callback received height: %d mm", self.height_mm)
        self.async_set_updated_data(self._desk)


type Uplift_Desk_DeskConfigEntry = ConfigEntry[UpliftDeskBluetoothCoordinator]  # noqa: F821
