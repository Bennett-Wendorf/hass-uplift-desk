"""Config flow for the Uplift Desk integration."""

from uplift_ble.desk import Desk

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from .const import DOMAIN

from typing import Any

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)

class UpliftDeskConfigFlow(ConfigFlow, domain=DOMAIN):
    """Uplift Desk config flow."""
    # The schema version of the entries that it creates
    # Home Assistant will call your migrate method if the version changes
    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_device: Desk | None = None
        self._discovered_devices: dict[
            str, tuple[Desk, BluetoothServiceInfoBleak]
        ] = {}

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        """Handle a discovered Bluetooth device."""

        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        self._discovery_info = discovery_info
        self._discovered_device = Desk(discovery_info.address)
        
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm discovery."""
        assert self._discovered_device is not None
        device = self._discovered_device
        assert self._discovery_info is not None
        discovery_info = self._discovery_info
        title = discovery_info.name
        if user_input is not None:
            return self.async_create_entry(
                title=title, data={"address": discovery_info.address, "name": discovery_info.name}
            )

        self._set_confirm_only()
        placeholders = {"name": title}
        self.context["title_placeholders"] = placeholders
        return self.async_show_form(
            step_id="bluetooth_confirm", description_placeholders=placeholders
        )

    # TODO: Allow config flow to work manually as well