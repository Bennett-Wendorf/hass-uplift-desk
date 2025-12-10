"""The Uplift Desk integration."""

from __future__ import annotations

from collections.abc import Callable
import logging
import asyncio

# TODO: Revert this back to installed uplift_ble package instead of local
from .uplift_ble.desk_controller import DeskController
from .uplift_ble.desk_validator import DeskValidator
from .uplift_ble.desk_enums import DeskEventType

from homeassistant.components.bluetooth import (
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)

from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from bleak import BleakClient

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


class UpliftDeskBluetoothCoordinator(DataUpdateCoordinator):
    """Define the Update Coordinator."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: Uplift_Desk_DeskConfigEntry,
        desk_address: str,
        desk_name: str,
    ) -> None:
        """Initialize the Data Coordinator."""
        super().__init__(hass, _LOGGER, name="Uplift Desk", config_entry=config_entry)
        _LOGGER.debug("Initializing coordinator for desk %s:%s with config entry %s", desk_name, desk_address, config_entry)

        self._desk_validator = DeskValidator()
        self._discovered_desk = DiscoveredDesk(name=desk_name, address=desk_address)
        self._desk = None

    async def _get_desk_controller(self):
        if self._desk is None:          
            validated_desk: DiscoveredDesk = await self._desk_validator.validate_device(self._discovered_desk, timeout=BLEAK_TIMEOUT_SECONDS)

            client = BleakClient(validated_desk.address, timeout=BLEAK_TIMEOUT_SECONDS)
            await client.connect()
            self._desk = validated_desk.create_controller(client)
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
        if self._desk is None:
            return False
        return self._desk.client.is_connected

    async def async_connect(self):
        await (await self._get_desk_controller()).start()

    async def async_disconnect(self):
        controller = await self._get_desk_controller()
        await controller.stop()
        await controller.client.disconnect()

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
