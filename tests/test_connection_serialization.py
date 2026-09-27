"""Lifecycle races exercised with the real controller and upstream BLE fakes."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from bleak.backends.service import BleakGATTServiceCollection

import custom_components.uplift_desk.coordinator as coordinator_module

from .conftest import DESK_CONFIG, wait_until


@pytest.mark.parametrize("background", [False, True])
async def test_concurrent_reconnects_share_started_controller(
    fake_ble, coordinator, monkeypatch, background
):
    """Commands and the background loop must join an in-flight connection."""
    client = fake_ble.valid_client()
    fake_ble.queue_client(client)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_start = client.start_notify

    async def start_notify(*args):
        entered.set()
        await release.wait()
        await original_start(*args)

    monkeypatch.setattr(client, "start_notify", start_notify)
    first = asyncio.create_task(coordinator._get_or_establish_controller())
    await entered.wait()
    if background:
        coordinator._start_reconnect_loop()
        second = coordinator._reconnect_task
    else:
        second = asyncio.create_task(coordinator._get_or_establish_controller())
    for _ in range(5):
        await asyncio.sleep(0)
    release.set()
    results = await asyncio.wait_for(
        asyncio.gather(first, second, return_exceptions=True), timeout=1
    )

    assert not any(isinstance(result, BaseException) for result in results)
    assert fake_ble.establish.call_count == 1
    assert coordinator._desk is results[0]
    if not background:
        assert results[0] is results[1]
    assert client.start_notify_calls == [DESK_CONFIG.output_char_uuid]


async def test_delayed_drop_handler_preserves_replacement(
    fake_ble, coordinator, monkeypatch
):
    """An old client's queued callback cannot tear down a new controller."""
    first = fake_ble.valid_client()
    second = fake_ble.valid_client()
    fake_ble.queue_client(first)
    fake_ble.queue_client(second)
    await coordinator.async_connect()
    old_controller = coordinator._desk
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    original_handler = coordinator._async_handle_unexpected_disconnect

    async def delayed_handler(*args):
        entered.set()
        await release.wait()
        await original_handler(*args)
        finished.set()

    monkeypatch.setattr(coordinator, "_async_handle_unexpected_disconnect", delayed_handler)
    first.simulate_disconnect()
    await entered.wait()
    replacement = await coordinator._get_or_establish_controller()
    release.set()
    await finished.wait()

    assert coordinator._desk is replacement
    assert replacement.client is second
    assert second.is_connected
    assert second.disconnect_calls == 0
    assert old_controller._processor_task.cancelled()
    assert fake_ble.establish.call_count == 2


@pytest.mark.parametrize("phase", ["establish", "start"])
async def test_unload_rejects_in_flight_and_waiting_commands(
    fake_ble, coordinator, monkeypatch, phase
):
    """Unload waits for cleanup and rejects both active and queued connects."""
    client = fake_ble.valid_client()
    fake_ble.queue_client(client)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = fake_ble.establish if phase == "establish" else client.start_notify

    async def blocked(*args, **kwargs):
        result = await original(*args, **kwargs)
        entered.set()
        await release.wait()
        return result

    if phase == "establish":
        monkeypatch.setattr(coordinator_module, "establish_connection", blocked)
    else:
        monkeypatch.setattr(client, "start_notify", blocked)
    command = asyncio.create_task(coordinator._get_or_establish_controller())
    await entered.wait()
    waiter = asyncio.create_task(coordinator._get_or_establish_controller())
    unload = asyncio.create_task(coordinator.async_disconnect())
    await wait_until(lambda: coordinator._intentional_disconnect)
    release.set()
    results = await asyncio.gather(command, waiter, unload, return_exceptions=True)

    assert isinstance(results[0], RuntimeError)
    assert isinstance(results[1], RuntimeError)
    assert results[2] is None
    assert coordinator._intentional_disconnect
    assert coordinator._desk is None
    assert not client.is_connected
    assert fake_ble.establish.call_count == 1


async def test_commands_after_unload_do_not_reconnect(fake_ble, coordinator):
    """A command cannot clear the shutdown flag after teardown completes."""
    fake_ble.queue_client(fake_ble.valid_client())
    await coordinator.async_disconnect()

    with pytest.raises(RuntimeError, match="disconnecting"):
        await coordinator._get_or_establish_controller()

    assert fake_ble.establish.call_count == 0


async def test_cancel_during_cache_clear_releases_client(
    fake_ble, coordinator, monkeypatch
):
    """An invalid connected client must be discarded even on cancellation."""
    client = fake_ble.client_with_services(BleakGATTServiceCollection())
    fake_ble.queue_client(client)
    monkeypatch.setattr(
        coordinator_module, "clear_cache", AsyncMock(side_effect=asyncio.CancelledError)
    )

    with pytest.raises(asyncio.CancelledError):
        await coordinator.async_connect()

    assert not client.is_connected
    assert client.disconnect_calls == 1
    assert coordinator._desk is None


async def test_drop_during_refresh_does_not_reenter_connection(
    fake_ble, coordinator, monkeypatch
):
    """Refresh remains on the acquired controller if its link drops mid-read."""
    first = fake_ble.valid_client()
    second = fake_ble.valid_client()
    fake_ble.queue_client(first)
    fake_ble.queue_client(second)
    original_write = first.write_gatt_char

    async def drop_on_write(*args, **kwargs):
        await original_write(*args, **kwargs)
        first.is_connected = False

    monkeypatch.setattr(first, "write_gatt_char", drop_on_write)
    result = await asyncio.wait_for(coordinator._get_or_establish_controller(), timeout=1)

    assert result.client is first
    assert fake_ble.establish.call_count == 1
    assert coordinator._desk is result


async def test_partial_start_failure_stops_notification_processor(
    fake_ble, coordinator, monkeypatch
):
    """Failure after start must stop the real processor as well as BLE."""
    client = fake_ble.valid_client()
    fake_ble.queue_client(client)
    original_start = coordinator_module.DeskController.start
    started = []

    async def fail_after_start(controller):
        await original_start(controller)
        started.append(controller)
        raise RuntimeError("failed after notification start")

    monkeypatch.setattr(coordinator_module.DeskController, "start", fail_after_start)
    with pytest.raises(RuntimeError, match="failed after notification start"):
        await coordinator.async_connect()

    assert coordinator._desk is None
    assert not client.is_connected
    assert started[0]._processor_task.cancelled()
    assert client.stop_notify_calls == [DESK_CONFIG.output_char_uuid]
