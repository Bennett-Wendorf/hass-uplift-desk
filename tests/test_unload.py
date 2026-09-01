"""Regression tests for UPLIFT config-entry teardown."""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock


def _install_module(name: str, *, package: bool = False, **attributes: object) -> None:
    module = ModuleType(name)
    if package:
        module.__path__ = []
    for attribute, value in attributes.items():
        setattr(module, attribute, value)
    sys.modules[name] = module


class _DataUpdateCoordinator:
    pass


_platform = SimpleNamespace(SENSOR="sensor", BUTTON="button")
_core_state = SimpleNamespace(running=object())
_desk_variant = SimpleNamespace(
    JIECANG_0x00FF=object(), JIECANG_0xFE60=object()
)
_desk_event_type = SimpleNamespace(HEIGHT=object())
_desk_unit = SimpleNamespace(CENTIMETERS=object())

_install_module("uplift_ble", package=True)
_install_module("uplift_ble.desk_controller", DeskController=object)
_install_module("uplift_ble.desk_configs", DeskVariant=_desk_variant)
_install_module("uplift_ble.desk_validator", DeskValidator=object)
_install_module("uplift_ble.models", DiscoveredDesk=object)
_install_module(
    "uplift_ble.desk_enums",
    DeskEventType=_desk_event_type,
    DeskUnit=_desk_unit,
)
_install_module(
    "uplift_ble.ble_protos", BLEClientProtocol=object, BLEDeviceProtocol=object
)

_install_module("bleak", package=True, BleakClient=object)
_install_module("bleak.backends", package=True)
_install_module("bleak.backends.device", BLEDevice=object)
_install_module(
    "bleak_retry_connector",
    BleakClientWithServiceCache=object,
    establish_connection=AsyncMock(),
)

_install_module("homeassistant", package=True)
_install_module("homeassistant.components", package=True)
_install_module("homeassistant.helpers", package=True)
_install_module("homeassistant.config_entries", ConfigEntry=object)
_install_module("homeassistant.const", CONF_ADDRESS="address", Platform=_platform)
_install_module(
    "homeassistant.core", CoreState=_core_state, HomeAssistant=object
)
_install_module("homeassistant.exceptions", ConfigEntryNotReady=Exception)
_install_module(
    "homeassistant.helpers.update_coordinator",
    CoordinatorEntity=object,
    DataUpdateCoordinator=_DataUpdateCoordinator,
    UpdateFailed=Exception,
)
_install_module(
    "homeassistant.helpers.dispatcher", async_dispatcher_send=MagicMock()
)
_install_module(
    "homeassistant.components.bluetooth",
    async_ble_device_from_address=MagicMock(),
    BluetoothScanningMode=object,
    BluetoothServiceInfoBleak=object,
)

from custom_components.uplift_desk import async_unload_entry
from custom_components.uplift_desk.coordinator import UpliftDeskBluetoothCoordinator


def _coordinator(controller: object | None = None) -> UpliftDeskBluetoothCoordinator:
    coordinator = UpliftDeskBluetoothCoordinator.__new__(
        UpliftDeskBluetoothCoordinator
    )
    coordinator._discovered_desk = SimpleNamespace(
        name="Test Desk", address="00:11:22:33:44:55"
    )
    coordinator._desk = controller
    coordinator._get_desk_controller = AsyncMock()
    return coordinator


def _controller(
    *, client: object | None, stop: AsyncMock | None = None
) -> SimpleNamespace:
    return SimpleNamespace(client=client, stop=stop or AsyncMock())


def _client(*, disconnect: AsyncMock | None = None) -> SimpleNamespace:
    return SimpleNamespace(is_connected=False, disconnect=disconnect or AsyncMock())


class UnloadEntryTests(IsolatedAsyncioTestCase):
    async def test_unloads_platforms_before_disconnecting(self) -> None:
        events: list[str] = []

        async def unload_platforms(*args: object) -> bool:
            events.append("platforms")
            return True

        async def disconnect() -> None:
            events.append("disconnect")

        hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_unload_platforms=unload_platforms)
        )
        entry = SimpleNamespace(
            runtime_data=SimpleNamespace(async_disconnect=disconnect)
        )

        result = await async_unload_entry(hass, entry)

        self.assertTrue(result)
        self.assertEqual(events, ["platforms", "disconnect"])

    async def test_platform_failure_keeps_connection(self) -> None:
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_unload_platforms=AsyncMock(return_value=False)
            )
        )
        coordinator = SimpleNamespace(async_disconnect=AsyncMock())

        result = await async_unload_entry(
            hass, SimpleNamespace(runtime_data=coordinator)
        )

        self.assertFalse(result)
        coordinator.async_disconnect.assert_not_awaited()

    async def test_missing_runtime_data_does_not_fail_unload(self) -> None:
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_unload_platforms=AsyncMock(return_value=True)
            )
        )

        self.assertTrue(await async_unload_entry(hass, SimpleNamespace()))


class CoordinatorDisconnectTests(IsolatedAsyncioTestCase):
    async def test_no_controller_never_connects(self) -> None:
        coordinator = _coordinator()

        await coordinator.async_disconnect()

        coordinator._get_desk_controller.assert_not_awaited()

    async def test_disconnected_client_is_stopped_and_disconnected(self) -> None:
        client = _client()
        controller = _controller(client=client)
        coordinator = _coordinator(controller)

        await coordinator.async_disconnect()

        controller.stop.assert_awaited_once()
        client.disconnect.assert_awaited_once()
        coordinator._get_desk_controller.assert_not_awaited()
        self.assertIsNone(coordinator._desk)
        self.assertIsNone(controller.client)

    async def test_controller_is_detached_before_teardown(self) -> None:
        coordinator = _coordinator()

        async def stop() -> None:
            self.assertIsNone(coordinator._desk)

        controller = _controller(client=None, stop=AsyncMock(side_effect=stop))
        coordinator._desk = controller

        await coordinator.async_disconnect()

        controller.stop.assert_awaited_once()

    async def test_stop_failure_does_not_prevent_disconnect(self) -> None:
        client = _client()
        controller = _controller(
            client=client, stop=AsyncMock(side_effect=RuntimeError("stop failed"))
        )
        coordinator = _coordinator(controller)

        await coordinator.async_disconnect()

        client.disconnect.assert_awaited_once()
        self.assertIsNone(controller.client)

    async def test_stop_cancellation_still_attempts_disconnect(self) -> None:
        client = _client()
        controller = _controller(
            client=client, stop=AsyncMock(side_effect=asyncio.CancelledError)
        )

        with self.assertRaises(asyncio.CancelledError):
            await _coordinator(controller).async_disconnect()

        client.disconnect.assert_awaited_once()
        self.assertIsNone(controller.client)

    async def test_disconnect_failure_does_not_block_unload(self) -> None:
        disconnect = AsyncMock(side_effect=RuntimeError("disconnect failed"))
        controller = _controller(client=_client(disconnect=disconnect))

        await _coordinator(controller).async_disconnect()

        disconnect.assert_awaited_once()
        self.assertIsNone(controller.client)

    async def test_disconnect_cancellation_propagates_after_cleanup(self) -> None:
        disconnect = AsyncMock(side_effect=asyncio.CancelledError)
        controller = _controller(client=_client(disconnect=disconnect))

        with self.assertRaises(asyncio.CancelledError):
            await _coordinator(controller).async_disconnect()

        disconnect.assert_awaited_once()
        self.assertIsNone(controller.client)

    async def test_disconnect_is_idempotent(self) -> None:
        client = _client()
        controller = _controller(client=client)
        coordinator = _coordinator(controller)

        await coordinator.async_disconnect()
        await coordinator.async_disconnect()

        controller.stop.assert_awaited_once()
        client.disconnect.assert_awaited_once()

    async def test_normal_connect_path_is_preserved(self) -> None:
        controller = _controller(client=_client())
        controller.start = AsyncMock()
        coordinator = _coordinator()
        coordinator._get_desk_controller = AsyncMock(return_value=controller)

        await coordinator.async_connect()

        coordinator._get_desk_controller.assert_awaited_once()
        controller.start.assert_awaited_once()
