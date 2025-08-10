"""The Uplift Desk integration."""

from __future__ import annotations

from collections.abc import Callable
import logging

from uplift_ble.desk import Desk
from uplift_ble.units import convert_mm_to_in

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
        desk_address: str,
        desk_name: str,
    ) -> None:
        """Initialize the Data Coordinator."""
        super().__init__(hass, _LOGGER, name="Uplift Desk", config_entry=config_entry)
        _LOGGER.debug("Initializing coordinator for desk %s:%s with config entry %s", desk_name, desk_address, config_entry)
        self._desk = Desk(desk_address)
        self.desk_name = desk_name
        self._desk._client = BleakClient(desk_address, timeout=BLEAK_TIMEOUT_SECONDS)
        self._desk.on_notification_height = self._async_height_notify_callback

    @property
    def desk_address(self):
        return self._desk.address

    @property
    def desk_info(self):
        return f"{self.desk_name} - {self.desk_address}"

    @property
    def is_connected(self):
        return self._desk._connected

    async def async_connect(self):
        await self._desk.connect()

    async def async_disconnect(self):
        await self._desk.disconnect()

    async def async_read_desk_height(self):
        await self.async_wake()
        last_known_height_mm = await self._desk.get_current_height()
        self.height_in = convert_mm_to_in(last_known_height_mm)
        return self.height_in

    async def async_preset_1(self):
        await self.async_wake()
        await self._desk.move_to_height_preset_1()

    async def async_preset_2(self):
        await self.async_wake()
        await self._desk.move_to_height_preset_2()

    async def async_wake(self):
        await self._desk.wake()

    def _async_height_notify_callback(self, height_mm: int):
        self.height_in: int =  convert_mm_to_in(height_mm)
        self.async_set_updated_data(self._desk)
