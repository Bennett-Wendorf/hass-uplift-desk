"""Bounded recovery through the selected client's service-cache API."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from bleak import BleakError

from .conftest import build_service_collection


def install_cache_clear(monkeypatch, client, *, result=True, error=None):
    """Model a backend cache API that must run before BLE disconnection."""
    async def clear():
        assert client.is_connected
        if error is not None:
            raise error
        return result

    method = AsyncMock(side_effect=clear)
    monkeypatch.setattr(client, "clear_cache", method, raising=False)
    return method


async def test_missing_services_clear_the_selected_backend_cache(
    coordinator, fake_ble, monkeypatch
):
    first = fake_ble.client_with_services(build_service_collection(None))
    second = fake_ble.valid_client()
    fake_ble.queue_client(first)
    fake_ble.queue_client(second)
    clear = install_cache_clear(monkeypatch, first)

    await coordinator.async_connect()

    clear.assert_awaited_once()
    fake_ble.clear_cache.assert_not_awaited()
    assert first.disconnect_calls == 1
    assert coordinator._desk.client is second
    assert first.writes == second.writes == []


@pytest.mark.parametrize("error", [None, BleakError("cache unavailable")])
async def test_local_bluez_fallback_remains_available(
    coordinator, fake_ble, monkeypatch, error
):
    first = fake_ble.client_with_services(build_service_collection(None))
    fake_ble.queue_client(first)
    fake_ble.queue_client(fake_ble.valid_client())
    clear = install_cache_clear(monkeypatch, first, result=False, error=error)

    await coordinator.async_connect()

    clear.assert_awaited_once()
    fake_ble.clear_cache.assert_awaited_once_with(coordinator.desk_address)
    assert first.disconnect_calls == 1


async def test_notification_timeout_clears_cache_and_starts_a_fresh_controller(
    coordinator, fake_ble, monkeypatch
):
    first = fake_ble.valid_client()
    second = fake_ble.valid_client()
    fake_ble.queue_client(first)
    fake_ble.queue_client(second)
    clear = install_cache_clear(monkeypatch, first)
    monkeypatch.setattr(
        first, "start_notify", AsyncMock(side_effect=TimeoutError("CCCD write timed out"))
    )

    await coordinator.async_connect()

    clear.assert_awaited_once()
    fake_ble.clear_cache.assert_not_awaited()
    first.start_notify.assert_awaited_once()
    assert first.disconnect_calls == 1
    assert len(second.start_notify_calls) == 1
    assert coordinator._desk.client is second
    assert fake_ble.establish.call_count == 2
    assert first.writes == second.writes == []


async def test_second_notification_timeout_stops_without_a_third_attempt(
    coordinator, fake_ble, monkeypatch
):
    first, second, unused = [fake_ble.valid_client() for _ in range(3)]
    for client in (first, second, unused):
        fake_ble.queue_client(client)
    clears = [install_cache_clear(monkeypatch, c) for c in (first, second)]
    for client in (first, second):
        monkeypatch.setattr(
            client, "start_notify", AsyncMock(side_effect=TimeoutError("CCCD timeout"))
        )

    with pytest.raises(TimeoutError, match="CCCD timeout"):
        await coordinator.async_connect()

    clears[0].assert_awaited_once()
    clears[1].assert_not_awaited()
    fake_ble.clear_cache.assert_not_awaited()
    assert fake_ble.establish.call_count == 2
    assert first.disconnect_calls == second.disconnect_calls == 1
    assert unused.connect_calls == 0
    assert coordinator._desk is None


async def test_cache_clear_cancellation_releases_client_without_retry(
    coordinator, fake_ble, monkeypatch
):
    first = fake_ble.valid_client()
    fake_ble.queue_client(first)
    fake_ble.queue_client(fake_ble.valid_client())
    clear = install_cache_clear(monkeypatch, first, error=asyncio.CancelledError())
    monkeypatch.setattr(first, "start_notify", AsyncMock(side_effect=TimeoutError()))

    with pytest.raises(asyncio.CancelledError):
        await coordinator.async_connect()

    clear.assert_awaited_once()
    fake_ble.clear_cache.assert_not_awaited()
    assert first.disconnect_calls == 1
    assert fake_ble.establish.call_count == 1
    assert coordinator._desk is None


async def test_unload_during_cache_recovery_prevents_retry(
    coordinator, fake_ble, monkeypatch
):
    first, second = [fake_ble.valid_client() for _ in range(2)]
    fake_ble.queue_client(first)
    fake_ble.queue_client(second)
    entered, release = asyncio.Event(), asyncio.Event()

    async def clear():
        entered.set()
        await release.wait()
        return True

    monkeypatch.setattr(first, "clear_cache", clear, raising=False)
    monkeypatch.setattr(first, "start_notify", AsyncMock(side_effect=TimeoutError()))
    connect = asyncio.create_task(coordinator.async_connect())
    disconnect = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        disconnect = asyncio.create_task(coordinator.async_disconnect())
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(RuntimeError, match="disconnecting"):
            await connect
        await disconnect
        assert first.disconnect_calls == 1
        assert second.connect_calls == 0
        assert coordinator._desk is None
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (connect, disconnect) if task is not None),
            return_exceptions=True,
        )


async def test_start_cancellation_is_not_a_cache_recovery_trigger(
    coordinator, fake_ble, monkeypatch
):
    first = fake_ble.valid_client()
    fake_ble.queue_client(first)
    clear = install_cache_clear(monkeypatch, first)
    monkeypatch.setattr(
        first, "start_notify", AsyncMock(side_effect=asyncio.CancelledError())
    )

    with pytest.raises(asyncio.CancelledError):
        await coordinator.async_connect()

    clear.assert_not_awaited()
    fake_ble.clear_cache.assert_not_awaited()
    assert first.disconnect_calls == 1
