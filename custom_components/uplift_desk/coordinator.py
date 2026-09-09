"""The Uplift Desk integration."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from bleak import BleakError
from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from bleak_retry_connector.bluez import clear_cache

from uplift_ble.desk_configs import DESK_CONFIGS_BY_SERVICE, DeskConfig, DeskVariant
from uplift_ble.desk_controller import DeskController
from uplift_ble.desk_enums import (
    DeskEventType,
    DeskUnit,
)
from uplift_ble.models import DiscoveredDesk as ValidatedDesk

from .models import DiscoveredDesk
from .const import CONF_FALLBACK_UNIT, FALLBACK_UNIT_NONE

_LOGGER: logging.Logger = logging.getLogger(__name__)

_EXTENDED_PRESET_VARIANTS = {
    DeskVariant.JIECANG_0x00FF,
    DeskVariant.JIECANG_0xFE60,
}


class UpliftDeskServicesError(BleakError):
    """Raised when a connected client still lacks the required GATT characteristics."""


_RECONNECT_BACKOFF_SECONDS: tuple[int, ...] = (5, 10, 20, 30)


def _parse_fallback_unit(value: str | None) -> DeskUnit | None:
    """Parse an explicit fallback without guessing for invalid values."""
    if value is None or value == FALLBACK_UNIT_NONE:
        return None
    try:
        return DeskUnit(value)
    except ValueError:
        _LOGGER.warning("Ignoring invalid fallback unit: %s", value)
        return None


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
        self._desk_lock = asyncio.Lock()
        self._fallback_unit = _parse_fallback_unit(
            config_entry.options.get(CONF_FALLBACK_UNIT)
        )
        self._desk_variant: DeskVariant | None = None
        self.height_mm: float | None = None
        self.keypad_display_units = None
        self._reconnect_task: "asyncio.Future | None" = None
        self._intentional_disconnect: bool = False

    def _resolve_ble_device(self) -> BLEDevice:
        """Resolve a fresh BLEDevice from the HA bluetooth registry, falling back to last known."""
        device = async_ble_device_from_address(self.hass, self.desk_address)
        if device is not None:
            if device is not self._desk_ble_device:
                _LOGGER.debug("Resolved fresh BLEDevice for %s (D-Bus path may have changed)", self.desk_address)
            self._desk_ble_device = device
            return device
        _LOGGER.debug("No fresh BLEDevice for %s; falling back to last known device", self.desk_address)
        return self._desk_ble_device

    def _validate_client_services(self, client) -> DeskConfig | None:
        """Validate a connected client's GATT services before building a controller.

        Returns the resolved DeskConfig, or None when the client does not
        expose the desk's service/characteristics (triggering a cache clear).
        """
        try:
            services = client.services
        except BleakError as err:
            # e.g. "Service Discovery has not been performed yet" — the
            # backend collection may also be a non-raiseable empty set.
            _LOGGER.warning(
                "Could not read services from connected client for %s: %s",
                self.desk_address,
                err,
            )
            return None

        # BleakGATTServiceCollection has no __bool__/__len__; materialize it.
        service_list = list(services)
        _LOGGER.debug(
            "Connected client for %s exposes %d service(s): %s",
            self.desk_address,
            len(service_list),
            [service.uuid for service in service_list],
        )

        for service in service_list:
            desk_config = DESK_CONFIGS_BY_SERVICE.get(service.uuid)
            if desk_config is None:
                continue

            missing = [
                char_uuid
                for char_uuid in (
                    desk_config.input_char_uuid,
                    desk_config.output_char_uuid,
                    desk_config.name_char_uuid,
                )
                if services.get_characteristic(char_uuid) is None
            ]
            if missing:
                _LOGGER.warning(
                    "Desk service %s found for %s but missing required characteristic(s): %s",
                    service.uuid,
                    self.desk_address,
                    missing,
                )
                return None

            _LOGGER.debug(
                "Validated desk service %s (%s) on connected client for %s",
                service.uuid,
                desk_config.desk_variant,
                self.desk_address,
            )
            return desk_config

        _LOGGER.warning(
            "No known desk service found on connected client for %s; services: %s",
            self.desk_address,
            [service.uuid for service in service_list],
        )
        return None

    async def _clear_cache_and_discard(self, client) -> None:
        """Best-effort: clear the stack service cache and discard the client."""
        try:
            cleared = await clear_cache(self.desk_address)
            _LOGGER.warning(
                "Cleared bleak_retry_connector service cache for %s (cleared=%s)",
                self.desk_address,
                cleared,
            )
        except Exception:
            _LOGGER.debug(
                "Could not clear service cache for %s (best-effort)",
                self.desk_address,
                exc_info=True,
            )
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                _LOGGER.debug("Could not disconnect discarded client (best-effort)", exc_info=True)
        await asyncio.sleep(1.0)  # let BlueZ/HA scanner re-advertise before retry

    async def _stop_current_controller(self) -> None:
        """Tear down the current controller (stop + disconnect) before replacement."""
        old = self._desk
        self._desk = None
        if old is None:
            return

        await self._stop_controller(old)

    async def _stop_controller(self, old: DeskController) -> None:
        """Release a controller, including one that was never published."""
        client = old.client
        try:
            await old.stop()
        except Exception:
            _LOGGER.debug(
                "Ignoring error while stopping previous controller",
                exc_info=True,
            )
        finally:
            if client is not None and client.is_connected:
                try:
                    await client.disconnect()
                except Exception:
                    _LOGGER.debug(
                        "Ignoring error while disconnecting previous client",
                        exc_info=True,
                    )

    async def _establish_and_start(self, refresh_state: bool = True) -> DeskController:
        """Serialize connection cycles and reuse a controller another caller started."""
        async with self._desk_lock:
            if self._intentional_disconnect:
                raise RuntimeError("Desk coordinator is disconnecting")
            if self.is_connected:
                return self._desk
            return await self._establish_and_start_locked(refresh_state)

    async def _establish_and_start_locked(self, refresh_state: bool) -> DeskController:
        """Run one (re)connect cycle: connect once, validate, start, refresh.

        Opens exactly one BLE connection per attempt, validates the connected
        client's GATT services before building a controller, clears the stack
        cache and retries once on an empty/partial service set, tears down the
        previous controller before replacement, and calls start() exactly once
        on the freshly built controller. A client that is connected but never
        adopted (because start() failed or the cycle was cancelled) is released
        so no BlueZ connection slot is leaked.
        """
        await self._stop_current_controller()
        for attempt in (1, 2):
            if self._intentional_disconnect:
                raise RuntimeError("Desk coordinator is disconnecting")
            device = self._resolve_ble_device()
            _LOGGER.debug("Starting (re)connect cycle for %s (attempt %d/2)", self.desk_info, attempt)
            client = await establish_connection(
                BleakClientWithServiceCache,
                device,
                device.name or self.desk_name or "Unknown",
                disconnected_callback=self._on_ble_disconnected,
                max_attempts=3,
            )
            controller = None
            try:
                if self._intentional_disconnect:
                    raise RuntimeError("Desk coordinator is disconnecting")
                desk_config = self._validate_client_services(client)
                if desk_config is None:
                    _LOGGER.warning("Incomplete GATT services (attempt %d/2); clearing cache and retrying", attempt)
                    await self._clear_cache_and_discard(client)
                    continue  # a second invalid client falls through to the error below
                controller = ValidatedDesk(
                    address=self.desk_address,
                    name=self.desk_name,
                    desk_config=desk_config,
                ).create_controller(client, fallback_unit=self._fallback_unit)
                controller.on(DeskEventType.HEIGHT, self._async_height_notify_callback)
                await controller.start()  # EXACTLY ONCE, on the fresh controller
                if self._intentional_disconnect:
                    raise RuntimeError("Desk coordinator is disconnecting")
                self._desk = controller
            except BaseException:
                # The client was connected but never adopted (start() raised, or
                # the cycle was cancelled mid-flight). Release it so we don't
                # leak a BlueZ connection slot.
                if controller is not None:
                    await self._stop_controller(controller)
                else:
                    try:
                        await client.disconnect()
                    except Exception:
                        _LOGGER.debug("Could not disconnect unadopted client (best-effort)", exc_info=True)
                raise
            self._desk_variant = desk_config.desk_variant
            _LOGGER.debug("Started notifications for %s", self.desk_info)
            if refresh_state:
                await self._refresh_state(controller)
            if self._intentional_disconnect:
                raise RuntimeError("Desk coordinator is disconnecting")
            return controller
        raise UpliftDeskServicesError(
            f"Connected client for {self.desk_address} still lacks required GATT characteristics after cache clear and one retry"
        )

    async def _refresh_state(self, controller: DeskController) -> None:
        """Best-effort refresh of units + height after a (re)connect; never fails the connect."""
        # Stay on this controller: public getters could reconnect and re-enter
        # the lifecycle lock if the link drops during the refresh.
        try:
            await self._read_desk_units(controller)
        except Exception:
            _LOGGER.warning("Failed to refresh desk units after (re)connect", exc_info=True)
        try:
            await self._read_desk_height(controller)
        except Exception:
            _LOGGER.warning("Failed to refresh desk height after (re)connect", exc_info=True)
        self.async_set_updated_data(self._desk)

    async def _get_or_establish_controller(self) -> DeskController:
        """Return the live controller, running the (re)connect cycle if needed."""
        if self._intentional_disconnect:
            raise RuntimeError("Desk coordinator is disconnecting")
        if self._desk is not None and self.is_connected:
            return self._desk
        return await self._establish_and_start()

    def _on_ble_disconnected(self, client) -> None:
        """Sync callback invoked by bleak when the link drops unexpectedly."""
        if self._intentional_disconnect:
            return
        if self._desk is None or self._desk.client is not client:
            return
        _LOGGER.warning("Desk connection lost; will attempt to reconnect")
        self.hass.async_create_task(self._async_handle_unexpected_disconnect(client))

    async def _async_handle_unexpected_disconnect(self, client) -> None:
        """Handle an unexpected link drop: tear down, then reconnect with backoff."""
        async with self._desk_lock:
            if self._intentional_disconnect:
                return
            if self._desk is None or self._desk.client is not client:
                return
            await self._stop_current_controller()
            # With self._desk now None, push unavailability to the entities.
            self.async_update_listeners()
            self._start_reconnect_loop()

    def _start_reconnect_loop(self) -> None:
        """Start the tracked reconnect task if one is not already running."""
        if self._intentional_disconnect:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return  # a reconnect is already in progress
        self._reconnect_task = self.hass.async_create_task(self._async_reconnect_loop())

    async def _async_reconnect_loop(self) -> None:
        """Retry the (re)connect cycle with capped backoff until success or unload."""
        try:
            backoff_index = 0
            while not self._intentional_disconnect:
                delay = _RECONNECT_BACKOFF_SECONDS[
                    min(backoff_index, len(_RECONNECT_BACKOFF_SECONDS) - 1)
                ]
                await asyncio.sleep(delay)
                if self._intentional_disconnect:
                    return
                try:
                    await self._establish_and_start()
                    _LOGGER.info("Reconnected to desk %s", self.desk_info)
                    return
                except Exception:
                    _LOGGER.warning(
                        "Reconnect attempt for %s failed; will retry with backoff",
                        self.desk_info,
                        exc_info=True,
                    )
                    backoff_index += 1
        finally:
            self._reconnect_task = None

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
        # Initial setup reads units+height itself (with strict error handling in
        # async_setup_entry), so skip the redundant best-effort refresh here.
        await self._establish_and_start(refresh_state=False)

    async def async_disconnect(self) -> None:
        """Tear down cleanly on unload: cancel reconnects, stop, disconnect, drop the controller."""
        self._intentional_disconnect = True
        reconnect_task = self._reconnect_task
        self._reconnect_task = None
        try:
            if reconnect_task is not None and not reconnect_task.done():
                reconnect_task.cancel()
                await asyncio.gather(reconnect_task, return_exceptions=True)
        finally:
            self._intentional_disconnect = True
            self._reconnect_task = None
            async with self._desk_lock:
                await self._stop_current_controller()

    async def async_read_desk_height(self):
        controller = await self._get_or_establish_controller()
        return await self._read_desk_height(controller)

    async def _read_desk_height(self, controller: DeskController):
        await controller.request_height_limits()
        self.height_mm = controller.height_mm
        return self.height_mm

    async def async_read_desk_units(self):
        controller = await self._get_or_establish_controller()
        return await self._read_desk_units(controller)

    async def _read_desk_units(self, controller: DeskController):
        await controller.request_units()
        retrieved_unit = controller.unit
        if retrieved_unit is None:
            retrieved_unit = self._fallback_unit
            if retrieved_unit is None:
                _LOGGER.warning("Desk did not report units and no fallback is configured")
            else:
                _LOGGER.warning("Desk did not report units; using configured %s fallback", retrieved_unit.value)
        self.keypad_display_units = retrieved_unit
        return self.keypad_display_units

    async def async_preset_1(self):
        await (await self._get_or_establish_controller()).move_to_height_preset_1()

    async def async_preset_2(self):
        await (await self._get_or_establish_controller()).move_to_height_preset_2()

    async def async_preset_3(self):
        await (await self._get_or_establish_controller()).move_to_height_preset_3()

    async def async_preset_4(self):
        await (await self._get_or_establish_controller()).move_to_height_preset_4()

    async def async_wake(self):
        await (await self._get_or_establish_controller()).wake()

    def _async_height_notify_callback(self, height_mm: int):
        self.height_mm: int =  height_mm
        _LOGGER.debug("Height notify callback received height: %d mm", self.height_mm)
        self.async_set_updated_data(self._desk)


type Uplift_Desk_DeskConfigEntry = ConfigEntry[UpliftDeskBluetoothCoordinator]  # noqa: F821
