"""Preset entities and recall commands through HA and the real BLE controller."""

from unittest.mock import AsyncMock

import pytest
from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from uplift_ble.desk_configs import DESK_CONFIGS_BY_SERVICE

from .conftest import (
    DESK_ADDRESS,
    DESK_DOMAIN,
    DESK_NAME,
    build_service_collection,
    wait_until,
)


@pytest.fixture(autouse=True)
def stub_bluetooth_setup(monkeypatch):
    """Keep HA dependency loading from opening the host Bluetooth stack."""
    monkeypatch.setattr(
        "homeassistant.components.bluetooth.async_setup", AsyncMock(return_value=True)
    )


def make_entry(hass):
    entry = MockConfigEntry(
        domain=DESK_DOMAIN,
        title=DESK_NAME,
        data={CONF_ADDRESS: DESK_ADDRESS},
        version=1,
        minor_version=2,
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.parametrize(
    ("service_uuid", "expected_slots"),
    [
        ("000000ff-0000-1000-8000-00805f9b34fb", (1, 2, 3, 4)),
        ("0000fe60-0000-1000-8000-00805f9b34fb", (1, 2, 3, 4)),
        ("0000ff00-0000-1000-8000-00805f9b34fb", (1, 2, 3, 4)),
        ("0000ff12-0000-1000-8000-00805f9b34fb", (1, 2)),
    ],
    ids=["00ff", "fe60", "ff00", "ff12"],
)
async def test_presets_follow_connected_gatt_profile(
    hass, fake_ble, service_uuid, expected_slots
):
    """A desk advertising 00FF can expose FF00 once connected; retain its presets."""
    config = DESK_CONFIGS_BY_SERVICE[service_uuid]
    fake_ble.queue_client(fake_ble.client_with_services(build_service_collection(config)))
    entry = make_entry(hass)
    registry = er.async_get(hass)
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        buttons = {
            entity.unique_id: entity
            for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
            if entity.domain == "button"
        }
        assert set(buttons) == {
            f"{DESK_ADDRESS}_desk_preset_{slot}" for slot in expected_slots
        }
        for slot in expected_slots:
            entity = buttons[f"{DESK_ADDRESS}_desk_preset_{slot}"]
            assert entity.disabled_by is (
                er.RegistryEntryDisabler.INTEGRATION if slot > 2 else None
            )
        assert fake_ble.establish.call_count == 1
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_existing_ff00_presets_keep_ids_and_recall_after_reconnect(hass, fake_ble):
    """Previously enabled presets survive upgrade and send recalls on the new client."""
    config = DESK_CONFIGS_BY_SERVICE["0000ff00-0000-1000-8000-00805f9b34fb"]
    first = fake_ble.client_with_services(build_service_collection(config))
    second = fake_ble.client_with_services(build_service_collection(config))
    fake_ble.queue_client(first)
    fake_ble.queue_client(second)
    entry = make_entry(hass)
    registry = er.async_get(hass)
    existing = {
        slot: registry.async_get_or_create(
            "button",
            DESK_DOMAIN,
            f"{DESK_ADDRESS}_desk_preset_{slot}",
            config_entry=entry,
            suggested_object_id=f"custom_desk_preset_{slot}",
        )
        for slot in (3, 4)
    }
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        for client in (first, second):
            if client is second:
                first.simulate_disconnect()
                await wait_until(
                    lambda: entry.runtime_data.is_connected
                    and entry.runtime_data._desk.client is second
                )
                await hass.async_block_till_done()
            for slot, registered in existing.items():
                current = registry.async_get(registered.entity_id)
                assert current.id == registered.id
                assert current.disabled_by is None
                state = hass.states.get(registered.entity_id)
                assert state is not None and state.state != "unavailable"
                client.writes.clear()
                await hass.services.async_call(
                    "button", "press", {"entity_id": registered.entity_id}, blocking=True
                )
                # Wake frames may precede recall; only recall may carry a preset opcode.
                recalls = [write for write in client.writes if write[1][2] != 0x00]
                opcode = 0x27 if slot == 3 else 0x28
                assert recalls == [
                    (
                        config.input_char_uuid,
                        bytes((0xF1, 0xF1, opcode, 0x00, opcode, 0x7E)),
                        False,
                    )
                ]
        assert fake_ble.establish.call_count == 2
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
