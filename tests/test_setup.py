"""Tests for config-entry setup failure handling.

An unreachable desk at setup time must surface as ``ConfigEntryNotReady``
(the entry shows "retrying"), never as a raw ``BleakError``.
"""

from __future__ import annotations

import pytest
from bleak import BleakError
from homeassistant.config_entries import ConfigEntryNotReady, ConfigEntryState
from homeassistant.const import CONF_ADDRESS

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.uplift_desk import async_setup_entry

from .conftest import DESK_ADDRESS, DESK_DOMAIN, DESK_NAME


def _make_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DESK_DOMAIN,
        data={CONF_ADDRESS: DESK_ADDRESS},
        title=DESK_NAME,
    )


async def test_setup_with_unreachable_desk_raises_config_entry_not_ready(
    hass, fake_ble
):
    """async_setup_entry converts a connect BleakError to ConfigEntryNotReady."""
    fake_ble.establish.fail_with = BleakError("could not connect to desk")
    entry = _make_entry()

    with pytest.raises(ConfigEntryNotReady):
        await async_setup_entry(hass, entry)


async def test_setup_with_unreachable_desk_entry_ends_retrying(
    hass, fake_ble, monkeypatch
):
    """Through the real config-entry machinery the entry ends in SETUP_RETRY."""
    # The manifest's bluetooth_adapters dependency would open a real BlueZ
    # mgmt socket during component setup; this test only exercises the
    # entry setup, so stub the bluetooth component (no-op success).
    async def _stub_bluetooth_setup(hass, config):
        return True

    monkeypatch.setattr(
        "homeassistant.components.bluetooth.async_setup", _stub_bluetooth_setup
    )

    fake_ble.establish.fail_with = BleakError("could not connect to desk")
    entry = _make_entry()
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    # The connect was attempted (and failed) exactly once for this setup.
    assert fake_ble.establish.call_count == 1
