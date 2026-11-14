"""Saving presets and reporting stored preset heights through HA and the real controller."""

import asyncio
import logging
import time
from unittest.mock import AsyncMock

import pytest
from bleak import BleakError
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, EntityCategory
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from uplift_ble.desk_configs import DESK_CONFIGS_BY_SERVICE
from uplift_ble.desk_enums import DeskUnit

from custom_components.uplift_desk.const import (
    CONF_QUERY_ON_CONNECT,
    PRESET_REPORT_DELAY_SECONDS,
    PRESET_SAVE_SETTLE_SECONDS,
)
from custom_components.uplift_desk.coordinator import decode_preset_height_mm

from .conftest import (
    DESK_ADDRESS,
    build_service_collection,
    make_notification_packet,
    wait_until,
)
from .test_fallback_unit import make_entry
from .test_notifications import make_height_packet, make_units_packet

CONFIG = DESK_CONFIGS_BY_SERVICE["0000ff00-0000-1000-8000-00805f9b34fb"]


def command(opcode: int) -> tuple[str, bytes, bool]:
    """A recorded zero-payload command write on the desk's input characteristic."""
    return (CONFIG.input_char_uuid, bytes((0xF1, 0xF1, opcode, 0x00, opcode, 0x7E)), False)


WAKE = command(0x00)
# Saves for presets 3/4 reuse the opcodes that report presets 1/2; they are not
# the 0x27/0x28 recalls, which would move the desk.
SAVE_OPCODES = {1: 0x03, 2: 0x04, 3: 0x25, 4: 0x26}
REQUEST_LIMITS = command(0x07)


def preset_packet(slot: int, raw: int) -> bytes:
    """Opcodes 0x25-0x28: the raw height stored in presets 1-4."""
    return make_notification_packet(0x24 + slot, raw.to_bytes(2, "big"))


def limits_packet(max_mm: int, min_mm: int) -> bytes:
    """Opcode 0x07: max then min configured height limits in mm."""
    return make_notification_packet(0x07, max_mm.to_bytes(2, "big") + min_mm.to_bytes(2, "big"))


def entity_id(hass, domain: str, key: str) -> str:
    resolved = er.async_get(hass).async_get_entity_id(
        domain, "uplift_desk", f"{DESK_ADDRESS}_{key}"
    )
    assert resolved is not None
    return resolved


async def press(hass, key: str) -> None:
    await hass.services.async_call(
        "button", "press", {"entity_id": entity_id(hass, "button", key)}, blocking=True
    )


def preset_state(hass, slot: int) -> str:
    return hass.states.get(entity_id(hass, "sensor", f"desk_preset_{slot}_height")).state


@pytest.fixture(autouse=True)
def stub_bluetooth_setup(monkeypatch):
    """Keep HA dependency loading from opening the host Bluetooth stack."""
    monkeypatch.setattr(
        "homeassistant.components.bluetooth.async_setup", AsyncMock(return_value=True)
    )


async def setup_desk(hass, fake_ble, options=None, extra_clients=0, enabled=()):
    entry = make_entry(hass, options=options)
    registry = er.async_get(hass)
    for domain, key in enabled:
        # An entity already in the registry keeps its enabled state, which is
        # how a user-enabled, disabled-by-default entity looks at setup.
        registry.async_get_or_create(
            domain, "uplift_desk", f"{DESK_ADDRESS}_{key}", config_entry=entry
        )
    clients = [
        fake_ble.client_with_services(build_service_collection(CONFIG))
        for _ in range(1 + extra_clients)
    ]
    for client in clients:
        fake_ble.queue_client(client)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    clients[0].writes.clear()
    return entry, clients


@pytest.fixture
async def loaded_desk(hass, fake_ble):
    entry, clients = await setup_desk(hass, fake_ble)
    try:
        yield entry, clients[0]
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_save_and_preset_entities_are_registered(hass, loaded_desk):
    registry = er.async_get(hass)
    for slot in (1, 2, 3, 4):
        # Slots 3 and 4 start disabled, like their Move to Preset buttons.
        disabled_by = er.RegistryEntryDisabler.INTEGRATION if slot > 2 else None
        button = registry.async_get(entity_id(hass, "button", f"desk_save_preset_{slot}"))
        assert button.entity_category is EntityCategory.CONFIG
        assert button.disabled_by is disabled_by
        sensor = registry.async_get(entity_id(hass, "sensor", f"desk_preset_{slot}_height"))
        assert sensor.entity_category is EntityCategory.DIAGNOSTIC
        assert sensor.disabled_by is disabled_by
        if disabled_by is None:
            assert preset_state(hass, slot) == STATE_UNKNOWN


@pytest.mark.parametrize("slot", [3, 4])
async def test_enabled_extended_preset_saves_and_reports(hass, fake_ble, slot):
    entry, (client,) = await setup_desk(
        hass,
        fake_ble,
        enabled=[
            ("button", f"desk_save_preset_{slot}"),
            ("sensor", f"desk_preset_{slot}_height"),
        ],
    )
    try:
        await press(hass, f"desk_save_preset_{slot}")
        assert client.writes == [
            WAKE, WAKE, WAKE, command(SAVE_OPCODES[slot]), WAKE, WAKE, WAKE, REQUEST_LIMITS
        ]
        assert preset_state(hass, slot) == STATE_UNKNOWN

        await client.simulate_notification(preset_packet(slot, 300))
        await hass.async_block_till_done()
        assert float(preset_state(hass, slot)) == 762.0
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("slot", [1, 2])
async def test_save_writes_save_then_requests_presets(hass, loaded_desk, slot):
    _, client = loaded_desk

    await press(hass, f"desk_save_preset_{slot}")

    # The readback repeats the wake + 0x07 sequence the desk answers on connect.
    assert client.writes == [
        WAKE, WAKE, WAKE, command(SAVE_OPCODES[slot]), WAKE, WAKE, WAKE, REQUEST_LIMITS
    ]


async def test_saved_preset_is_unknown_until_the_desk_reports_it(hass, loaded_desk):
    _, client = loaded_desk
    await client.simulate_notification(preset_packet(1, 300))
    await client.simulate_notification(preset_packet(2, 486))
    await hass.async_block_till_done()
    assert float(preset_state(hass, 1)) == 762.0

    await press(hass, "desk_save_preset_1")
    await hass.async_block_till_done()
    assert preset_state(hass, 1) == STATE_UNKNOWN
    assert float(preset_state(hass, 2)) == pytest.approx(1234.44)

    await client.simulate_notification(preset_packet(1, 278))
    await hass.async_block_till_done()
    assert float(preset_state(hass, 1)) == pytest.approx(706.12)


async def test_save_skips_preset_request_when_queries_are_disabled(hass, fake_ble):
    entry, (client,) = await setup_desk(
        hass, fake_ble, options={CONF_QUERY_ON_CONNECT: False}
    )
    try:
        await press(hass, "desk_save_preset_1")
        assert client.writes == [WAKE, WAKE, WAKE, command(0x03)]
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_save_is_refused_until_reported_height_settles(hass, loaded_desk):
    entry, client = loaded_desk
    coordinator = entry.runtime_data
    await client.simulate_notification(make_units_packet(0x00))
    await client.simulate_notification(make_height_packet(750))
    await client.simulate_notification(make_height_packet(760))
    await wait_until(lambda: coordinator.height_mm == 760.0)

    with pytest.raises(HomeAssistantError, match="moving"):
        await press(hass, "desk_save_preset_1")
    assert client.writes == []

    # Settled: the last change is older than the settle window, and an idle
    # repeat of the same height does not count as movement.
    coordinator._last_motion_at = time.monotonic() - PRESET_SAVE_SETTLE_SECONDS
    await client.simulate_notification(make_height_packet(760))
    await hass.async_block_till_done()
    await press(hass, "desk_save_preset_1")
    assert command(0x03) in client.writes


async def test_first_height_after_connect_does_not_block_save(hass, loaded_desk):
    entry, client = loaded_desk
    await client.simulate_notification(make_units_packet(0x00))
    await client.simulate_notification(make_height_packet(750))
    await wait_until(lambda: entry.runtime_data.height_mm == 750.0)

    await press(hass, "desk_save_preset_2")

    assert command(0x04) in client.writes


async def test_save_is_refused_when_height_units_are_unknown(hass, loaded_desk):
    entry, client = loaded_desk
    # No unit report and no fallback: height changes cannot be observed.
    await client.simulate_notification(make_height_packet(750))
    await wait_until(lambda: entry.runtime_data._desk.height_raw == 750)

    with pytest.raises(HomeAssistantError, match="fallback height unit"):
        await press(hass, "desk_save_preset_1")
    assert client.writes == []


async def test_save_is_refused_right_after_a_recall(hass, loaded_desk):
    _, client = loaded_desk
    await press(hass, "desk_preset_1")
    client.writes.clear()

    with pytest.raises(HomeAssistantError, match="moving"):
        await press(hass, "desk_save_preset_2")
    assert client.writes == []


async def test_save_is_refused_while_a_height_move_is_pending(hass, loaded_desk):
    entry, client = loaded_desk
    await entry.runtime_data.async_move_to_height(900)
    client.writes.clear()

    # The desk never reports arriving, so the message must point at Stop.
    with pytest.raises(HomeAssistantError, match="press Stop to cancel"):
        await press(hass, "desk_save_preset_1")
    assert client.writes == []


async def test_stop_cancels_a_save_that_is_still_waking(loaded_desk, monkeypatch):
    entry, client = loaded_desk
    coordinator = entry.runtime_data
    entered = asyncio.Event()
    release = asyncio.Event()

    async def held_wake():
        entered.set()
        await release.wait()

    monkeypatch.setattr(coordinator._desk, "wake", held_wake)
    pending = asyncio.create_task(coordinator.async_save_preset(1))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await coordinator.async_stop_movement()
        release.set()
        with pytest.raises(HomeAssistantError, match="cancelled"):
            await pending
        assert client.writes == [command(0x2B)]
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_motion_during_wake_aborts_the_save(loaded_desk, monkeypatch):
    entry, client = loaded_desk
    coordinator = entry.runtime_data
    await client.simulate_notification(make_units_packet(0x00))
    await client.simulate_notification(make_height_packet(750))
    await wait_until(lambda: coordinator.height_mm == 750.0)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def held_wake():
        entered.set()
        await release.wait()

    monkeypatch.setattr(coordinator._desk, "wake", held_wake)
    pending = asyncio.create_task(coordinator.async_save_preset(1))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        # The keypad starts moving the desk while the save is still waking it.
        await client.simulate_notification(make_height_packet(760))
        await wait_until(lambda: coordinator.height_mm == 760.0)
        release.set()
        with pytest.raises(HomeAssistantError, match="moving"):
            await pending
        assert client.writes == []
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_failed_preset_request_does_not_fail_the_save(
    hass, loaded_desk, monkeypatch, caplog
):
    _, client = loaded_desk
    record_write = client.write_gatt_char

    async def write(char_uuid, data, response=False):
        if data[2] == 0x07:
            raise BleakError("link lost")
        await record_write(char_uuid, data, response)

    monkeypatch.setattr(client, "write_gatt_char", write)

    with caplog.at_level(logging.WARNING):
        await press(hass, "desk_save_preset_1")

    assert [packet for _, packet, _ in client.writes if packet[2] != 0x00] == [
        command(0x03)[1]
    ]
    assert "could not request its updated presets" in caplog.text


async def test_report_during_the_save_write_confirms_the_preset(
    hass, fake_ble, monkeypatch
):
    entry, (client,) = await setup_desk(
        hass, fake_ble, options={CONF_QUERY_ON_CONNECT: False}
    )
    try:
        coordinator = entry.runtime_data
        record_write = client.write_gatt_char

        async def write(char_uuid, data, response=False):
            await record_write(char_uuid, data, response)
            if data[2] == 0x03:
                # Some desks may report the new preset before the write returns.
                await client.simulate_notification(preset_packet(1, 300))
                await wait_until(lambda: coordinator._desk.height_preset_1 == 300)

        monkeypatch.setattr(client, "write_gatt_char", write)

        await press(hass, "desk_save_preset_1")
        await hass.async_block_till_done()

        assert float(preset_state(hass, 1)) == 762.0
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_failed_save_write_keeps_the_reported_preset(hass, loaded_desk, monkeypatch):
    entry, client = loaded_desk
    await client.simulate_notification(preset_packet(1, 300))
    await hass.async_block_till_done()
    record_write = client.write_gatt_char

    async def write(char_uuid, data, response=False):
        if data[2] == 0x03:
            # A notification landing mid-write publishes the unconfirmed slot.
            entry.runtime_data.async_update_listeners()
            raise BleakError("link lost")
        await record_write(char_uuid, data, response)

    monkeypatch.setattr(client, "write_gatt_char", write)

    with pytest.raises(BleakError):
        await press(hass, "desk_save_preset_1")
    await hass.async_block_till_done()

    assert float(preset_state(hass, 1)) == 762.0


async def test_failed_overlapping_save_keeps_the_other_saves_mark(
    hass, loaded_desk, monkeypatch
):
    entry, client = loaded_desk
    await client.simulate_notification(preset_packet(1, 300))
    await hass.async_block_till_done()
    record_write = client.write_gatt_char

    async def write(char_uuid, data, response=False):
        if data[2] == 0x03 and command(0x03) in client.writes:
            raise BleakError("link lost")
        await record_write(char_uuid, data, response)

    monkeypatch.setattr(client, "write_gatt_char", write)

    results = await asyncio.gather(
        press(hass, "desk_save_preset_1"),
        press(hass, "desk_save_preset_1"),
        return_exceptions=True,
    )

    assert [type(result) for result in results] == [type(None), BleakError]
    # The first save still awaits its report; the failed one must not confirm it.
    assert entry.runtime_data.height_preset_mm(1) is None


async def test_preset_reports_update_sensors(hass, loaded_desk):
    _, client = loaded_desk
    await client.simulate_notification(limits_packet(1300, 650))
    # Preset 1 reported in millimeters; preset 2 in tenths of an inch (30.0 in).
    await client.simulate_notification(preset_packet(1, 1100))
    await client.simulate_notification(preset_packet(2, 300))
    await hass.async_block_till_done()

    assert float(preset_state(hass, 1)) == 1100.0
    assert float(preset_state(hass, 2)) == 762.0


async def test_ambiguous_preset_resolves_when_limits_arrive_later(hass, loaded_desk):
    _, client = loaded_desk
    # 510 is 510 mm or 51.0 in (1295.4 mm); both fit the 500-1300 mm fallback.
    await client.simulate_notification(preset_packet(1, 510))
    await hass.async_block_till_done()
    assert preset_state(hass, 1) == STATE_UNKNOWN

    await client.simulate_notification(limits_packet(1300, 650))
    await hass.async_block_till_done()
    assert float(preset_state(hass, 1)) == pytest.approx(1295.4)


async def test_display_unit_breaks_a_preset_tie(hass, loaded_desk):
    _, client = loaded_desk
    await client.simulate_notification(preset_packet(1, 510))
    await hass.async_block_till_done()
    assert preset_state(hass, 1) == STATE_UNKNOWN

    await client.simulate_notification(make_units_packet(0x01))
    await hass.async_block_till_done()
    assert float(preset_state(hass, 1)) == pytest.approx(1295.4)

    # The first unit learned is pinned, so a later change does not reinterpret.
    await client.simulate_notification(make_units_packet(0x00))
    await hass.async_block_till_done()
    assert float(preset_state(hass, 1)) == pytest.approx(1295.4)


async def test_unit_change_does_not_reinterpret_an_earlier_preset(hass, loaded_desk):
    _, client = loaded_desk
    await client.simulate_notification(make_units_packet(0x01))
    await client.simulate_notification(preset_packet(1, 500))
    await hass.async_block_till_done()
    assert float(preset_state(hass, 1)) == pytest.approx(1270.0)

    # Switching the keypad to centimeters does not change the stored preset.
    await client.simulate_notification(make_units_packet(0x00))
    await hass.async_block_till_done()
    assert float(preset_state(hass, 1)) == pytest.approx(1270.0)


async def test_stop_during_readback_delay_skips_the_preset_request(
    loaded_desk, monkeypatch
):
    entry, client = loaded_desk
    coordinator = entry.runtime_data
    fast_sleep = asyncio.sleep

    async def sleep(delay=None, *args, **kwargs):
        if delay == PRESET_REPORT_DELAY_SECONDS:
            await coordinator.async_stop_movement()
        await fast_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", sleep)

    await coordinator.async_save_preset(1)

    assert client.writes == [WAKE, WAKE, WAKE, command(0x03), command(0x2B)]


async def test_preset_heights_do_not_outlive_the_connection(hass, fake_ble):
    entry, (first, second) = await setup_desk(hass, fake_ble, extra_clients=1)
    try:
        await first.simulate_notification(preset_packet(1, 1100))
        await hass.async_block_till_done()
        assert float(preset_state(hass, 1)) == 1100.0

        first.simulate_disconnect()
        await wait_until(lambda: preset_state(hass, 1) == STATE_UNAVAILABLE)
        await wait_until(
            lambda: entry.runtime_data.is_connected
            and entry.runtime_data._desk.client is second
        )
        await hass.async_block_till_done()
        assert preset_state(hass, 1) == STATE_UNKNOWN
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("raw", "min_mm", "max_mm", "unit", "expected"),
    [
        (1100, 650, 1300, None, 1100.0),
        (300, 650, 1300, None, 762.0),
        (510, 650, 1300, None, 1295.4),
        (510, 500, 1300, None, None),
        (510, 500, 1300, DeskUnit.INCHES, 1295.4),
        (510, 500, 1300, DeskUnit.CENTIMETERS, 510.0),
        (646, 650, 1300, None, 646.0),
        (0, 650, 1300, DeskUnit.INCHES, None),
        (2000, 650, 1300, DeskUnit.INCHES, None),
    ],
    ids=[
        "mm",
        "tenths-inch",
        "limits-disambiguate",
        "ambiguous",
        "inches-tie-break",
        "centimeters-tie-break",
        "tolerance",
        "empty",
        "out-of-range",
    ],
)
def test_decode_preset_height(raw, min_mm, max_mm, unit, expected):
    decoded = decode_preset_height_mm(raw, min_mm, max_mm, unit)
    assert decoded == (None if expected is None else pytest.approx(expected))
