"""Fallback options through the real HA flow manager and BLE controller."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_ADDRESS
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from uplift_ble.desk_enums import DeskUnit

from custom_components.uplift_desk import async_migrate_entry
from custom_components.uplift_desk.const import CONF_FALLBACK_UNIT, FALLBACK_UNIT_NONE
from custom_components.uplift_desk.coordinator import UpliftDeskBluetoothCoordinator
from custom_components.uplift_desk.sensor import DeskHeightSensor

from .conftest import DESK_ADDRESS, DESK_DOMAIN, DESK_NAME, wait_until
from .test_notifications import make_height_packet, make_units_packet


@pytest.fixture(autouse=True)
def stub_bluetooth_setup(monkeypatch):
    """Keep HA dependency loading from opening the host Bluetooth stack."""
    monkeypatch.setattr(
        "homeassistant.components.bluetooth.async_setup", AsyncMock(return_value=True)
    )


def make_entry(hass, *, options=None, minor_version=2, version=1):
    entry = MockConfigEntry(
        domain=DESK_DOMAIN,
        title=DESK_NAME,
        data={CONF_ADDRESS: DESK_ADDRESS},
        options=options or {},
        version=version,
        minor_version=minor_version,
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, {CONF_FALLBACK_UNIT: "centimeters"}),
        ({"other": True}, {"other": True, CONF_FALLBACK_UNIT: "centimeters"}),
        ({CONF_FALLBACK_UNIT: "none"}, {CONF_FALLBACK_UNIT: "none"}),
        ({CONF_FALLBACK_UNIT: "inches"}, {CONF_FALLBACK_UNIT: "inches"}),
    ],
)
async def test_migration_preserves_existing_options(hass, options, expected):
    entry = make_entry(hass, options=options, minor_version=1)
    assert await async_migrate_entry(hass, entry)
    assert entry.options == expected
    assert (entry.version, entry.minor_version) == (1, 2)
    assert await async_migrate_entry(hass, entry)
    assert entry.options == expected


async def test_future_major_migration_is_rejected(hass):
    entry = make_entry(hass, version=2)
    assert not await async_migrate_entry(hass, entry)
    assert entry.version == 2
    assert entry.options == {}


@pytest.mark.parametrize("selection", ["none", "centimeters", "inches"])
async def test_options_manager_saves_and_reloads(hass, fake_ble, selection):
    initial = "inches" if selection != "inches" else "none"
    entry = make_entry(hass, options={CONF_FALLBACK_UNIT: initial, "other": True})
    fake_ble.queue_client(fake_ble.valid_client())
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)) as reload:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        assert result["data_schema"]({}) == {CONF_FALLBACK_UNIT: initial}
        with pytest.raises(vol.Invalid):
            result["data_schema"]({CONF_FALLBACK_UNIT: "meters"})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={CONF_FALLBACK_UNIT: selection}
        )
        await hass.async_block_till_done()
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options == {CONF_FALLBACK_UNIT: selection, "other": True}
        reload.assert_awaited_once_with(entry.entry_id)
    await hass.config_entries.async_unload(entry.entry_id)


async def test_unchanged_options_do_not_reload(hass, fake_ble):
    entry = make_entry(hass, options={CONF_FALLBACK_UNIT: "centimeters"})
    fake_ble.queue_client(fake_ble.valid_client())
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={CONF_FALLBACK_UNIT: "centimeters"}
        )
        await hass.async_block_till_done()
        reload.assert_not_awaited()
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("fallback", [None, "none", "centimeters", "inches", "invalid"])
async def test_height_fallback_and_late_report(hass, fake_ble, fallback):
    options = {} if fallback is None else {CONF_FALLBACK_UNIT: fallback}
    entry = make_entry(hass, options=options)
    coordinator = UpliftDeskBluetoothCoordinator(hass, entry, fake_ble.device)
    client = fake_ble.valid_client()
    fake_ble.queue_client(client)
    try:
        await coordinator.async_connect()
        sensor = DeskHeightSensor(coordinator)
        await coordinator.async_read_desk_units()
        assert coordinator._desk.unit is None
        await client.simulate_notification(make_height_packet(300))
        # Process notifications even when no fallback means there is no HEIGHT event.
        for _ in range(5):
            await asyncio.sleep(0)
        expected = {"centimeters": 300.0, "inches": 762.0}.get(fallback)
        assert coordinator.height_mm == expected
        assert sensor.native_value == expected

        # A later, conflicting real unit report reinterprets cached raw height.
        reported_byte = 0 if fallback == "inches" else 1
        expected = 300.0 if reported_byte == 0 else 762.0
        await client.simulate_notification(make_units_packet(reported_byte))
        await wait_until(lambda: sensor.native_value == expected)
        assert await coordinator.async_read_desk_units() is (
            DeskUnit.CENTIMETERS if reported_byte == 0 else DeskUnit.INCHES
        )
    finally:
        await coordinator.async_disconnect()


async def test_fallback_survives_replacement_controller(hass, fake_ble):
    entry = make_entry(hass, options={CONF_FALLBACK_UNIT: "inches"})
    coordinator = UpliftDeskBluetoothCoordinator(hass, entry, fake_ble.device)
    first, second = fake_ble.valid_client(), fake_ble.valid_client()
    fake_ble.queue_client(first)
    fake_ble.queue_client(second)
    try:
        await coordinator.async_connect()
        old = coordinator._desk
        first.simulate_disconnect()
        await wait_until(lambda: coordinator.is_connected and coordinator._desk.client is second)
        assert coordinator._desk is not old
        await second.simulate_notification(make_height_packet(300))
        await wait_until(lambda: coordinator.height_mm == 762.0)
        assert coordinator._desk.unit is None
    finally:
        await coordinator.async_disconnect()


async def test_changed_options_reload_real_entry(hass, fake_ble):
    """HA unloads the old controller and sets up the new option through its manager."""
    entry = make_entry(hass, options={CONF_FALLBACK_UNIT: "centimeters"})
    first, second = fake_ble.valid_client(), fake_ble.valid_client()
    fake_ble.queue_client(first)
    fake_ble.queue_client(second)
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        old = entry.runtime_data
        await first.simulate_notification(make_height_packet(300))
        await wait_until(lambda: old.height_mm == 300.0)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={CONF_FALLBACK_UNIT: "inches"}
        )
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert entry.runtime_data is not old
        assert not first.is_connected
        assert entry.runtime_data._desk.client is second
        await second.simulate_notification(make_height_packet(300))
        await wait_until(lambda: entry.runtime_data.height_mm == 762.0)
        assert entry.runtime_data._desk.unit is None
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_new_entry_has_no_fallback(hass, fake_ble, monkeypatch):
    """The existing manual flow creates version 1.2 without an implicit unit."""
    monkeypatch.setattr(
        "custom_components.uplift_desk.config_flow.async_discovered_service_info",
        lambda hass: [],
    )
    monkeypatch.setattr(
        "custom_components.uplift_desk.config_flow.DeskValidator.validate_device",
        AsyncMock(return_value=fake_ble.device),
    )
    with patch("custom_components.uplift_desk.async_setup_entry", AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_init(
            DESK_DOMAIN, context={"source": "user"}
        )
        assert result["step_id"] == "user_manual"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_ADDRESS: DESK_ADDRESS, "name": DESK_NAME}
        )
        assert result["step_id"] == "user_confirm"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert (entry.version, entry.minor_version) == (1, 2)
    assert await async_migrate_entry(hass, entry)
    assert CONF_FALLBACK_UNIT not in entry.options
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["data_schema"]({}) == {CONF_FALLBACK_UNIT: FALLBACK_UNIT_NONE}
