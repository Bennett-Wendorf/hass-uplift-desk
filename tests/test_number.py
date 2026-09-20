"""Tests for the height setpoint number entity.

Covers the entity's static properties (BOX mode, DISTANCE device class, mm
native unit, 1 mm step, fallback 500-1300 bounds), the dynamic min/max fed by
the height-limit notifications (0x07 configuration, 0x21 max, 0x22 min), the
set path (0x1B move command), the in-flight coalescing guard, the
locked-desk guard, and the setpoint semantics: the entity shows the commanded
target while the desk moves toward it and returns to unknown on arrival,
interruption, or disconnect.

State assertions (min/max/value/available) go through a full config-entry
setup and ``hass.states``, because ``CoordinatorEntity`` only registers
``_handle_coordinator_update`` in ``async_added_to_hass`` — a bare
``DeskHeightSetpointNumber`` instance would not track coordinator updates.
Coordinator-level behavior (coalescing, lock guard) is tested on the
``coordinator`` fixture directly; the set-path test uses a bare instance
because ``async_set_native_value`` only delegates to the coordinator.
"""

from __future__ import annotations

import pytest
from homeassistant.components.number import NumberDeviceClass, NumberMode
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfLength,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.uplift_desk.coordinator import (
    UpliftDeskBluetoothCoordinator,
    UpliftDeskLockedError,
    UpliftDeskMoveInFlightError,
)
from custom_components.uplift_desk.const import (
    DEFAULT_HEIGHT_LIMIT_MAX_MM,
    DEFAULT_HEIGHT_LIMIT_MIN_MM,
)
from custom_components.uplift_desk.number import DeskHeightSetpointNumber
from uplift_ble.desk_enums import DeskLockStatus

from .conftest import (
    DESK_ADDRESS,
    DESK_CONFIG,
    DESK_DOMAIN,
    FakeBleHub,
    FakeBleakClient,
    make_notification_packet,
    wait_until,
)

# --- Local packet builders ---------------------------------------------------


def make_limits_config_packet(max_mm: int, min_mm: int) -> bytes:
    """Opcode 0x07 (height limits configuration): max 2-byte BE + min 2-byte BE."""
    return make_notification_packet(
        0x07, max_mm.to_bytes(2, "big") + min_mm.to_bytes(2, "big")
    )


def make_limit_max_packet(max_mm: int) -> bytes:
    """Opcode 0x21 (height limit max): 2-byte BE."""
    return make_notification_packet(0x21, max_mm.to_bytes(2, "big"))


def make_limit_min_packet(min_mm: int) -> bytes:
    """Opcode 0x22 (height limit min): 2-byte BE."""
    return make_notification_packet(0x22, min_mm.to_bytes(2, "big"))


def make_units_packet(unit_byte: int) -> bytes:
    """Opcode 0x0E (display unit preference): 1 byte (0x00=cm, 0x01=in)."""
    return make_notification_packet(0x0E, bytes([unit_byte]))


def make_height_packet(height_tenths: int) -> bytes:
    """Opcode 0x01 (current height): 2-byte BE height in tenths + 1 unknown byte."""
    return make_notification_packet(0x01, height_tenths.to_bytes(2, "big") + b"\x00")


def make_lock_packet(lock_byte: int) -> bytes:
    """Opcode 0x1F (lock status): 1 byte (0x00=unlocked, 0x01=locked)."""
    return make_notification_packet(0x1F, bytes([lock_byte]))


def make_command_packet(opcode: int, payload: bytes) -> bytes:
    """Build a command frame as written to the input characteristic.

    Mirrors ``uplift_ble.packet.create_command_packet``:
    ``F1 F1 <opcode> <len> <payload...> <checksum> 7E`` — same layout and
    checksum as the notification frames, but commands use the F1 F1 header
    (notifications use F2 F2).
    """
    checksum = (opcode + len(payload) + sum(payload)) & 0xFF
    return bytes([0xF1, 0xF1, opcode, len(payload), *payload, checksum, 0x7E])


# --- Setup helpers -----------------------------------------------------------


async def _setup_entry(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    fake_ble: FakeBleHub,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[UpliftDeskBluetoothCoordinator, FakeBleakClient]:
    """Run a full config-entry setup against the fake desk.

    Returns the entry's coordinator and the connected fake client.
    """
    # The manifest's bluetooth dependency would open a real BlueZ mgmt socket
    # during component setup; stub the bluetooth component (no-op success),
    # same as test_setup.py.
    async def _stub_bluetooth_setup(hass, config):
        return True

    monkeypatch.setattr(
        "homeassistant.components.bluetooth.async_setup", _stub_bluetooth_setup
    )
    await async_setup_component(hass, "homeassistant", {})

    client = fake_ble.valid_client()
    fake_ble.queue_client(client)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED
    return entry.runtime_data, client


def _number_entity_id(hass: HomeAssistant) -> str:
    """Resolve the height setpoint number's entity id via the entity registry."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "number", DESK_DOMAIN, f"{DESK_ADDRESS}_desk_height_setpoint"
    )
    assert entity_id is not None, "height setpoint number entity not in registry"
    return entity_id


async def _wait_for_number_bounds(
    hass: HomeAssistant, entity_id: str, min_mm: int, max_mm: int
) -> None:
    """Wait until the number's state reports the given min/max bounds."""
    await wait_until(
        lambda: (state := hass.states.get(entity_id)) is not None
        and state.attributes["min"] == min_mm
        and state.attributes["max"] == max_mm
    )


# --- Tests --------------------------------------------------------------------


async def test_number_entity_created_with_fallback_limits(
    hass, config_entry, fake_ble, monkeypatch
):
    """The number entity is created with BOX/DISTANCE/mm/1mm and fallback bounds."""
    await _setup_entry(hass, config_entry, fake_ble, monkeypatch)
    entity_id = _number_entity_id(hass)

    entity_entry = er.async_get(hass).async_get(entity_id)
    assert entity_entry is not None
    assert entity_entry.unique_id == f"{DESK_ADDRESS}_desk_height_setpoint"

    state = hass.states.get(entity_id)
    assert state is not None
    # No setpoint has been set: the value is unknown, but the entity is
    # available (the desk is connected).
    assert state.state == STATE_UNKNOWN
    # Fallback limits: no limit notification has been pushed.
    assert state.attributes["min"] == DEFAULT_HEIGHT_LIMIT_MIN_MM
    assert state.attributes["max"] == DEFAULT_HEIGHT_LIMIT_MAX_MM
    assert state.attributes["step"] == 1
    assert state.attributes["mode"] == NumberMode.BOX
    assert state.attributes["unit_of_measurement"] == UnitOfLength.MILLIMETERS
    assert state.attributes["device_class"] == NumberDeviceClass.DISTANCE


async def test_height_limits_configuration_updates_coordinator_and_number(
    hass, config_entry, fake_ble, monkeypatch
):
    """A 0x07 limits notification updates the coordinator and the number's bounds."""
    coordinator, client = await _setup_entry(hass, config_entry, fake_ble, monkeypatch)
    entity_id = _number_entity_id(hass)

    await client.simulate_notification(make_limits_config_packet(1200, 600))
    await wait_until(
        lambda: coordinator.height_limit_max_mm == 1200
        and coordinator.height_limit_min_mm == 600
    )
    assert coordinator.height_limit_max_mm == 1200
    assert coordinator.height_limit_min_mm == 600

    # The number's bounds follow the coordinator's effective limits. Asserted
    # via the entity state: the entity's _attr_ bounds only refresh on
    # coordinator updates, which a bare instance would not receive.
    await _wait_for_number_bounds(hass, entity_id, min_mm=600, max_mm=1200)
    state = hass.states.get(entity_id)
    assert state.attributes["min"] == 600
    assert state.attributes["max"] == 1200


async def test_height_limit_max_min_events_update_individually(
    hass, config_entry, fake_ble, monkeypatch
):
    """0x21 and 0x22 notifications update the max/min limits independently."""
    coordinator, client = await _setup_entry(hass, config_entry, fake_ble, monkeypatch)
    entity_id = _number_entity_id(hass)

    # Only the max is reported (0x21): max moves, min stays at the fallback.
    await client.simulate_notification(make_limit_max_packet(1100))
    await wait_until(lambda: coordinator.height_limit_max_mm == 1100)
    assert coordinator.height_limit_max_mm == 1100
    assert coordinator.height_limit_min_mm is None
    await _wait_for_number_bounds(
        hass, entity_id, min_mm=DEFAULT_HEIGHT_LIMIT_MIN_MM, max_mm=1100
    )

    # Then only the min (0x22): min moves, max is preserved.
    await client.simulate_notification(make_limit_min_packet(650))
    await wait_until(lambda: coordinator.height_limit_min_mm == 650)
    assert coordinator.height_limit_max_mm == 1100
    assert coordinator.height_limit_min_mm == 650
    await _wait_for_number_bounds(hass, entity_id, min_mm=650, max_mm=1100)


async def test_set_value_issues_move_command(fake_ble, coordinator):
    """async_set_native_value(800) issues exactly one 0x1B move write (800 mm BE)."""
    client = fake_ble.valid_client()
    fake_ble.queue_client(client)
    await coordinator.async_connect()

    # A bare instance is fine: async_set_native_value only delegates to the
    # coordinator (it never reads entity state).
    number = DeskHeightSetpointNumber(coordinator)
    await number.async_set_native_value(800)

    assert coordinator._move_in_flight is False
    # The successful set leaves the commanded target as the active setpoint.
    assert coordinator.height_setpoint_mm == 800

    move_frame = make_command_packet(0x1B, (800).to_bytes(2, "big"))
    input_writes = [
        data
        for char_uuid, data, _ in client.writes
        if char_uuid == DESK_CONFIG.input_char_uuid
    ]
    # Exactly one 0x1B move command to the input characteristic (the desk
    # profile also sends a wake preamble before it).
    assert input_writes.count(move_frame) == 1


async def test_second_set_while_in_flight_is_rejected(fake_ble, coordinator):
    """A set while a move is in flight raises and issues no BLE write."""
    client = fake_ble.valid_client()
    fake_ble.queue_client(client)
    await coordinator.async_connect()

    # White-box: a first move command is in flight.
    coordinator._move_in_flight = True
    writes_before = len(client.writes)

    with pytest.raises(UpliftDeskMoveInFlightError):
        await coordinator.async_move_to_height(800)

    # No BLE write was issued for the rejected set...
    assert len(client.writes) == writes_before
    # ...and the in-flight command still owns the flag.
    assert coordinator._move_in_flight is True
    # No setpoint was ever set in this test (the rejection happens before any
    # setpoint mutation).
    assert coordinator.height_setpoint_mm is None


async def test_locked_desk_rejects_move(fake_ble, coordinator):
    """A desk that last reported LOCKED rejects the move; lock_status None proceeds."""
    client = fake_ble.valid_client()
    fake_ble.queue_client(client)
    await coordinator.async_connect()

    move_frame = make_command_packet(0x1B, (800).to_bytes(2, "big"))

    def _move_write_count() -> int:
        return sum(
            1
            for char_uuid, data, _ in client.writes
            if char_uuid == DESK_CONFIG.input_char_uuid and data == move_frame
        )

    # No lock status reported yet (None): the move proceeds.
    await coordinator.async_move_to_height(800)
    assert coordinator._move_in_flight is False
    assert _move_write_count() == 1
    assert coordinator.height_setpoint_mm == 800

    # The desk now reports LOCKED (0x1F notification, byte 0x01).
    await client.simulate_notification(make_lock_packet(0x01))
    await wait_until(
        lambda: coordinator._desk is not None
        and coordinator._desk.lock_status is DeskLockStatus.LOCKED
    )

    writes_before = len(client.writes)
    with pytest.raises(UpliftDeskLockedError):
        await coordinator.async_move_to_height(800)

    # The rejected move issued no 0x1B write (no writes at all)...
    assert len(client.writes) == writes_before
    # ...and the flag is cleared again.
    assert coordinator._move_in_flight is False
    # The rejected set did not wipe the earlier target: the setpoint is
    # restored to its pre-call value.
    assert coordinator.height_setpoint_mm == 800


async def test_set_value_shows_target_while_height_streams_in(
    hass, config_entry, fake_ble, monkeypatch
):
    """The number keeps showing the target while height notifications stream in."""
    coordinator, client = await _setup_entry(hass, config_entry, fake_ble, monkeypatch)
    entity_id = _number_entity_id(hass)

    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": 800}, blocking=True
    )
    await wait_until(
        lambda: (state := hass.states.get(entity_id)) is not None
        and float(state.state) == 800.0
    )

    # The desk reports its display unit (cm), then streams the live height
    # while it moves toward the target.
    await client.simulate_notification(make_units_packet(0x00))
    await client.simulate_notification(make_height_packet(750))
    await wait_until(lambda: coordinator.height_mm == 750.0)
    await client.simulate_notification(make_height_packet(760))
    await wait_until(lambda: coordinator.height_mm == 760.0)
    await client.simulate_notification(make_height_packet(770))
    await wait_until(lambda: coordinator.height_mm == 770.0)

    # The number still shows the commanded target, not the live height.
    await wait_until(
        lambda: (state := hass.states.get(entity_id)) is not None
        and float(state.state) == 800.0
    )
    assert coordinator.height_setpoint_mm == 800
    assert coordinator.height_mm == 770.0


async def test_setpoint_clears_on_arrival(
    hass, config_entry, fake_ble, monkeypatch
):
    """The number returns to unknown when the desk arrives within 4 mm of the target."""
    coordinator, client = await _setup_entry(hass, config_entry, fake_ble, monkeypatch)
    entity_id = _number_entity_id(hass)

    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": 800}, blocking=True
    )
    await wait_until(
        lambda: (state := hass.states.get(entity_id)) is not None
        and float(state.state) == 800.0
    )

    await client.simulate_notification(make_units_packet(0x00))
    await client.simulate_notification(make_height_packet(750))
    await wait_until(lambda: coordinator.height_mm == 750.0)
    assert float(hass.states.get(entity_id).state) == 800.0

    await client.simulate_notification(make_height_packet(790))
    await wait_until(lambda: coordinator.height_mm == 790.0)
    assert float(hass.states.get(entity_id).state) == 800.0

    # 798 mm is within the 4 mm arrival tolerance of the 800 mm target.
    await client.simulate_notification(make_height_packet(798))
    await wait_until(lambda: coordinator.height_mm == 798.0)
    await wait_until(lambda: hass.states.get(entity_id).state == STATE_UNKNOWN)
    assert coordinator.height_setpoint_mm is None


async def test_setpoint_clears_on_interruption(
    hass, config_entry, fake_ble, monkeypatch
):
    """The number returns to unknown when the desk moves away from the target."""
    coordinator, client = await _setup_entry(hass, config_entry, fake_ble, monkeypatch)
    entity_id = _number_entity_id(hass)

    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": 800}, blocking=True
    )
    await wait_until(
        lambda: (state := hass.states.get(entity_id)) is not None
        and float(state.state) == 800.0
    )

    await client.simulate_notification(make_units_packet(0x00))
    await client.simulate_notification(make_height_packet(750))
    await wait_until(lambda: coordinator.height_mm == 750.0)
    await client.simulate_notification(make_height_packet(760))
    await wait_until(lambda: coordinator.height_mm == 760.0)
    assert float(hass.states.get(entity_id).state) == 800.0

    # A 5 mm move away from the target interrupts the move.
    await client.simulate_notification(make_height_packet(755))
    await wait_until(lambda: coordinator.height_mm == 755.0)
    await wait_until(lambda: hass.states.get(entity_id).state == STATE_UNKNOWN)
    assert coordinator.height_setpoint_mm is None


async def test_setpoint_survives_single_mm_jitter(
    hass, config_entry, fake_ble, monkeypatch
):
    """A single-quantum (1 mm) backward blip does not clear the setpoint."""
    coordinator, client = await _setup_entry(hass, config_entry, fake_ble, monkeypatch)
    entity_id = _number_entity_id(hass)

    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": 800}, blocking=True
    )
    await wait_until(
        lambda: (state := hass.states.get(entity_id)) is not None
        and float(state.state) == 800.0
    )

    await client.simulate_notification(make_units_packet(0x00))
    await client.simulate_notification(make_height_packet(750))
    await wait_until(lambda: coordinator.height_mm == 750.0)
    await client.simulate_notification(make_height_packet(760))
    await wait_until(lambda: coordinator.height_mm == 760.0)

    # 1 mm backward: indistinguishable from encoder jitter.
    await client.simulate_notification(make_height_packet(759))
    await wait_until(lambda: coordinator.height_mm == 759.0)

    await wait_until(
        lambda: (state := hass.states.get(entity_id)) is not None
        and float(state.state) == 800.0
    )
    assert coordinator.height_setpoint_mm == 800


async def test_setpoint_equal_to_current_height_clears_immediately(
    hass, config_entry, fake_ble, monkeypatch
):
    """Setting the current height still sends the command but clears the setpoint at once."""
    coordinator, client = await _setup_entry(hass, config_entry, fake_ble, monkeypatch)
    entity_id = _number_entity_id(hass)

    # The desk is already at 800 mm.
    await client.simulate_notification(make_units_packet(0x00))
    await client.simulate_notification(make_height_packet(800))
    await wait_until(lambda: coordinator.height_mm == 800.0)

    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": 800}, blocking=True
    )
    await wait_until(lambda: hass.states.get(entity_id).state == STATE_UNKNOWN)

    # The command was still sent (the firmware accepts it harmlessly)...
    move_frame = make_command_packet(0x1B, (800).to_bytes(2, "big"))
    input_writes = [
        data
        for char_uuid, data, _ in client.writes
        if char_uuid == DESK_CONFIG.input_char_uuid
    ]
    assert input_writes.count(move_frame) == 1
    # ...but the setpoint is cleared immediately (treated as immediate arrival).
    assert coordinator.height_setpoint_mm is None


async def test_number_unavailable_when_disconnected(
    hass, config_entry, fake_ble, monkeypatch
):
    """The number goes unavailable on a link drop and recovers on reconnect."""
    coordinator, client = await _setup_entry(hass, config_entry, fake_ble, monkeypatch)
    entity_id = _number_entity_id(hass)

    # An active setpoint is cleared by the disconnect.
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": 800}, blocking=True
    )
    await wait_until(
        lambda: (state := hass.states.get(entity_id)) is not None
        and float(state.state) == 800.0
    )

    # Simulate an unexpected link drop; a fresh client is queued so the
    # proactive reconnect loop can restore the connection.
    client.simulate_disconnect()
    fake_ble.queue_client(fake_ble.valid_client())

    await wait_until(
        lambda: coordinator.is_connected is False
        and hass.states.get(entity_id).state == STATE_UNAVAILABLE
    )
    assert coordinator.is_connected is False
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
    # The link is gone, so the commanded target is no longer being tracked.
    assert coordinator.height_setpoint_mm is None

    await wait_until(
        lambda: coordinator.is_connected is True
        and hass.states.get(entity_id).state == STATE_UNKNOWN
    )
    assert coordinator.is_connected
    # No stale setpoint survives the reconnect: the entity is unknown.
    assert hass.states.get(entity_id).state == STATE_UNKNOWN
