"""The uplift desk Bluetooth integration."""

from __future__ import annotations

from collections.abc import Callable
import logging

from uplift import Desk

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
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .const import DOMAIN

type Uplift_Desk_DeskConfigEntry = ConfigEntry[UpliftDeskBluetoothCoordinator]  # noqa: F821

_LOGGER: logging.Logger = logging.getLogger(__name__)

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
        desk: Desk
    ) -> None:
        """Initialize the Data Coordinator."""
        super().__init__(hass, _LOGGER, name="Uplift Desk", config_entry=config_entry)
        _LOGGER.debug("Initializing coordinator for desk %s with config entry %s", desk, config_entry)
        self._desk = desk
        self._desk.register_callback(self._async_height_notify_callback)

    @property
    def desk_address(self):
        return self._desk.address

    @property
    def desk_name(self):
        return self._desk.name

    @property
    def desk_info(self):
        return str(self._desk)

    @property
    def is_connected(self):
        return self._desk.bleak_client is not None and\
            self._desk.bleak_client.is_connected

    async def async_connect(self):
        if not self.is_connected:
            self._desk.bleak_client = await establish_connection(
                BleakClientWithServiceCache,
                self.desk_address,
                self.desk_name or "Unknown",
                max_attempts=3
            )

    async def async_disconnect(self):
        try:
            await self._desk.bleak_client.disconnect()
        finally:
            self._desk.bleak_client = None

    async def async_start_notify(self):
        await self._desk.start_notify()

    async def async_stop_notify(self):
        await self._desk.stop_notify()

    async def async_read_desk_height(self):
        return await self._desk.read_height()

    async def async_sit(self):
        await self._desk.move_to_sitting()

    async def async_stand(self):
        await self._desk.move_to_standing()

    async def _async_height_notify_callback(self, desk: Desk):
        self.async_set_updated_data(desk)
