"""The Uplift Desk integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import functools
import logging
import time

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from bleak import BleakError
from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from bleak_retry_connector.bluez import clear_cache

from uplift_ble.desk_configs import DESK_CONFIGS_BY_SERVICE, DeskConfig, DeskVariant
from uplift_ble.desk_controller import DeskController, command_writer
from uplift_ble.desk_enums import (
    DeskEventType,
    DeskLockStatus,
    DeskUnit,
)
from uplift_ble.models import DiscoveredDesk as ValidatedDesk

from .models import DiscoveredDesk
from .const import (
    BASE_PRESET_SLOTS,
    CONF_FALLBACK_UNIT,
    CONF_QUERY_ON_CONNECT,
    DEFAULT_HEIGHT_LIMIT_MAX_MM,
    DEFAULT_HEIGHT_LIMIT_MIN_MM,
    EXTENDED_PRESET_SLOTS,
    FALLBACK_UNIT_NONE,
    HEIGHT_SETPOINT_ARRIVAL_TOLERANCE_MM,
    HEIGHT_SETPOINT_INTERRUPTION_EPSILON_MM,
    PRESET_HEIGHT_LIMIT_TOLERANCE_MM,
    PRESET_REPORT_DELAY_SECONDS,
    PRESET_SAVE_SETTLE_SECONDS,
)

_LOGGER: logging.Logger = logging.getLogger(__name__)

_EXTENDED_PRESET_VARIANTS = {
    DeskVariant.JIECANG_0x00FF,
    DeskVariant.JIECANG_0xFE60,
    # Some V3 adapters advertise 0x00FF but expose 0xFF00 after connecting.
    DeskVariant.JIECANG_0xFF00,
}


class UpliftDeskServicesError(BleakError):
    """Raised when a connected client still lacks the required GATT characteristics."""


class UpliftDeskMoveInFlightError(HomeAssistantError):
    """Raised when a move command is already in flight for the desk."""


class UpliftDeskLockedError(HomeAssistantError):
    """Raised when the desk is locked and cannot be moved."""


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


def decode_preset_height_mm(
    raw: int, min_mm: int, max_mm: int, unit: DeskUnit | None = None
) -> float | None:
    """Decode a raw preset report into millimeters, or None when unsure.

    Preset reports carry no unit. A tested inch-mode desk reports tenths of
    its display unit (like height), but uplift-ble warns firmware varies, so
    both readings are considered: millimeters (identical to tenths of a
    centimeter) and tenths of an inch. A desk's height range spans well under
    the 2.54x ratio between them, so normally only one lands within the
    desk's limits. When both do (wide fallback limits), the desk's display
    unit breaks the tie; without one the preset stays unknown.
    """
    as_mm = float(raw)
    as_tenths_inch = raw / 10 * 25.4
    plausible = [
        height_mm
        for height_mm in (as_mm, as_tenths_inch)
        if min_mm - PRESET_HEIGHT_LIMIT_TOLERANCE_MM
        <= height_mm
        <= max_mm + PRESET_HEIGHT_LIMIT_TOLERANCE_MM
    ]
    if len(plausible) == 1:
        return plausible[0]
    if len(plausible) == 2 and unit is not None:
        return as_tenths_inch if unit is DeskUnit.INCHES else as_mm
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
        self._motion_write_lock = asyncio.Lock()
        self._motion_generation = 0
        self._fallback_unit = _parse_fallback_unit(
            config_entry.options.get(CONF_FALLBACK_UNIT)
        )
        self._query_on_connect = config_entry.options.get(CONF_QUERY_ON_CONNECT, True)
        self._desk_variant: DeskVariant | None = None
        self.height_mm: float | None = None
        self._height_setpoint_mm: int | None = None
        self.height_limit_min_mm: int | None = None
        self.height_limit_max_mm: int | None = None
        self._move_in_flight: bool = False
        # Monotonic time of the last observed height change or preset recall.
        self._last_motion_at: float | None = None
        # Saved slots whose new height the desk has not reported back yet.
        self._unconfirmed_preset_slots: set[int] = set()
        # Display unit reported when each preset report arrived, if known.
        self._preset_report_units: dict[int, DeskUnit | None] = {}
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
        """Clear the selected backend's cache before discarding its client."""
        cleared = False
        try:
            clear_client_cache = getattr(client, "clear_cache", None)
            if callable(clear_client_cache):
                try:
                    # ESPHome clears its on-device cache through this API and
                    # requires the BLE connection to still be active.
                    cleared = await clear_client_cache()
                except Exception:
                    _LOGGER.debug(
                        "Could not clear the client's service cache for %s",
                        self.desk_address,
                        exc_info=True,
                    )
            if not cleared:
                try:
                    # Retain recovery for local BlueZ clients without the
                    # backend extension. This does not address ESP32 caches.
                    cleared = await clear_cache(self.desk_address)
                except Exception:
                    _LOGGER.debug(
                        "Could not clear BlueZ cache for %s (best-effort)",
                        self.desk_address,
                        exc_info=True,
                    )
            _LOGGER.warning(
                "Service cache clear for %s (cleared=%s)",
                self.desk_address,
                cleared,
            )
        finally:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    _LOGGER.debug("Could not disconnect discarded client (best-effort)", exc_info=True)
        await asyncio.sleep(1.0)  # allow the selected scanner to rediscover it

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
        client's GATT services before building a controller, clears the selected
        backend's cache and retries once on an empty/partial service set or a
        notification-subscription timeout, tears down the
        previous controller before replacement, and calls start() exactly once
        on the freshly built controller. A client that is connected but never
        adopted (because start() failed or the cycle was cancelled) is released
        so no Bluetooth connection slot is leaked.
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
                controller.on(
                    DeskEventType.HEIGHT_LIMITS_CONFIGURATION,
                    self._async_height_limits_configuration_callback,
                )
                controller.on(
                    DeskEventType.HEIGHT_LIMIT_MAX,
                    self._async_height_limit_max_callback,
                )
                controller.on(
                    DeskEventType.HEIGHT_LIMIT_MIN,
                    self._async_height_limit_min_callback,
                )
                # All four slots: this connection's variant is recorded only
                # after start(), and a report for an unoffered slot is harmless.
                for slot in BASE_PRESET_SLOTS + EXTENDED_PRESET_SLOTS:
                    controller.on(
                        DeskEventType[f"HEIGHT_PRESET_{slot}"],
                        functools.partial(self._async_height_preset_callback, controller, slot),
                    )
                # Preset decoding uses the display unit to break ties.
                controller.on(DeskEventType.UNIT, self._async_unit_callback)
                try:
                    await controller.start()  # once per fresh controller
                except TimeoutError:
                    if attempt == 2:
                        raise
                    _LOGGER.warning(
                        "Desk notification startup timed out; clearing its service "
                        "cache before one reconnect attempt",
                        exc_info=True,
                    )
                    await self._clear_cache_and_discard(client)
                    await self._stop_controller(controller)
                    continue
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
            # The link is gone, so the commanded target is no longer being
            # tracked; clear it so the same push below delivers both the
            # unavailability and the setpoint clear.
            self._height_setpoint_mm = None
            _LOGGER.debug(
                "Cleared height setpoint for desk %s on unexpected disconnect",
                self.desk_info,
            )
            # The cached position is invalid once the link is gone: the first
            # height notification after a (re)connect must not reconcile the
            # setpoint against a stale pre-disconnect height.
            self.height_mm = None
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

    @property
    def preset_slots(self) -> tuple[int, ...]:
        """The preset slots this desk can save and report."""
        if self.supports_extended_presets:
            return BASE_PRESET_SLOTS + EXTENDED_PRESET_SLOTS
        return BASE_PRESET_SLOTS

    @property
    def height_setpoint_mm(self) -> int | None:
        """The height (mm) the desk was last commanded to move to, or None."""
        return self._height_setpoint_mm

    @property
    def effective_height_limits_mm(self) -> tuple[int, int]:
        """Resolve the effective min/max (mm) from the desk's reported limits.

        A reported limit wins over the fallback default; partial knowledge
        is fine (the unreported side stays at its fallback). A misreported
        inverted range (min >= max) falls back to the full default range.
        """
        min_mm = self.height_limit_min_mm
        if min_mm is None:
            min_mm = DEFAULT_HEIGHT_LIMIT_MIN_MM
        max_mm = self.height_limit_max_mm
        if max_mm is None:
            max_mm = DEFAULT_HEIGHT_LIMIT_MAX_MM
        if min_mm >= max_mm:
            _LOGGER.debug(
                "Desk %s reported inverted height limits (min %d >= max %d); using fallback range",
                self.desk_info,
                min_mm,
                max_mm,
            )
            return DEFAULT_HEIGHT_LIMIT_MIN_MM, DEFAULT_HEIGHT_LIMIT_MAX_MM
        return min_mm, max_mm

    def height_preset_mm(self, slot: int) -> float | None:
        """The height (mm) stored in a preset slot, as last reported by the desk.

        Decoded on every read from the live controller's raw report and the
        current limits and unit, so the result does not depend on whether the
        preset, unit, or limit reports arrived first, and never outlives a
        connection. Unknown after a save until the desk reports the new value.
        """
        if self._desk is None or slot in self._unconfirmed_preset_slots:
            return None
        raw = getattr(self._desk, f"height_preset_{slot}")
        if raw is None:
            return None
        return decode_preset_height_mm(
            raw,
            *self.effective_height_limits_mm,
            unit=(
                self._preset_report_units.get(slot)
                or self._desk.unit
                or self._fallback_unit
            ),
        )

    @property
    def desk_recently_moved(self) -> bool:
        """Whether the desk may still be moving from a command, recall, or keypad."""
        if self._move_in_flight or self._height_setpoint_mm is not None:
            return True
        return (
            self._last_motion_at is not None
            and time.monotonic() - self._last_motion_at < PRESET_SAVE_SETTLE_SECONDS
        )

    async def async_connect(self):
        # Initial setup reads units+height itself (with strict error handling in
        # async_setup_entry), so skip the redundant best-effort refresh here.
        await self._establish_and_start(refresh_state=False)

    async def async_disconnect(self) -> None:
        """Tear down cleanly on unload: cancel reconnects, stop, disconnect, drop the controller."""
        self._intentional_disconnect = True
        self._motion_generation += 1
        # Defensive: entity platforms are already unloaded before
        # async_unload_entry calls this, so no listener push is needed.
        self._height_setpoint_mm = None
        _LOGGER.debug(
            "Cleared height setpoint for desk %s on disconnect",
            self.desk_info,
        )
        # The cached position is invalid once the link is gone.
        self.height_mm = None
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
        if self._query_on_connect:
            await controller.request_height_limits()
        self.height_mm = controller.height_mm
        return self.height_mm

    async def async_read_desk_units(self):
        controller = await self._get_or_establish_controller()
        return await self._read_desk_units(controller)

    async def _read_desk_units(self, controller: DeskController):
        if self._query_on_connect:
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
        await self._async_recall_preset(1)

    async def async_preset_2(self):
        await self._async_recall_preset(2)

    async def async_preset_3(self):
        await self._async_recall_preset(3)

    async def async_preset_4(self):
        await self._async_recall_preset(4)

    async def _async_recall_preset(self, slot: int) -> None:
        """Discard a pending recall if Stop or unload supersedes it."""
        generation = self._motion_generation
        controller = await self._get_or_establish_controller()
        if await self._async_write_motion(
            controller, f"move_to_height_preset_{slot}", generation
        ):
            self._last_motion_at = time.monotonic()

    async def async_save_preset(self, slot: int) -> None:
        """Store the desk's current height in a preset slot.

        The desk saves wherever it is when the command lands, so the save is
        refused while the desk may still be moving. It shares the motion write
        path so Stop or unload cancels a save that is still waking the desk.
        """
        if slot not in self.preset_slots:
            raise ValueError(f"Preset {slot} cannot be saved")
        self._raise_unless_desk_settled(slot)
        generation = self._motion_generation
        controller = await self._get_or_establish_controller()
        already_unconfirmed = slot in self._unconfirmed_preset_slots

        def before_write() -> None:
            # Re-check under the write lock: motion may start while the desk
            # wakes. Mark the slot before sending so a report that arrives
            # while the write is in flight confirms it rather than being lost.
            self._raise_unless_desk_settled(slot)
            self._unconfirmed_preset_slots.add(slot)

        try:
            written = await self._async_write_motion(
                controller,
                f"save_height_preset_{slot}",
                generation,
                before_write=before_write,
            )
        except BaseException:
            if not already_unconfirmed:
                self._unconfirmed_preset_slots.discard(slot)
            raise
        if not written:
            raise HomeAssistantError(
                f"Saving preset {slot} for desk {self.desk_info} was cancelled"
            )
        _LOGGER.debug("Saved current height as preset %d on desk %s", slot, self.desk_info)
        self.async_update_listeners()
        if self._query_on_connect:
            await self._async_request_preset_report(controller, generation)

    def _raise_unless_desk_settled(self, slot: int) -> None:
        desk = self._desk
        if desk is not None and desk.height_raw is not None and desk.height_mm is None:
            # Height reports in unknown units never reach the coordinator, so
            # keypad movement would go unnoticed.
            raise HomeAssistantError(
                f"Desk {self.desk_info} reports its height in unknown units, so movement "
                f"cannot be tracked; set a fallback height unit before saving preset {slot}"
            )
        if self.desk_recently_moved:
            raise HomeAssistantError(
                f"Desk {self.desk_info} is moving; wait for it to stop before saving preset {slot}"
            )

    async def _async_request_preset_report(
        self, controller: DeskController, generation: int
    ) -> None:
        """Best-effort: ask the desk to re-report its presets after a save."""
        try:
            # On hardware, a bare 0x07 sent straight after the save drew no
            # reply. Pause, then repeat the wake + 0x07 sequence the desk
            # answers with its presets on connect.
            await asyncio.sleep(PRESET_REPORT_DELAY_SECONDS)
            if (
                generation != self._motion_generation
                or self._intentional_disconnect
                or controller is not self._desk
                or not self.is_connected
            ):
                return
            await controller.request_height_limits()
        except (BleakError, TimeoutError):
            _LOGGER.warning(
                "Saved a preset on desk %s but could not request its updated presets",
                self.desk_info,
                exc_info=True,
            )

    async def _async_write_motion(
        self,
        controller: DeskController,
        name: str,
        generation: int,
        *args,
        before_write: Callable[[], None] | None = None,
    ) -> bool:
        """Write motion only if it has not been superseded by Stop or unload.

        ``before_write`` runs under the write lock just before the packet and
        may raise to abort the write.
        """
        # Preserve 0.7.0's wake sequence outside the final-write lock, so a
        # slow wake or reconnect cannot hold Stop behind a later motion write.
        if controller.requires_wake:
            for _ in range(3):
                if generation != self._motion_generation or self._intentional_disconnect:
                    return False
                await controller.wake()
                await asyncio.sleep(0.1)
        async with self._motion_write_lock:
            if generation != self._motion_generation or self._intentional_disconnect:
                return False
            if before_write is not None:
                before_write()
            await self._async_command_only(controller, name, *args)
            return True

    @staticmethod
    async def _async_command_only(controller: DeskController, name: str, *args) -> None:
        """Keep the library's packet definition while omitting wake and waits."""
        definition = getattr(DeskController, name).__wrapped__
        await command_writer(skip_wake=True)(definition)(controller, *args)

    async def async_stop_movement(self) -> None:
        """Cancel pending motion and send Stop only on the existing connection."""
        self._motion_generation += 1
        self._height_setpoint_mm = None
        self.async_update_listeners()
        controller = self._desk if self.is_connected else None
        async with self._motion_write_lock:
            if (
                self._intentional_disconnect
                or controller is None
                or controller is not self._desk
                or not self.is_connected
            ):
                raise HomeAssistantError(
                    "Desk connection changed or is unavailable; pending motion was cancelled, "
                    "but no Stop packet was sent. Use the desk keypad."
                )
            try:
                await self._async_command_only(controller, "stop_movement")
            except (BleakError, TimeoutError) as err:
                raise HomeAssistantError(
                    "Could not send Stop to the desk. Use the desk keypad."
                ) from err

    async def async_wake(self):
        await (await self._get_or_establish_controller()).wake()

    async def async_move_to_height(self, height_mm: int | float) -> None:
        """Command the desk to move to a specific height (mm).

        At most one move command may be in flight (coalescing, below);
        a second concurrent set raises UpliftDeskMoveInFlightError. The
        in-flight window covers the command write plus any (re)connect
        cycle needed to reach a disconnected desk, so it can last well
        longer than the BLE write itself.
        Raises UpliftDeskLockedError if the desk last reported LOCKED.

        The commanded target is stored as the height setpoint *before* the
        write (optimistic), so the Height Setpoint entity shows the target
        immediately - even while a (re)connect cycle is still in progress -
        and height notifications reconcile it away as the desk arrives or is
        interrupted. Stop or unload cancels a pending write and clears its
        target. Otherwise, a failed command restores the previous setpoint so
        a failed set never leaves a stale target.
        """
        # Reject (not queue) a second concurrent set: the check-and-set below
        # has no await between them, so it is race-free on the HA event loop.
        if self._move_in_flight:
            raise UpliftDeskMoveInFlightError(
                f"A move command for desk {self.desk_info} is in flight, or the desk is "
                "reconnecting; try again shortly"
            )
        self._move_in_flight = True
        generation = self._motion_generation
        target_mm = int(round(height_mm))
        previous_setpoint_mm = self._height_setpoint_mm
        # Optimistic: show the commanded target immediately, even while a
        # (re)connect cycle is still in progress.
        self._height_setpoint_mm = target_mm
        self.async_set_updated_data(self._desk)
        try:
            controller = await self._get_or_establish_controller()
            if controller.lock_status is DeskLockStatus.LOCKED:
                raise UpliftDeskLockedError(
                    f"Desk {self.desk_info} is locked; unlock it before moving"
                )
            # A lock_status of None means the desk has not reported one since
            # connect; proceed — the firmware rejects a move if truly locked.
            if not await self._async_write_motion(
                controller, "move_to_specified_height", generation, target_mm
            ):
                return
            _LOGGER.debug(
                "Commanded desk %s to move to %d mm", self.desk_info, target_mm
            )
            if (
                self.height_mm is not None
                and abs(self.height_mm - target_mm)
                <= HEIGHT_SETPOINT_ARRIVAL_TOLERANCE_MM
            ):
                # Desk already at (within tolerance of) the commanded height:
                # nothing left to track.
                self._height_setpoint_mm = None
                self.async_update_listeners()
        except Exception:
            # Command failed (write error, locked, reconnect failure): restore
            # the previous setpoint so a failed set never leaves a stale target.
            # A concurrent Stop or unload has already cancelled this target;
            # do not resurrect an older one when the pending command fails.
            if generation == self._motion_generation and not self._intentional_disconnect:
                self._height_setpoint_mm = previous_setpoint_mm
                self.async_update_listeners()
            raise
        finally:
            self._move_in_flight = False

    def _async_height_notify_callback(self, height_mm: float) -> None:
        previous_height_mm = self.height_mm
        self.height_mm = height_mm
        if previous_height_mm is not None and height_mm != previous_height_mm:
            self._last_motion_at = time.monotonic()
        self._reconcile_height_setpoint(previous_height_mm, height_mm)
        _LOGGER.debug("Height notify callback received height: %s mm", height_mm)
        self.async_set_updated_data(self._desk)

    def _reconcile_height_setpoint(
        self, previous_height_mm: float | None, height_mm: float
    ) -> None:
        """Reconcile the active setpoint against a fresh height report.

        Clears the setpoint on arrival (the reported height is within the
        arrival tolerance of the target, or the desk crossed the target
        between two notifications) or on interruption (the desk moved away
        from the target by more than the jitter epsilon - keypad, preset,
        or any other control).
        """
        setpoint_mm = self._height_setpoint_mm
        if setpoint_mm is None:
            return
        # Arrival: reported height within tolerance of the target.
        if abs(height_mm - setpoint_mm) <= HEIGHT_SETPOINT_ARRIVAL_TOLERANCE_MM:
            self._height_setpoint_mm = None
            _LOGGER.debug(
                "Desk %s arrived at setpoint %d mm (reported %s mm)",
                self.desk_info,
                setpoint_mm,
                height_mm,
            )
            return
        if previous_height_mm is None:
            return
        # Arrival: the desk crossed the target between two notifications.
        if (previous_height_mm - setpoint_mm) * (height_mm - setpoint_mm) < 0:
            self._height_setpoint_mm = None
            _LOGGER.debug(
                "Desk %s crossed setpoint %d mm (%s -> %s mm)",
                self.desk_info,
                setpoint_mm,
                previous_height_mm,
                height_mm,
            )
            return
        # Interruption: desk moved away from the target (keypad/preset/etc.).
        delta = height_mm - previous_height_mm
        to_target = setpoint_mm - previous_height_mm
        if (
            abs(delta) > HEIGHT_SETPOINT_INTERRUPTION_EPSILON_MM
            and delta * to_target < 0
        ):
            self._height_setpoint_mm = None
            _LOGGER.debug(
                "Desk %s moved away from setpoint %d mm (%s -> %s mm)",
                self.desk_info,
                setpoint_mm,
                previous_height_mm,
                height_mm,
            )

    def _async_height_limits_configuration_callback(self, max_mm: int, min_mm: int) -> None:
        self.height_limit_max_mm = max_mm
        self.height_limit_min_mm = min_mm
        _LOGGER.debug(
            "Height limits configuration callback received max: %d mm, min: %d mm",
            max_mm,
            min_mm,
        )
        self.async_set_updated_data(self._desk)

    def _async_height_limit_max_callback(self, max_mm: int) -> None:
        self.height_limit_max_mm = max_mm
        _LOGGER.debug("Height limit max callback received max: %d mm", max_mm)
        self.async_set_updated_data(self._desk)

    def _async_height_limit_min_callback(self, min_mm: int) -> None:
        self.height_limit_min_mm = min_mm
        _LOGGER.debug("Height limit min callback received min: %d mm", min_mm)
        self.async_set_updated_data(self._desk)

    def _async_unit_callback(self, unit: DeskUnit) -> None:
        _LOGGER.debug("Unit callback received unit: %s", unit)
        self.async_set_updated_data(self._desk)

    def _async_height_preset_callback(
        self, controller: DeskController, slot: int, raw: int
    ) -> None:
        self._unconfirmed_preset_slots.discard(slot)
        # A later display-unit change does not convert the stored preset, so
        # decode this report with the unit in effect when it arrived.
        self._preset_report_units[slot] = controller.unit
        _LOGGER.debug(
            "Height preset %d callback received raw value %d (decoded: %s mm)",
            slot,
            raw,
            self.height_preset_mm(slot),
        )
        self.async_set_updated_data(self._desk)


type Uplift_Desk_DeskConfigEntry = ConfigEntry[UpliftDeskBluetoothCoordinator]  # noqa: F821
