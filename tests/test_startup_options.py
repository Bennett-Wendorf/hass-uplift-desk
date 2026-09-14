"""Notification-only setup and reconnect through the real HA entry lifecycle."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_ADDRESS
from homeassistant.data_entry_flow import FlowResultType

from custom_components.uplift_desk.const import (
    CONF_FALLBACK_UNIT,
    CONF_QUERY_ON_CONNECT,
)
from custom_components.uplift_desk.sensor import DeskHeightSensor

from .conftest import DESK_ADDRESS, DESK_DOMAIN, DESK_NAME, wait_until
from .test_fallback_unit import make_entry
from .test_notifications import make_height_packet


@pytest.fixture(autouse=True)
def stub_bluetooth_setup(monkeypatch):
    """Prevent the HA test harness from opening the host Bluetooth stack."""
    monkeypatch.setattr(
        "homeassistant.components.bluetooth.async_setup", AsyncMock(return_value=True)
    )


@pytest.mark.parametrize("query_on_connect", [None, True, False])
async def test_setup_and_reconnect_obey_query_option(hass, fake_ble, query_on_connect):
    """Disabled queries send no commands while real height notifications work."""
    options = {CONF_FALLBACK_UNIT: "centimeters"}
    if query_on_connect is not None:
        options[CONF_QUERY_ON_CONNECT] = query_on_connect
    entry = make_entry(hass, options=options)
    first, second = fake_ble.valid_client(), fake_ble.valid_client()
    fake_ble.queue_client(first)
    fake_ble.queue_client(second)
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        coordinator = entry.runtime_data
        sensor = DeskHeightSensor(coordinator)
        assert sensor.native_value is None
        for index, client in enumerate((first, second)):
            if index:
                first.simulate_disconnect()
                await wait_until(
                    lambda: coordinator.is_connected
                    and coordinator._desk.client is second
                    and coordinator._reconnect_task is None
                )
                assert coordinator._desk.client is second
            opcodes = [packet[2] for _, packet, _ in client.writes]
            if query_on_connect is False:
                assert opcodes == []
            else:
                # Defaults preserve the released library's unit and limits queries.
                assert opcodes.count(0x0E) == 1
                assert opcodes.count(0x07) == 1
            assert len(client.start_notify_calls) == 1
            await client.simulate_notification(make_height_packet(750 + index))
            await wait_until(lambda: sensor.native_value == 750 + index)
        assert fake_ble.establish.call_count == 2
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_options_reload_to_notification_only_and_preserve_selection(hass, fake_ble):
    """The real options flow applies the switch and retains it on later edits."""
    entry = make_entry(hass, options={CONF_FALLBACK_UNIT: "centimeters"})
    clients = [fake_ble.valid_client() for _ in range(3)]
    for client in clients:
        fake_ble.queue_client(client)
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert clients[0].writes
        for selection in (
            {CONF_FALLBACK_UNIT: "centimeters", CONF_QUERY_ON_CONNECT: False},
            {CONF_FALLBACK_UNIT: "inches"},
        ):
            old = entry.runtime_data
            result = await hass.config_entries.options.async_init(entry.entry_id)
            await hass.config_entries.options.async_configure(
                result["flow_id"], user_input=selection
            )
            await hass.async_block_till_done()
            assert entry.state is ConfigEntryState.LOADED
            assert entry.runtime_data is not old
            assert not old.is_connected
            assert entry.options[CONF_QUERY_ON_CONNECT] is False
            assert entry.runtime_data._desk.client.writes == []
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["data_schema"]({})[CONF_QUERY_ON_CONNECT] is False
        await clients[-1].simulate_notification(make_height_packet(300))
        await wait_until(lambda: entry.runtime_data.height_mm == 762.0)
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("source", ["user", "bluetooth"])
async def test_initial_setup_can_disable_queries_before_first_connection(
    hass, fake_ble, monkeypatch, source
):
    """Both confirmation paths persist the choice before entry setup runs."""
    monkeypatch.setattr(
        "custom_components.uplift_desk.config_flow.async_discovered_service_info",
        lambda hass: [],
    )
    monkeypatch.setattr(
        "custom_components.uplift_desk.config_flow.DeskValidator.validate_device",
        AsyncMock(return_value=fake_ble.device),
    )
    with patch("custom_components.uplift_desk.async_setup_entry", AsyncMock(return_value=True)):
        data = SimpleNamespace(address=DESK_ADDRESS, name=DESK_NAME) if source == "bluetooth" else None
        result = await hass.config_entries.flow.async_init(
            DESK_DOMAIN, context={"source": source}, data=data
        )
        if source == "user":
            assert result["step_id"] == "user_manual"
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], user_input={CONF_ADDRESS: DESK_ADDRESS, "name": DESK_NAME}
            )
        assert result["step_id"] == f"{source}_confirm"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_FALLBACK_UNIT: "centimeters", CONF_QUERY_ON_CONNECT: False},
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].options == {
        CONF_FALLBACK_UNIT: "centimeters", CONF_QUERY_ON_CONNECT: False,
    }
