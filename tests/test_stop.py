"""Targeted Stop and motion ordering against HA and the real 0.7 controller."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from bleak import BleakError
from homeassistant.const import CONF_ADDRESS, STATE_UNAVAILABLE
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from uplift_ble.desk_configs import DESK_CONFIGS_BY_SERVICE

from .conftest import FakeBLEDevice, build_service_collection, wait_until
from .test_preset_buttons import make_entry


CONFIG = DESK_CONFIGS_BY_SERVICE["0000ff00-0000-1000-8000-00805f9b34fb"]
STOP_PACKET = bytes((0xF1, 0xF1, 0x2B, 0, 0x2B, 0x7E))


def stop_entity_id(hass, entry):
    """Resolve the desk's Stop button through its stable registry identity."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "button", "uplift_desk", f"{entry.data['address']}_desk_stop"
    )
    assert entity_id is not None
    return entity_id


async def press_stop(hass, entry):
    """Use the same HA action as the UI, automations, and Companion."""
    await hass.services.async_call(
        "button", "press", {"entity_id": stop_entity_id(hass, entry)}, blocking=True
    )


@pytest.fixture
async def loaded_desk(hass, fake_ble, monkeypatch):
    """Load one connected FF00 desk, preserving the production setup path."""
    monkeypatch.setattr(
        "homeassistant.components.bluetooth.async_setup", AsyncMock(return_value=True)
    )
    entry = make_entry(hass)
    client = fake_ble.client_with_services(build_service_collection(CONFIG))
    fake_ble.queue_client(client)
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        client.writes.clear()
        yield entry, client
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_stop_button_sends_only_stop_without_height(
    hass, fake_ble, loaded_desk
):
    entry, client = loaded_desk
    registry = er.async_get(hass)
    before = {
        entity.entity_id: entity.id
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert entry.runtime_data.height_mm is None
    attempts = fake_ble.establish.call_count

    stop = registry.async_get(stop_entity_id(hass, entry))
    assert stop.disabled_by is None
    assert stop.device_id == registry.async_get(
        registry.async_get_entity_id(
            "sensor", "uplift_desk", f"{entry.data['address']}_desk_height"
        )
    ).device_id
    assert not hass.services.has_service("uplift_desk", "stop")

    await press_stop(hass, entry)

    assert client.writes == [(CONFIG.input_char_uuid, STOP_PACKET, False)]
    assert fake_ble.establish.call_count == attempts
    assert {
        entity.entity_id: entity.id
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
    } == before


async def test_unknown_stop_target_does_not_broadcast(hass, loaded_desk):
    _, client = loaded_desk
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.missing_stop"}, blocking=True
    )
    assert client.writes == []


async def test_unloaded_stop_target_does_not_connect(hass, fake_ble, loaded_desk):
    entry, client = loaded_desk
    assert await hass.config_entries.async_unload(entry.entry_id)
    attempts = fake_ble.establish.call_count
    await press_stop(hass, entry)
    assert client.writes == []
    assert fake_ble.establish.call_count == attempts


async def test_stop_button_only_writes_to_the_selected_desk(
    hass, fake_ble, loaded_desk, monkeypatch
):
    first_entry, first = loaded_desk
    other_address = "AA:BB:CC:DD:EE:02"
    other_device = FakeBLEDevice(other_address, "Other desk")

    def resolve_device(hass, address):
        if address == other_address:
            return other_device
        return fake_ble.device_from_address(hass, address)

    monkeypatch.setattr(
        "custom_components.uplift_desk.async_ble_device_from_address", resolve_device
    )
    monkeypatch.setattr(
        "custom_components.uplift_desk.coordinator.async_ble_device_from_address",
        resolve_device,
    )
    other_entry = MockConfigEntry(
        domain="uplift_desk",
        title="Other desk",
        data={CONF_ADDRESS: other_address},
        version=1,
        minor_version=2,
    )
    other_entry.add_to_hass(hass)
    second = fake_ble.client_with_services(build_service_collection(CONFIG))
    fake_ble.queue_client(second)
    try:
        assert await hass.config_entries.async_setup(other_entry.entry_id)
        await hass.async_block_till_done()
        first.writes.clear()
        second.writes.clear()

        await press_stop(hass, first_entry)

        assert first.writes == [(CONFIG.input_char_uuid, STOP_PACKET, False)]
        assert second.writes == []
        first.writes.clear()

        await press_stop(hass, other_entry)

        assert first.writes == []
        assert second.writes == [(CONFIG.input_char_uuid, STOP_PACKET, False)]
    finally:
        await hass.config_entries.async_unload(other_entry.entry_id)


@pytest.mark.parametrize("motion", ["preset", "height"])
async def test_stop_button_cancels_motion_during_bluetooth_reconnect(
    hass, fake_ble, loaded_desk, monkeypatch, motion
):
    entry, first = loaded_desk
    coordinator = entry.runtime_data
    second = fake_ble.client_with_services(build_service_collection(CONFIG))
    fake_ble.queue_client(second)
    connecting = asyncio.Event()
    release = asyncio.Event()

    async def held_connect(*args, **kwargs):
        connecting.set()
        await release.wait()
        return await fake_ble.establish(*args, **kwargs)

    monkeypatch.setattr(
        "custom_components.uplift_desk.coordinator.establish_connection", held_connect
    )
    pending = None
    try:
        first.simulate_disconnect()
        await asyncio.wait_for(connecting.wait(), 3)
        assert not coordinator.is_connected
        assert hass.states.get(stop_entity_id(hass, entry)).state != STATE_UNAVAILABLE
        preset_id = er.async_get(hass).async_get_entity_id(
            "button", "uplift_desk", f"{entry.data[CONF_ADDRESS]}_desk_preset_1"
        )
        assert hass.states.get(preset_id).state == STATE_UNAVAILABLE
        pending = asyncio.create_task(
            coordinator.async_preset_2()
            if motion == "preset"
            else coordinator.async_move_to_height(900)
        )
        await asyncio.sleep(0)
        attempts = fake_ble.establish.call_count

        with pytest.raises(HomeAssistantError, match="no Stop packet"):
            await press_stop(hass, entry)

        assert fake_ble.establish.call_count == attempts
        assert first.writes == []
        assert coordinator.height_setpoint_mm is None
        release.set()
        await pending
        await hass.async_block_till_done()
        assert coordinator.is_connected
        assert not any(packet[2] in (0x06, 0x1B, 0x2B) for _, packet, _ in second.writes)
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)


async def test_stop_button_keeps_renamed_entity_and_device_after_reload(
    hass, fake_ble, loaded_desk
):
    entry, _ = loaded_desk
    registry = er.async_get(hass)
    original = registry.async_update_entity(
        stop_entity_id(hass, entry), new_entity_id="button.custom_desk_stop"
    )
    await hass.async_block_till_done()
    second = fake_ble.client_with_services(build_service_collection(CONFIG))
    fake_ble.queue_client(second)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    current = registry.async_get(stop_entity_id(hass, entry))
    assert current.entity_id == original.entity_id
    assert current.id == original.id
    assert current.device_id == original.device_id
    second.writes.clear()
    await press_stop(hass, entry)
    assert second.writes == [(CONFIG.input_char_uuid, STOP_PACKET, False)]


@pytest.mark.parametrize("waiting_for", ["wake", "controller"])
@pytest.mark.parametrize("motion", ["preset", "height"])
async def test_stop_cancels_motion_waiting_before_its_write(
    loaded_desk, monkeypatch, waiting_for, motion
):
    entry, client = loaded_desk
    coordinator = entry.runtime_data
    controller = coordinator._desk
    entered = asyncio.Event()
    release = asyncio.Event()

    async def wait_before_write():
        entered.set()
        await release.wait()
        return controller

    if waiting_for == "wake":
        monkeypatch.setattr(controller, "wake", wait_before_write)
    else:
        monkeypatch.setattr(
            coordinator, "_get_or_establish_controller", wait_before_write
        )
    pending = asyncio.create_task(
        coordinator.async_preset_2()
        if motion == "preset"
        else coordinator.async_move_to_height(900)
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await coordinator.async_stop_movement()
        release.set()
        await pending
        assert client.writes == [(CONFIG.input_char_uuid, STOP_PACKET, False)]
        assert coordinator.height_setpoint_mm is None
    finally:
        release.set()
        await pending


@pytest.mark.parametrize("motion, opcode", [("preset", 0x05), ("height", 0x1B)])
async def test_stop_follows_an_in_flight_write_and_cancels_a_queued_recall(
    loaded_desk, monkeypatch, motion, opcode
):
    entry, client = loaded_desk
    coordinator = entry.runtime_data
    entered = asyncio.Event()
    release = asyncio.Event()
    original_write = client.write_gatt_char
    monkeypatch.setattr(coordinator._desk, "wake", AsyncMock())

    async def held_write(characteristic, packet, response=False):
        if packet[2] == opcode:
            entered.set()
            await release.wait()
        await original_write(characteristic, packet, response=response)

    monkeypatch.setattr(client, "write_gatt_char", held_write)
    first = asyncio.create_task(
        coordinator.async_preset_1()
        if motion == "preset"
        else coordinator.async_move_to_height(900)
    )
    tasks = [first]
    try:
        await asyncio.wait_for(entered.wait(), 3)
        tasks.append(asyncio.create_task(coordinator.async_preset_2()))
        await asyncio.sleep(0)
        tasks.append(asyncio.create_task(coordinator.async_stop_movement()))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(*tasks)
        assert [packet[2] for _, packet, _ in client.writes] == [opcode, 0x2B]
        assert coordinator.height_setpoint_mm is None
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_disconnected_stop_never_arrives_after_automatic_reconnect(
    hass, fake_ble, loaded_desk
):
    entry, first = loaded_desk
    second = fake_ble.client_with_services(build_service_collection(CONFIG))
    fake_ble.queue_client(second)
    first.simulate_disconnect()
    attempts = fake_ble.establish.call_count

    with pytest.raises(HomeAssistantError, match="no Stop packet"):
        await entry.runtime_data.async_stop_movement()
    assert fake_ble.establish.call_count == attempts
    assert first.writes == []

    await wait_until(
        lambda: entry.runtime_data.is_connected
        and entry.runtime_data._desk.client is second
    )
    await hass.async_block_till_done()
    assert not any(packet[2] == 0x2B for _, packet, _ in second.writes)


async def test_failed_stop_write_does_not_retry_or_reconnect(
    fake_ble, loaded_desk, monkeypatch
):
    entry, client = loaded_desk
    attempts = fake_ble.establish.call_count
    write = AsyncMock(side_effect=BleakError("write failed"))
    monkeypatch.setattr(client, "write_gatt_char", write)

    with pytest.raises(HomeAssistantError, match="keypad"):
        await entry.runtime_data.async_stop_movement()
    write.assert_awaited_once_with(CONFIG.input_char_uuid, STOP_PACKET, response=False)
    assert fake_ble.establish.call_count == attempts


async def test_waiting_stop_does_not_transfer_to_a_replacement_connection(
    fake_ble, loaded_desk
):
    """A Stop waiting behind a write belongs only to its original BLE session."""
    entry, first = loaded_desk
    coordinator = entry.runtime_data
    second = fake_ble.client_with_services(build_service_collection(CONFIG))
    fake_ble.queue_client(second)
    await coordinator._motion_write_lock.acquire()
    pending = asyncio.create_task(coordinator.async_stop_movement())
    try:
        await asyncio.sleep(0)
        assert not pending.done()
        first.simulate_disconnect()
        await wait_until(
            lambda: coordinator.is_connected
            and coordinator._desk.client is second
            and coordinator._reconnect_task is None
        )
        coordinator._motion_write_lock.release()
        with pytest.raises(HomeAssistantError, match="no Stop packet"):
            await pending
        assert not any(packet[2] == 0x2B for _, packet, _ in second.writes)
    finally:
        if coordinator._motion_write_lock.locked():
            coordinator._motion_write_lock.release()
        await asyncio.gather(pending, return_exceptions=True)


async def test_a_later_explicit_preset_keeps_the_stock_wake_sequence(loaded_desk):
    entry, client = loaded_desk
    await entry.runtime_data.async_stop_movement()
    await entry.runtime_data.async_preset_3()
    assert [packet[2] for _, packet, _ in client.writes] == [0x2B, 0, 0, 0, 0x27]


@pytest.mark.parametrize("motion", ["preset", "height"])
async def test_unload_cancels_motion_that_is_still_waking(
    hass, loaded_desk, monkeypatch, motion
):
    entry, client = loaded_desk
    coordinator = entry.runtime_data
    entered = asyncio.Event()
    release = asyncio.Event()

    async def held_wake():
        entered.set()
        await release.wait()

    monkeypatch.setattr(coordinator._desk, "wake", held_wake)
    pending = asyncio.create_task(
        coordinator.async_preset_4()
        if motion == "preset"
        else coordinator.async_move_to_height(900)
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert await hass.config_entries.async_unload(entry.entry_id)
        release.set()
        await pending
        assert client.writes == []
        assert coordinator.height_setpoint_mm is None
    finally:
        release.set()
        await pending


async def test_stop_clears_height_setpoint_entity(hass, loaded_desk):
    entry, client = loaded_desk
    entity_id = er.async_get(hass).async_get_entity_id(
        "number", "uplift_desk", f"{entry.runtime_data.desk_address}_desk_height_setpoint"
    )
    await entry.runtime_data.async_move_to_height(900)
    assert hass.states.get(entity_id).state == "900"
    client.writes.clear()

    await press_stop(hass, entry)

    assert client.writes == [(CONFIG.input_char_uuid, STOP_PACKET, False)]
    assert hass.states.get(entity_id).state == "unknown"


async def test_failed_height_write_after_stop_does_not_restore_old_target(
    loaded_desk, monkeypatch
):
    entry, client = loaded_desk
    coordinator = entry.runtime_data
    await coordinator.async_move_to_height(800)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def failed_wake():
        entered.set()
        await release.wait()
        raise BleakError("wake failed")

    monkeypatch.setattr(coordinator._desk, "wake", failed_wake)
    pending = asyncio.create_task(coordinator.async_move_to_height(900))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await coordinator.async_stop_movement()
        release.set()
        with pytest.raises(BleakError, match="wake failed"):
            await pending
        assert coordinator.height_setpoint_mm is None
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_a_later_explicit_height_command_keeps_the_stock_packet_and_wake(
    loaded_desk
):
    entry, client = loaded_desk
    await entry.runtime_data.async_stop_movement()
    await entry.runtime_data.async_move_to_height(900)
    assert [packet[2] for _, packet, _ in client.writes] == [0x2B, 0, 0, 0, 0x1B]
    assert client.writes[-1][1][4:6] == (900).to_bytes(2, "big")
    assert entry.runtime_data.height_setpoint_mm == 900
