"""Shared fixtures and BLE test doubles for the uplift_desk test suite.

The suite exercises the *real* ``UpliftDeskBluetoothCoordinator`` and the
*real* ``uplift_ble.DeskController`` against a fake BLE client that records
every GATT interaction, so the (re)connect lifecycle, GATT service
validation, and controller teardown all run as production code. Only the
BLE stack (``establish_connection``, ``clear_cache``, device resolution)
and the client itself are faked.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest
from bleak import BleakError
from bleak.backends.service import (
    BleakGATTCharacteristic,
    BleakGATTService,
    BleakGATTServiceCollection,
)
from homeassistant.const import CONF_ADDRESS

from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.uplift_desk as uplift_desk_module
import custom_components.uplift_desk.coordinator as coordinator_module
from custom_components.uplift_desk.coordinator import (
    UpliftDeskBluetoothCoordinator,
)
from uplift_ble.desk_configs import DESK_CONFIGS_BY_SERVICE, DeskConfig

# Load the Home Assistant core test plugin (provides the `hass` fixture).
pytest_plugins = "pytest_homeassistant_custom_component"

# --- Test constants ---------------------------------------------------------

DESK_ADDRESS = "00:11:22:33:44:55"
DESK_NAME = "Uplift Desk Test"
DESK_DOMAIN = "uplift_desk"

# The 0xFE60 Jiecang variant, used as the default desk profile in tests.
DESK_SERVICE_UUID = "0000fe60-0000-1000-8000-00805f9b34fb"
DESK_CONFIG: DeskConfig = DESK_CONFIGS_BY_SERVICE[DESK_SERVICE_UUID]


def pytest_configure(config: pytest.Config) -> None:
    """Run pytest-asyncio in auto mode.

    The Home Assistant test plugin declares async fixtures with plain
    ``@pytest.fixture``; pytest-asyncio only processes those in auto mode.
    """
    config.option.asyncio_mode = "auto"


# --- BLE fakes ---------------------------------------------------------------


class FakeBLEDevice:
    """Stand-in for a bleak BLEDevice (only ``.address``/``.name`` are used)."""

    def __init__(self, address: str, name: str) -> None:
        self.address = address
        self.name = name


class FakeBleakClient:
    """A plain (non-BleakClient) stand-in for a connected bleak client.

    Records every GATT interaction so tests can assert on them, and exposes
    hooks to simulate an unexpected link drop and to push notifications into
    the (real) ``DeskController`` notify handler.
    """

    def __init__(
        self,
        services: BleakGATTServiceCollection | None,
        address: str = DESK_ADDRESS,
        name: str = DESK_NAME,
        services_raise: bool = False,
    ) -> None:
        self.address = address
        self.name = name
        self.is_connected = False
        self._services = services
        # When True, ``self.services`` raises instead of returning a
        # collection (bleak "Service Discovery has not been performed yet").
        self._services_raise = services_raise
        self._disconnected_callback: (
            Callable[[FakeBleakClient], None] | None
        ) = None
        self._notify_handler: Callable | None = None
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.simulated_drops = 0
        self.start_notify_calls: list[str] = []
        self.stop_notify_calls: list[str] = []
        self.stop_notify_attempts: list[str] = []
        self.writes: list[tuple[str, bytes, bool]] = []

    @property
    def services(self) -> BleakGATTServiceCollection:
        if self._services_raise:
            raise BleakError("Service Discovery has not been performed yet")
        assert self._services is not None
        return self._services

    # --- GATT operations (all recorded) -------------------------------------

    async def connect(self) -> None:
        self.connect_calls += 1
        self.is_connected = True

    async def disconnect(self) -> bool:
        if not self.is_connected:
            return False
        self.disconnect_calls += 1
        self.is_connected = False
        return True

    async def start_notify(self, char_uuid: str, handler: Callable) -> None:
        if not self.is_connected:
            raise BleakError("Not connected")
        self.start_notify_calls.append(char_uuid)
        self._notify_handler = handler

    async def stop_notify(self, char_uuid: str) -> None:
        self.stop_notify_attempts.append(char_uuid)
        if not self.is_connected:
            # Mirrors the BlueZ backend: stop_notify on a dropped client
            # raises BleakError("Not connected").
            raise BleakError("Not connected")
        self.stop_notify_calls.append(char_uuid)
        self._notify_handler = None

    async def write_gatt_char(
        self, char_uuid: str, data: bytes, response: bool = False
    ) -> None:
        if not self.is_connected:
            raise BleakError("Not connected")
        self.writes.append((char_uuid, bytes(data), response))

    # --- test hooks ----------------------------------------------------------

    def attach_disconnected_callback(self, callback: Callable) -> None:
        self._disconnected_callback = callback

    def simulate_disconnect(self) -> None:
        """Simulate an unexpected link drop (what bleak's watcher does)."""
        self.is_connected = False
        self.simulated_drops += 1
        if self._disconnected_callback is not None:
            self._disconnected_callback(self)

    async def simulate_notification(self, data: bytes) -> None:
        """Push a GATT notification into the registered notify handler."""
        if self._notify_handler is None:
            raise AssertionError("no notify handler registered on fake client")
        await self._notify_handler(self, data)


def build_service_collection(
    config: DeskConfig | None,
    missing_characteristics: tuple[str, ...] = (),
) -> BleakGATTServiceCollection:
    """Build a ``BleakGATTServiceCollection`` for a desk config.

    Pass ``config=None`` for an empty collection (the Issue 2 BlueZ cache
    race), or list characteristic UUIDs in ``missing_characteristics`` to
    simulate a partial service set.
    """
    collection = BleakGATTServiceCollection()
    if config is None:
        return collection
    service = BleakGATTService(None, 1, config.service_uuid)
    collection.add_service(service)
    for offset, char_uuid in enumerate(
        (
            config.input_char_uuid,
            config.output_char_uuid,
            config.name_char_uuid,
        )
    ):
        if char_uuid in missing_characteristics:
            continue
        characteristic = BleakGATTCharacteristic(
            None, 10 + offset, char_uuid, [], lambda: 512, service
        )
        # Adds the characteristic to both the collection and its service.
        collection.add_characteristic(characteristic)
    return collection


class FakeEstablishConnection:
    """Callable stand-in for ``bleak_retry_connector.establish_connection``.

    Hands out pre-queued :class:`FakeBleakClient` instances (marking them
    connected, as the real helper does) and captures the
    ``disconnected_callback`` kwarg so drops can be simulated.
    """

    def __init__(self) -> None:
        self._clients: deque[FakeBleakClient] = deque()
        self.fail_with: Exception | None = None
        self.calls: list[dict[str, Any]] = []

    def queue_client(self, client: FakeBleakClient) -> None:
        self._clients.append(client)

    async def __call__(
        self,
        client_class: Any,
        device: Any,
        name: str,
        disconnected_callback: Callable | None = None,
        max_attempts: int | None = None,
        **kwargs: Any,
    ) -> FakeBleakClient:
        self.calls.append(
            {
                "client_class": client_class,
                "device": device,
                "name": name,
                "disconnected_callback": disconnected_callback,
                "max_attempts": max_attempts,
            }
        )
        if self.fail_with is not None:
            raise self.fail_with
        if not self._clients:
            raise BleakError("FakeEstablishConnection: no clients left to hand out")
        client = self._clients.popleft()
        client.attach_disconnected_callback(disconnected_callback)
        await client.connect()
        return client

    @property
    def call_count(self) -> int:
        return len(self.calls)


class FakeBleHub:
    """The patched BLE stack: device registry, establish_connection, cache."""

    def __init__(self) -> None:
        self.device = FakeBLEDevice(DESK_ADDRESS, DESK_NAME)
        self.establish = FakeEstablishConnection()
        self.clear_cache = AsyncMock(return_value=True)

    def device_from_address(self, hass: Any, address: str) -> FakeBLEDevice | None:
        if address == self.device.address:
            return self.device
        return None

    def valid_client(self) -> FakeBleakClient:
        return FakeBleakClient(build_service_collection(DESK_CONFIG))

    def client_with_services(
        self,
        services: BleakGATTServiceCollection | None,
        services_raise: bool = False,
    ) -> FakeBleakClient:
        return FakeBleakClient(services, services_raise=services_raise)

    def queue_client(self, client: FakeBleakClient) -> None:
        self.establish.queue_client(client)


# --- Helpers -----------------------------------------------------------------


def make_notification_packet(opcode: int, payload: bytes) -> bytes:
    """Build a Uplift notification frame.

    Frame layout (see ``uplift_ble.packet``):
    ``F2 F2 <opcode> <len> <payload...> <checksum> 7E`` where the checksum is
    ``(opcode + len(payload) + sum(payload)) & 0xFF``.
    """
    checksum = (opcode + len(payload) + sum(payload)) & 0xFF
    return bytes([0xF2, 0xF2, opcode, len(payload), *payload, checksum, 0x7E])


async def wait_until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    """Spin the loop until ``condition()`` is true (fast under instant sleep)."""
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


# --- Fixtures ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations) -> None:
    """Allow the HA loader to find the integration under custom_components/."""
    yield


@pytest.fixture(autouse=True)
def instant_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse ``asyncio.sleep`` to a single loop yield so tests run fast.

    This keeps the proactive-reconnect backoff, the cache-clear grace period,
    and the DeskController's post-command notification wait from adding real
    wall-clock time to the tests.
    """
    real_sleep = asyncio.sleep

    async def fast_sleep(delay: float | None = None, *args: Any, **kwargs: Any) -> None:
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    yield


@pytest.fixture
def fake_ble(monkeypatch: pytest.MonkeyPatch) -> FakeBleHub:
    """Patch the BLE entry points used by the coordinator (and setup)."""
    hub = FakeBleHub()
    monkeypatch.setattr(coordinator_module, "establish_connection", hub.establish)
    monkeypatch.setattr(coordinator_module, "clear_cache", hub.clear_cache)
    monkeypatch.setattr(
        coordinator_module,
        "async_ble_device_from_address",
        hub.device_from_address,
    )
    # async_setup_entry resolves the device through its own module namespace.
    monkeypatch.setattr(
        uplift_desk_module,
        "async_ble_device_from_address",
        hub.device_from_address,
    )
    return hub


@pytest.fixture
def config_entry(hass):
    """A config entry pointing at the fake desk address."""
    entry = MockConfigEntry(
        domain=DESK_DOMAIN,
        data={CONF_ADDRESS: DESK_ADDRESS},
        title=DESK_NAME,
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
async def coordinator(hass, config_entry, fake_ble):
    """The real coordinator wired to the fake BLE stack."""
    coord = UpliftDeskBluetoothCoordinator(hass, config_entry, fake_ble.device)
    yield coord
    # Teardown: stop the reconnect loop and controller so no tasks linger.
    await coord.async_disconnect()
    for _ in range(10):
        await asyncio.sleep(0)
