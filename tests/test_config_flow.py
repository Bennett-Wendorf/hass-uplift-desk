"""Tests for manual desk entry errors in the config flow.

A well-formed address whose desk cannot be validated must not be reported
as an invalid address; only a malformed address gets that error.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType

from custom_components.uplift_desk.config_flow import UpliftDeskConfigFlow

from .conftest import DESK_ADDRESS


def _make_flow(hass) -> UpliftDeskConfigFlow:
    flow = UpliftDeskConfigFlow()
    flow.hass = hass
    flow.context = {"source": SOURCE_USER}
    return flow


async def test_manual_entry_malformed_address_reports_invalid_address(hass):
    flow = _make_flow(hass)

    result = await flow.async_step_user_manual({"address": "not-an-address"})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_address"}


async def test_manual_entry_unvalidated_desk_is_not_reported_as_invalid_address(hass):
    flow = _make_flow(hass)

    with patch(
        "custom_components.uplift_desk.config_flow.DeskValidator.validate_device",
        AsyncMock(return_value=None),
    ):
        result = await flow.async_step_user_manual({"address": DESK_ADDRESS})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "desk_not_validated"}
