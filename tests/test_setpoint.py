"""Coordinator-level tests for the height setpoint lifecycle.

Exercises the real ``UpliftDeskBluetoothCoordinator`` and the real
``uplift_ble.DeskController`` against the fake BLE client (no full
config-entry setup): the setpoint is set optimistically on a successful
move, cleared on arrival (within tolerance or on crossing), on interruption
(away-move beyond the jitter epsilon), on command failure (restoring the
previous setpoint), and on both disconnect paths; it is never persisted, so
a reconnect starts from ``None``.

The controller emits ``HEIGHT`` only once the display unit is known, so a
``0x0E`` units packet is pushed before any height packet in every test.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from bleak import BleakError

from custom_components.uplift_desk.coordinator import (
    UpliftDeskLockedError,
    UpliftDeskMoveInFlightError,
)
from uplift_ble.desk_enums import DeskLockStatus

from .conftest import (
    DESK_CONFIG,
    FakeBleakClient,
    build_service_collection,
    wait_until,
)
from .test_number import (
    make_command_packet,
    make_height_packet,
    make_lock_packet,
    make_units_packet,
)


def _input_writes(client) -> list[bytes]:
    """The command frames written to the desk's input characteristic."""
    return [
        data
        for char_uuid, data, _ in client.writes
        if char_uuid == DESK_CONFIG.input_char_uuid
    ]


def _move_frame(height_mm: int) -> bytes:
    """The 0x1B move-to-height command frame for the given height (mm)."""
    return make_command_packet(0x1B, height_mm.to_bytes(2, "big"))


class SpontaneousHeightClient(FakeBleakClient):
    """A fake client that reports its display unit and height on (re)connect.

    Mirrors a real desk that pushes a height notification shortly after the
    link is (re)established (e.g. completing a move that was in flight while
    the link was down). The notification is pushed right after ``start_notify``
    — i.e. before the coordinator's post-(re)connect state refresh — so it is
    the first height report the fresh controller sees.
    """

    def __init__(
        self,
        services,
        height_tenths: int,
        unit_byte: int = 0x00,
        **kwargs,
    ) -> None:
        super().__init__(services, **kwargs)
        self._spontaneous_height_tenths = height_tenths
        self._spontaneous_unit_byte = unit_byte

    async def start_notify(self, char_uuid: str, handler) -> None:
        await super().start_notify(char_uuid, handler)
        asyncio.get_running_loop().call_soon(self._push_state)

    def _push_state(self) -> None:
        asyncio.get_running_loop().create_task(self._push())

    async def _push(self) -> None:
        # Report the display unit first so the height is interpretable, then
        # the (completed) height — like a real desk on (re)connect.
        await self.simulate_notification(
            make_units_packet(self._spontaneous_unit_byte)
        )
        await self.simulate_notification(
            make_height_packet(self._spontaneous_height_tenths)
        )


async def _connect_cm_desk(fake_ble, coordinator):
    """Connect a cm-mode desk: fresh client, connect, report the display unit."""
    client = fake_ble.valid_client()
    fake_ble.queue_client(client)
    await coordinator.async_connect()
    await client.simulate_notification(make_units_packet(0x00))
    return client


async def test_setpoint_set_on_successful_move(fake_ble, coordinator):
    """A successful move leaves the commanded target as the active setpoint."""
    client = await _connect_cm_desk(fake_ble, coordinator)
    await client.simulate_notification(make_height_packet(700))
    await wait_until(lambda: coordinator.height_mm == 700.0)

    await coordinator.async_move_to_height(800)

    assert coordinator.height_setpoint_mm == 800
    # Exactly one 0x1B move command (the desk profile also sends a wake
    # preamble before it).
    assert _input_writes(client).count(_move_frame(800)) == 1


async def test_setpoint_cleared_on_arrival(fake_ble, coordinator):
    """A reported height within 4 mm of the target clears the setpoint."""
    client = await _connect_cm_desk(fake_ble, coordinator)

    await coordinator.async_move_to_height(800)
    assert coordinator.height_setpoint_mm == 800

    await client.simulate_notification(make_height_packet(750))
    await wait_until(lambda: coordinator.height_mm == 750.0)
    assert coordinator.height_setpoint_mm == 800

    # 798 mm is within the 4 mm arrival tolerance of the 800 mm target.
    await client.simulate_notification(make_height_packet(798))
    await wait_until(lambda: coordinator.height_mm == 798.0)
    assert coordinator.height_setpoint_mm is None


async def test_setpoint_cleared_on_crossing_target(fake_ble, coordinator):
    """Crossing the target between two notifications clears the setpoint."""
    client = await _connect_cm_desk(fake_ble, coordinator)

    await coordinator.async_move_to_height(800)

    await client.simulate_notification(make_height_packet(700))
    await wait_until(lambda: coordinator.height_mm == 700.0)
    assert coordinator.height_setpoint_mm == 800

    await client.simulate_notification(make_height_packet(790))
    await wait_until(lambda: coordinator.height_mm == 790.0)
    assert coordinator.height_setpoint_mm == 800

    # 790 -> 810 jumps over the 800 mm target without landing within
    # tolerance (a sparse notification, or a superseding preset move).
    await client.simulate_notification(make_height_packet(810))
    await wait_until(lambda: coordinator.height_mm == 810.0)
    assert coordinator.height_setpoint_mm is None


async def test_setpoint_cleared_on_interruption(fake_ble, coordinator):
    """Moving away from the target by more than the epsilon clears it."""
    client = await _connect_cm_desk(fake_ble, coordinator)

    await coordinator.async_move_to_height(800)

    await client.simulate_notification(make_height_packet(750))
    await wait_until(lambda: coordinator.height_mm == 750.0)
    await client.simulate_notification(make_height_packet(760))
    await wait_until(lambda: coordinator.height_mm == 760.0)
    assert coordinator.height_setpoint_mm == 800

    # A 5 mm move away from the target interrupts the move (keypad/preset).
    await client.simulate_notification(make_height_packet(755))
    await wait_until(lambda: coordinator.height_mm == 755.0)
    assert coordinator.height_setpoint_mm is None


async def test_setpoint_survives_single_mm_jitter(fake_ble, coordinator):
    """A single-quantum (1 mm) backward blip is not an interruption."""
    client = await _connect_cm_desk(fake_ble, coordinator)

    await coordinator.async_move_to_height(800)

    await client.simulate_notification(make_height_packet(750))
    await wait_until(lambda: coordinator.height_mm == 750.0)
    await client.simulate_notification(make_height_packet(760))
    await wait_until(lambda: coordinator.height_mm == 760.0)

    # 1 mm backward: |delta| == 1 is not > the 1 mm jitter epsilon.
    await client.simulate_notification(make_height_packet(759))
    await wait_until(lambda: coordinator.height_mm == 759.0)
    assert coordinator.height_setpoint_mm == 800


async def test_setpoint_clears_immediately_when_desk_at_target(fake_ble, coordinator):
    """Commanding the current height sends the command but clears the setpoint."""
    client = await _connect_cm_desk(fake_ble, coordinator)
    await client.simulate_notification(make_height_packet(800))
    await wait_until(lambda: coordinator.height_mm == 800.0)

    await coordinator.async_move_to_height(800)

    # The command was still sent (the firmware accepts it harmlessly)...
    assert _input_writes(client).count(_move_frame(800)) == 1
    # ...but the desk is already at the target, so nothing is left to track.
    assert coordinator.height_setpoint_mm is None


@pytest.mark.parametrize("previous", [None, 800])
async def test_failed_move_restores_previous_setpoint(
    fake_ble, coordinator, monkeypatch, previous
):
    """A failed BLE write restores the pre-call setpoint (no stale target)."""
    client = await _connect_cm_desk(fake_ble, coordinator)

    if previous == 800:
        # A successful move first (the desk stays silent, so the setpoint
        # remains active).
        await coordinator.async_move_to_height(800)
        assert coordinator.height_setpoint_mm == 800

    monkeypatch.setattr(
        client, "write_gatt_char", AsyncMock(side_effect=BleakError("write failed"))
    )

    with pytest.raises(BleakError):
        await coordinator.async_move_to_height(700)

    assert coordinator.height_setpoint_mm == previous


async def test_locked_move_preserves_previous_setpoint(fake_ble, coordinator):
    """A locked desk rejects the move and preserves the active setpoint."""
    client = await _connect_cm_desk(fake_ble, coordinator)

    await coordinator.async_move_to_height(800)
    assert coordinator.height_setpoint_mm == 800

    # The desk now reports LOCKED (0x1F notification, byte 0x01).
    await client.simulate_notification(make_lock_packet(0x01))
    await wait_until(
        lambda: coordinator._desk is not None
        and coordinator._desk.lock_status is DeskLockStatus.LOCKED
    )

    with pytest.raises(UpliftDeskLockedError):
        await coordinator.async_move_to_height(700)

    # The rejected retarget did not wipe the earlier target.
    assert coordinator.height_setpoint_mm == 800


async def test_in_flight_rejection_preserves_setpoint(fake_ble, coordinator):
    """An in-flight rejection raises before any setpoint mutation."""
    client = await _connect_cm_desk(fake_ble, coordinator)

    await coordinator.async_move_to_height(800)
    assert coordinator.height_setpoint_mm == 800

    # White-box: a first move command is in flight.
    coordinator._move_in_flight = True
    with pytest.raises(UpliftDeskMoveInFlightError):
        await coordinator.async_move_to_height(700)

    assert coordinator.height_setpoint_mm == 800


async def test_setpoint_cleared_on_unexpected_disconnect(fake_ble, coordinator):
    """An unexpected link drop clears the setpoint."""
    client = await _connect_cm_desk(fake_ble, coordinator)

    await coordinator.async_move_to_height(800)
    assert coordinator.height_setpoint_mm == 800

    client.simulate_disconnect()
    # Wait for the disconnect handler to finish: the controller is torn down
    # (_desk is None) and the setpoint is cleared in the same handler.
    await wait_until(
        lambda: coordinator._desk is None
        and coordinator.height_setpoint_mm is None
    )
    assert coordinator.height_setpoint_mm is None


async def test_height_cache_reset_on_unexpected_disconnect(fake_ble, coordinator):
    """The cached height is invalidated when the link drops unexpectedly.

    Without the reset, the first height notification after a (re)connect
    reconciles the setpoint against a stale pre-disconnect height, which can
    spuriously clear a fresh setpoint (the interruption check sees a large
    away-move).
    """
    client = await _connect_cm_desk(fake_ble, coordinator)
    await client.simulate_notification(make_height_packet(750))
    await wait_until(lambda: coordinator.height_mm == 750.0)

    client.simulate_disconnect()
    # Wait for the disconnect handler to finish: the controller is torn down
    # (_desk is None), the setpoint is cleared, and the cached height is
    # invalidated in the same handler. (Waiting on height_mm directly avoids a
    # race where _desk is set to None before the height cache is cleared.)
    await wait_until(
        lambda: coordinator._desk is None
        and coordinator.height_setpoint_mm is None
        and coordinator.height_mm is None
    )
    # The cached position is gone with the link, so the first post-reconnect
    # notification reconciles against previous=None (skipping the
    # crossing/interruption checks).
    assert coordinator.height_mm is None


async def test_setpoint_cleared_on_intentional_disconnect(fake_ble, coordinator):
    """An intentional disconnect (unload) clears the setpoint."""
    client = await _connect_cm_desk(fake_ble, coordinator)

    await coordinator.async_move_to_height(800)
    assert coordinator.height_setpoint_mm == 800

    await coordinator.async_disconnect()
    assert coordinator.height_setpoint_mm is None


async def test_setpoint_none_after_reconnect(fake_ble, coordinator):
    """The setpoint is not persisted: after a reconnect it is unknown again."""
    client1 = fake_ble.valid_client()
    client2 = fake_ble.valid_client()
    fake_ble.queue_client(client1)
    fake_ble.queue_client(client2)
    await coordinator.async_connect()
    await client1.simulate_notification(make_units_packet(0x00))

    await coordinator.async_move_to_height(800)
    assert coordinator.height_setpoint_mm == 800

    client1.simulate_disconnect()
    await wait_until(
        lambda: coordinator.is_connected and coordinator._desk.client is client2
    )
    assert coordinator.height_setpoint_mm is None


async def test_setpoint_set_while_disconnected_shows_target(
    fake_ble, coordinator
):
    """A set issued while disconnected is tracked once the link re-establishes.

    Objective 1 requires the setpoint to show optimistically even while the
    on-demand (re)connect cycle is in progress, and the move command must go
    out on the freshly connected client.
    """
    client1 = await _connect_cm_desk(fake_ble, coordinator)
    await client1.simulate_notification(make_height_packet(700))
    await wait_until(lambda: coordinator.height_mm == 700.0)

    client1.simulate_disconnect()
    # Wait for the disconnect handler to fully finish (it clears the setpoint
    # and the height cache in the same handler); otherwise the handler's
    # setpoint clear would race with the move's optimistic set below.
    await wait_until(
        lambda: coordinator._desk is None
        and coordinator.height_setpoint_mm is None
        and coordinator.height_mm is None
    )

    # The move targets the desk while it is disconnected; a fresh valid
    # client answers the on-demand (re)connect cycle the command triggers.
    client2 = fake_ble.valid_client()
    fake_ble.queue_client(client2)

    await coordinator.async_move_to_height(800)

    await wait_until(
        lambda: coordinator.is_connected
        and coordinator._desk is not None
        and coordinator._desk.client is client2
    )
    assert coordinator.height_setpoint_mm == 800
    # Exactly one 0x1B move write, on the NEW client...
    assert _input_writes(client2).count(_move_frame(800)) == 1
    # ...and the old client got none for this command.
    assert _input_writes(client1).count(_move_frame(800)) == 0


async def test_setpoint_restored_to_none_when_reconnect_fails(
    fake_ble, coordinator
):
    """A set issued while disconnected that cannot reconnect restores None.

    The optimistic set must not leave a stale target when the (re)connect
    cycle fails and the command never goes out.
    """
    client = await _connect_cm_desk(fake_ble, coordinator)
    await client.simulate_notification(make_height_packet(700))
    await wait_until(lambda: coordinator.height_mm == 700.0)

    client.simulate_disconnect()
    # Wait for the disconnect handler to fully finish before issuing the
    # failing move (see the sibling test's comment on the handler race).
    await wait_until(
        lambda: coordinator._desk is None
        and coordinator.height_setpoint_mm is None
        and coordinator.height_mm is None
    )

    # Inject a connection failure: every establish_connection attempt raises.
    fake_ble.establish.fail_with = BleakError("connection failed")

    with pytest.raises(BleakError):
        await coordinator.async_move_to_height(800)

    # The optimistic set was restored: the command never went out.
    assert coordinator.height_setpoint_mm is None


async def test_arrival_in_inch_mode_within_quantization(fake_ble, coordinator):
    """Inch-mode arrival (2.54 mm quanta) clears within the 4 mm tolerance.

    Documents the tolerance choice: 315 tenths of an inch is 800.1 mm, 0.1 mm
    past the target - within tolerance despite the coarse inch quantization.
    """
    client = fake_ble.valid_client()
    fake_ble.queue_client(client)
    await coordinator.async_connect()
    await client.simulate_notification(make_units_packet(0x01))  # inches

    # 30.0 in = 762 mm.
    await client.simulate_notification(make_height_packet(300))
    await wait_until(lambda: coordinator.height_mm == 762.0)

    await coordinator.async_move_to_height(800)
    assert coordinator.height_setpoint_mm == 800

    # 31.0 in = 787.4 mm: still 12.6 mm short of the target.
    await client.simulate_notification(make_height_packet(310))
    await wait_until(lambda: coordinator.height_mm == 787.4)
    assert coordinator.height_setpoint_mm == 800

    # 31.5 in = 800.1 mm: within the 4 mm arrival tolerance -> clear.
    await client.simulate_notification(make_height_packet(315))
    await wait_until(
        lambda: coordinator.height_mm is not None
        and abs(coordinator.height_mm - 800.1) < 1e-6
    )
    assert coordinator.height_setpoint_mm is None


async def test_no_spurious_clear_on_first_notify_after_reconnect(
    fake_ble, coordinator
):
    """The first height report after a (re)connect can't spuriously clear a setpoint.

    End-to-end regression for the stale height cache: the desk is at 750, the
    user commands 800, the link drops mid-move, and the desk completes the
    move to 800 while the link is down. After the (re)connect the user
    commands 700. The desk's first height report after the (re)connect is 800
    (the completed stale move). Without the height cache reset, that report
    reconciles against the stale 750 and looks like a 50 mm move away from the
    fresh 700 target, spuriously clearing the setpoint. With the reset, it
    reconciles against previous=None and the setpoint stays 700.
    """
    client1 = await _connect_cm_desk(fake_ble, coordinator)
    await client1.simulate_notification(make_height_packet(750))
    await wait_until(lambda: coordinator.height_mm == 750.0)

    await coordinator.async_move_to_height(800)
    assert coordinator.height_setpoint_mm == 800

    client1.simulate_disconnect()
    # Wait for the disconnect handler to finish: it tears down the controller
    # (_desk is None) and clears the setpoint (which is 800 here, so this
    # genuinely waits for the handler rather than returning immediately).
    await wait_until(
        lambda: coordinator._desk is None
        and coordinator.height_setpoint_mm is None
    )

    # The desk completes the stale 800 move while the link is down and reports
    # it (with its display unit) right after the (re)connect.
    client2 = SpontaneousHeightClient(
        build_service_collection(DESK_CONFIG), height_tenths=800
    )
    fake_ble.queue_client(client2)

    # The user commands a new target while the desk is (re)connecting.
    await coordinator.async_move_to_height(700)

    # Wait for the (re)connect to complete and the spontaneous 800 report to
    # be processed (it is the first height report the fresh controller sees).
    await wait_until(
        lambda: coordinator.is_connected
        and coordinator._desk is not None
        and coordinator._desk.client is client2
        and coordinator.height_mm == 800.0
    )
    # The first post-(re)connect report (800, the completed stale move) must
    # not look like an away-move from the fresh 700 target.
    assert coordinator.height_setpoint_mm == 700
