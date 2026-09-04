"""Regression tests for UPLIFT config-entry teardown."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.uplift_desk import async_unload_entry


async def test_unload_platforms_before_disconnecting() -> None:
    """Entity platforms unload before the BLE coordinator is torn down."""
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

    assert await async_unload_entry(hass, entry) is True
    assert events == ["platforms", "disconnect"]


async def test_platform_unload_failure_keeps_connection() -> None:
    """A failed entity-platform unload leaves the BLE coordinator running."""
    coordinator = SimpleNamespace(async_disconnect=AsyncMock())
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_unload_platforms=AsyncMock(return_value=False)
        )
    )

    assert (
        await async_unload_entry(
            hass,
            SimpleNamespace(runtime_data=coordinator),
        )
        is False
    )
    coordinator.async_disconnect.assert_not_awaited()


async def test_missing_runtime_data_does_not_fail_unload() -> None:
    """A partially initialized entry can still unload its platforms."""
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_unload_platforms=AsyncMock(return_value=True)
        )
    )

    assert await async_unload_entry(hass, SimpleNamespace()) is True


async def test_stop_failure_still_disconnects_client(coordinator) -> None:
    """An ordinary controller-stop failure does not leak the BLE connection."""
    client = SimpleNamespace(is_connected=True, disconnect=AsyncMock())
    controller = SimpleNamespace(
        client=client,
        stop=AsyncMock(side_effect=RuntimeError("stop failed")),
    )
    coordinator._desk = controller

    await coordinator.async_disconnect()

    client.disconnect.assert_awaited_once()
    assert coordinator._desk is None


async def test_stop_cancellation_disconnects_then_propagates(coordinator) -> None:
    """Cancellation propagates only after best-effort BLE cleanup."""
    client = SimpleNamespace(is_connected=True, disconnect=AsyncMock())
    controller = SimpleNamespace(
        client=client,
        stop=AsyncMock(side_effect=asyncio.CancelledError),
    )
    coordinator._desk = controller

    with pytest.raises(asyncio.CancelledError):
        await coordinator.async_disconnect()

    client.disconnect.assert_awaited_once()
    assert coordinator._desk is None


async def test_disconnect_failure_does_not_block_unload(coordinator) -> None:
    """An ordinary BLE disconnect failure is contained during teardown."""
    client = SimpleNamespace(
        is_connected=True,
        disconnect=AsyncMock(side_effect=RuntimeError("disconnect failed")),
    )
    coordinator._desk = SimpleNamespace(client=client, stop=AsyncMock())

    await coordinator.async_disconnect()

    client.disconnect.assert_awaited_once()
    assert coordinator._desk is None


async def test_disconnect_cancellation_propagates(coordinator) -> None:
    """Cancellation during BLE disconnect is not mistaken for an ordinary failure."""
    client = SimpleNamespace(
        is_connected=True,
        disconnect=AsyncMock(side_effect=asyncio.CancelledError),
    )
    coordinator._desk = SimpleNamespace(client=client, stop=AsyncMock())

    with pytest.raises(asyncio.CancelledError):
        await coordinator.async_disconnect()

    client.disconnect.assert_awaited_once()
    assert coordinator._desk is None


async def test_unload_caller_cancellation_propagates(coordinator) -> None:
    """Cancelling unload is not swallowed while its reconnect task is cancelled."""
    reconnect_started = asyncio.Event()
    reconnect_cancelled = asyncio.Event()

    async def reconnect() -> None:
        reconnect_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            reconnect_cancelled.set()
            await asyncio.Future()

    reconnect_task = asyncio.create_task(reconnect())
    await reconnect_started.wait()
    coordinator._reconnect_task = reconnect_task

    unload_task = asyncio.create_task(coordinator.async_disconnect())
    await reconnect_cancelled.wait()
    unload_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await unload_task

    assert coordinator._intentional_disconnect is True
    assert coordinator._reconnect_task is None
    assert reconnect_task.cancelled()


async def test_disconnect_is_idempotent(coordinator) -> None:
    """Repeated unload cleanup does not stop or disconnect twice."""
    client = SimpleNamespace(is_connected=True, disconnect=AsyncMock())
    controller = SimpleNamespace(client=client, stop=AsyncMock())
    coordinator._desk = controller

    await coordinator.async_disconnect()
    await coordinator.async_disconnect()

    controller.stop.assert_awaited_once()
    client.disconnect.assert_awaited_once()
