"""Tests for the coordinator (re)connect lifecycle.

These tests cover the two production bugs this spec fixes:

* Issue 1 — after a BLE drop the coordinator rebuilt a ``DeskController``
  but never called ``start()``, so GATT notifications never resumed.
* Issue 2 — a connected client whose GATT service set is empty/partial
  (BlueZ manager cache race) was used as-is, later failing deep inside a
  command write with ``BleakCharacteristicNotFoundError``.

Tests 1-4 below genuinely fail if ``start()`` is removed from the (re)connect
cycle or if the service validation is removed.
"""

from __future__ import annotations

import asyncio

import pytest
from bleak.backends.service import BleakGATTServiceCollection

import custom_components.uplift_desk.coordinator as coordinator_module
from custom_components.uplift_desk.coordinator import UpliftDeskServicesError
from custom_components.uplift_desk.sensor import DeskHeightSensor

from .conftest import (
    DESK_ADDRESS,
    DESK_CONFIG,
    build_service_collection,
    wait_until,
)


async def test_reconnect_after_drop_starts_fresh_controller(fake_ble, coordinator):
    """A dropped link is recovered with a NEW controller whose start() ran.

    The old client must be torn down (stop attempted + disconnected) and the
    new client's output characteristic must receive start_notify exactly once.
    """
    first = fake_ble.valid_client()
    second = fake_ble.valid_client()
    fake_ble.queue_client(first)
    fake_ble.queue_client(second)

    await coordinator.async_connect()
    first_controller = coordinator._desk
    assert coordinator.is_connected
    assert first.start_notify_calls == [DESK_CONFIG.output_char_uuid]

    # The link drops unexpectedly (bleak watcher fires the callback).
    first.simulate_disconnect()

    # The proactive reconnect cycle must install a NEW controller on the NEW
    # client and start notifications on it exactly once.
    await wait_until(
        lambda: coordinator._desk is not None and coordinator._desk.client is second
    )
    new_controller = coordinator._desk

    assert new_controller is not first_controller
    assert second.start_notify_calls == [DESK_CONFIG.output_char_uuid]
    assert second.simulated_drops == 0
    assert not new_controller._processor_task.done()
    assert coordinator.is_connected
    # Exactly one connection for the initial connect, one for the reconnect.
    assert fake_ble.establish.call_count == 2

    # The outgoing controller was torn down BEFORE replacement: its
    # processor task is cancelled (no task leak), stop() was attempted on
    # the old client, and the old client is down.
    assert first_controller._processor_task.cancelled()
    assert first.stop_notify_attempts == [DESK_CONFIG.output_char_uuid]
    assert first.is_connected is False


async def test_successful_cycle_uses_single_connection(fake_ble, coordinator):
    """One successful (re)connect cycle opens exactly one BLE connection.

    The pre-fix code connected twice (validator churn) per cycle; the cycle
    must now establish a single connection that is validated in place.
    """
    client = fake_ble.valid_client()
    fake_ble.queue_client(client)

    await coordinator.async_connect()

    assert fake_ble.establish.call_count == 1
    assert coordinator.is_connected
    assert coordinator._desk.client is client
    assert client.start_notify_calls == [DESK_CONFIG.output_char_uuid]


@pytest.mark.parametrize(
    "bad_services",
    [
        pytest.param(BleakGATTServiceCollection(), id="empty-services"),
        pytest.param(
            build_service_collection(
                DESK_CONFIG,
                missing_characteristics=(DESK_CONFIG.output_char_uuid,),
            ),
            id="missing-output-characteristic",
        ),
    ],
)
async def test_invalid_first_client_clears_cache_and_retries_once(
    fake_ble, coordinator, bad_services
):
    """An invalid first client triggers one cache clear + one retry.

    Covers the Issue 2 recovery path: the empty/partial collection is
    detected on the connected client, the stack cache is cleared, the client
    is discarded, and the cycle retries once on a fresh (valid) client.
    """
    bad = fake_ble.client_with_services(bad_services)
    good = fake_ble.valid_client()
    fake_ble.queue_client(bad)
    fake_ble.queue_client(good)

    await coordinator.async_connect()

    # One cache clear for the whole cycle, then recovery on the 2nd client.
    assert fake_ble.establish.call_count == 2
    fake_ble.clear_cache.assert_awaited_once_with(DESK_ADDRESS)
    assert coordinator._desk is not None
    assert coordinator._desk.client is good
    assert good.start_notify_calls == [DESK_CONFIG.output_char_uuid]
    assert coordinator.is_connected
    # The invalid client was discarded (best-effort disconnect).
    assert bad.disconnect_calls == 1
    assert bad.start_notify_calls == []


async def test_both_clients_invalid_raises_and_installs_no_controller(
    fake_ble, coordinator
):
    """Two invalid clients fail the cycle cleanly with no controller left."""
    first = fake_ble.client_with_services(BleakGATTServiceCollection())
    second = fake_ble.client_with_services(
        build_service_collection(
            DESK_CONFIG,
            missing_characteristics=(DESK_CONFIG.input_char_uuid,),
        )
    )
    fake_ble.queue_client(first)
    fake_ble.queue_client(second)

    with pytest.raises(UpliftDeskServicesError):
        await coordinator.async_connect()

    assert fake_ble.establish.call_count == 2
    # Cache was cleared before each (failed) connection.
    assert fake_ble.clear_cache.await_count == 2
    # No controller was installed and no notifications were started.
    assert coordinator._desk is None
    assert coordinator.is_connected is False
    assert first.start_notify_calls == []
    assert second.start_notify_calls == []
    # Both clients were discarded.
    assert first.disconnect_calls == 1
    assert second.disconnect_calls == 1


async def test_disconnect_with_dropped_desk_does_not_reconnect(
    fake_ble, coordinator
):
    """async_disconnect() on a dropped desk: no reconnect, no BleakError.

    Covers the pre-fix "reconnect-to-disconnect" bug (async_disconnect went
    through the (re)connect path just to tear down) and the
    ``BleakError("Not connected")`` that stop_notify raises on a dropped
    client.
    """
    first = fake_ble.valid_client()
    fake_ble.queue_client(first)
    await coordinator.async_connect()

    first.simulate_disconnect()
    # Let the disconnect handler tear the controller down and start the
    # proactive reconnect loop (it fails immediately: no clients are queued).
    await wait_until(lambda: coordinator._desk is None)
    for _ in range(10):
        await asyncio.sleep(0)

    # The disconnect path must never fetch/establish a controller.
    get_calls = []
    original_get = coordinator._get_or_establish_controller

    async def spy_get():
        get_calls.append(1)
        return await original_get()

    coordinator._get_or_establish_controller = spy_get

    # Must complete without raising (stop_notify on the dropped client
    # raises BleakError("Not connected"), which the coordinator tolerates).
    await coordinator.async_disconnect()

    assert get_calls == []
    assert coordinator._desk is None
    assert coordinator.is_connected is False
    assert coordinator._intentional_disconnect is True
    # The reconnect loop was cancelled and will not attempt more connects.
    assert coordinator._reconnect_task is None
    calls_after_disconnect = fake_ble.establish.call_count
    for _ in range(10):
        await asyncio.sleep(0)
    assert fake_ble.establish.call_count == calls_after_disconnect


async def test_drop_schedules_reconnect_and_restores_availability(
    fake_ble, coordinator, monkeypatch
):
    """Unexpected drop -> unavailable -> proactive reconnect -> available.

    The disconnected callback must schedule the reconnect task; after the
    (mocked) backoff the full cycle runs and the sensor flips
    available False -> True with no user action.
    """
    monkeypatch.setattr(
        coordinator_module, "_RECONNECT_BACKOFF_SECONDS", (0, 0, 0, 0)
    )
    first = fake_ble.valid_client()
    second = fake_ble.valid_client()
    fake_ble.queue_client(first)
    fake_ble.queue_client(second)

    await coordinator.async_connect()
    sensor = DeskHeightSensor(coordinator)
    assert sensor.available is True

    first.simulate_disconnect()

    # While the link is down the sensor is unavailable...
    await wait_until(lambda: coordinator._desk is None)
    assert coordinator.is_connected is False
    assert sensor.available is False

    # ...and the proactive reconnect (after the mocked backoff) restores it.
    await wait_until(lambda: coordinator.is_connected)
    assert sensor.available is True
    assert coordinator._desk.client is second
    assert second.start_notify_calls == [DESK_CONFIG.output_char_uuid]
    assert fake_ble.establish.call_count == 2


async def test_drop_notifies_listeners_so_entities_go_unavailable(
    fake_ble, coordinator
):
    """An unexpected drop actively notifies coordinator listeners.

    Regression: before the fix the disconnect handler only tore the
    controller down and started reconnecting without pushing anything to the
    coordinator's listeners, so entities (sensor + buttons) kept reporting
    their stale ``available = True`` state until a successful reconnect
    re-pushed data. The handler must call ``async_update_listeners()`` so
    entities flip to unavailable immediately.
    """
    client = fake_ble.valid_client()
    fake_ble.queue_client(client)
    await coordinator.async_connect()

    calls = []
    coordinator.async_add_listener(lambda: calls.append(1))

    # Simulate an unexpected link drop (the bleak watcher fires the callback)
    # and let the disconnect handler run. Without the fix, the listener is
    # never called and this times out.
    client.simulate_disconnect()
    await wait_until(lambda: len(calls) >= 1)

    # A listener notification proves ``async_update_listeners()`` ran on the
    # disconnect path — what makes the entities go unavailable right away.
    assert len(calls) >= 1
    assert coordinator._desk is None
    assert coordinator.is_connected is False


async def test_services_not_discovered_is_treated_as_invalid(fake_ble, coordinator):
    """A client whose services raise (discovery not performed) is recovered.

    Secondary Issue 2 signature: ``BleakError("Service Discovery has not
    been performed yet")`` from the connected client must take the same
    cache-clear + one-retry path as an empty/partial collection.
    """
    broken = fake_ble.client_with_services(None, services_raise=True)
    good = fake_ble.valid_client()
    fake_ble.queue_client(broken)
    fake_ble.queue_client(good)

    await coordinator.async_connect()

    assert fake_ble.establish.call_count == 2
    fake_ble.clear_cache.assert_awaited_once_with(DESK_ADDRESS)
    assert coordinator._desk.client is good
    assert good.start_notify_calls == [DESK_CONFIG.output_char_uuid]
    assert coordinator.is_connected
