"""The Uplift Desk integration."""

from __future__ import annotations

from collections.abc import Callable
import logging
import asyncio

# TODO: Revert this back to installed uplift_ble package instead of local
from .uplift_ble.desk_controller import DeskController
from .uplift_ble.desk_validator import DeskValidator
from .uplift_ble.desk_enums import DeskEventType
from .uplift_ble.ble_protos import (
    BLEClientProtocol, 
    BLEDeviceProtocol
)

from homeassistant.components.bluetooth import BluetoothServiceInfoBleak

from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .const import DOMAIN, BLEAK_TIMEOUT_SECONDS
from .models import DiscoveredDesk

type Uplift_Desk_DeskConfigEntry = ConfigEntry[UpliftDeskBluetoothCoordinator]  # noqa: F821

_LOGGER: logging.Logger = logging.getLogger(__name__)

def convert_mm_to_in(millimeters: int | float) -> float:
    """
    Converts a value in millimeters to inches.
    """
    # 1 inch = 25.4 mm
    return millimeters / 25.4

def process_service_info(
    hass: HomeAssistant,
    entry: Uplift_Desk_DeskConfigEntry,
    service_info: BluetoothServiceInfoBleak,
) -> SensorUpdate:
    """Process a BluetoothServiceInfoBleak, running side effects and returning sensor data."""
    coordinator = entry.runtime_data
    data = coordinator.device_data
    update = data.update(service_info)
    if not coordinator.model_info and (device_type := data.device_type):
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_DEVICE_TYPE: device_type}
        )
        coordinator.set_model_info(device_type)
    if update.events and hass.state is CoreState.running:
        # Do not fire events on data restore
        address = service_info.device.address
        for event in update.events.values():
            key = event.device_key.key
            signal = format_event_dispatcher_name(address, key)
            async_dispatcher_send(hass, signal)

    return update


def format_event_dispatcher_name(address: str, key: str) -> str:
    """Format an event dispatcher name."""
    return f"{DOMAIN}_{address}_{key}"

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

    async def _get_desk_controller(self):
        _LOGGER.debug("Getting desk controller for %s", self.desk_info)
        _LOGGER.debug("self._desk is %s", self._desk)
        _LOGGER.debug("self.is_connected is %s", self.is_connected)
        if self._desk is None or not self.is_connected:
            bleak_client = await establish_connection(
                BleakClientWithServiceCache, 
                self._desk_ble_device, 
                self._desk_ble_device.name or self.desk_name or "Unknown",
                max_attempts=3
            )
            _LOGGER.debug("bleak_client is %s", bleak_client)

            bleak_client_factory: Callable[..., BLEClientProtocol] = _generate_existing_client_factory(bleak_client)
            _LOGGER.debug("bleak_client_factory is %s", bleak_client_factory)
            
            validated_desk: DiscoveredDesk = await DeskValidator(bleak_client_factory).validate_device(self._discovered_desk)
            _LOGGER.debug("validated_desk is %s", validated_desk)

            bleak_client = await establish_connection(
                BleakClientWithServiceCache, 
                self._desk_ble_device, 
                self._desk_ble_device.name or self.desk_name or "Unknown",
                max_attempts=3
            )
            _LOGGER.debug("Re-established bleak_client is %s", bleak_client)
            self._desk = validated_desk.create_controller(bleak_client)
            _LOGGER.debug("self._desk is %s", self._desk)
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
        # await self.async_wake()
        await (await self._get_desk_controller()).request_height_limits()
        self.height_in = convert_mm_to_in((await self._get_desk_controller()).height_mm)
        return self.height_in

    async def async_preset_1(self):
        await self.async_wake()
        await (await self._get_desk_controller()).move_to_height_preset_1()

    async def async_preset_2(self):
        await self.async_wake()
        await (await self._get_desk_controller()).move_to_height_preset_2()

    async def async_wake(self):
        await (await self._get_desk_controller()).wake()

    def _async_height_notify_callback(self, height_mm: int):
        _LOGGER.debug("Height notify callback received height: %d mm", height_mm)
        self.height_in: int =  convert_mm_to_in(height_mm)
        self.async_set_updated_data(self._desk)
