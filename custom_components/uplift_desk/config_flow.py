"""Config flow for the Uplift Desk integration."""

# TODO: Revert this back to installed uplift_ble package instead of local
from .uplift_ble.desk_controller import DeskController
from .uplift_ble.desk_validator import DeskValidator

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from .const import DOMAIN, BLEAK_TIMEOUT_SECONDS
from .models import DiscoveredDesk

from typing import Any

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)

import re
import voluptuous as vol

from homeassistant.helpers.selector import selector
from dataclasses import dataclass
@dataclass
class _ManualBLEDevice:
    """BLEDeviceProtocol-compatible stub for manual entry."""
    address: str
    name: str | None = None
def _validate_mac_address(value: str) -> str:
    """Validate a MAC address string.

    Accepts two formats:
      - AA:BB:CC:DD:EE:FF  (6 hex byte pairs, colon-separated)
      - AABBCCDDEEFF        (12 hex characters, no separators)

    Returns the normalized (uppercase) MAC address on success.
    Raises vol.Invalid on failure.
    """
    value = str(value).strip().upper()

    if re.fullmatch(r"[0-9A-F]{2}(:[0-9A-F]{2}){5}", value):
        return value

    if re.fullmatch(r"[0-9A-F]{12}", value):
        # Normalize 12-char format to colon-separated
        return ":".join(value[i : i + 2] for i in range(0, 12, 2))

    raise vol.Invalid(f"invalid mac address: {value}")
class UpliftDeskConfigFlow(ConfigFlow, domain=DOMAIN):
    """Uplift Desk config flow."""
    # The schema version of the entries that it creates
    # Home Assistant will call your migrate method if the version changes
    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_device: DiscoveredDesk | None = None
        self._discovered_devices: dict[
            str, tuple[DiscoveredDesk, BluetoothServiceInfoBleak]
        ] = {}
        self._manual_address: str | None = None
        self._manual_name: str | None = None

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        """Handle a flow initialized by Bluetooth discovery."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        self._discovery_info = discovery_info
        self._desk_validator = DeskValidator()
        self._discovered_device = await self._desk_validator.validate_device(discovery_info, timeout=BLEAK_TIMEOUT_SECONDS)

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

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle a flow initialized by the user."""
        # Get currently discovered Bluetooth devices
        discovered = async_discovered_service_info(self.hass)

        # Build options list from discovered devices
        device_options: list[str] = []
        for info in discovered:
            name = info.name or info.address
            device_options.append(name)

        # Always include "Manual entry" as an option
        device_options.append("Manual entry")

        if user_input is not None:
            selected = user_input["device"]

            if selected == "Manual entry":
                # Transition to manual entry step
                return await self.async_step_user_manual()
            else:
                # Find the matching BluetoothServiceInfoBleak for the selected device
                selected_info: BluetoothServiceInfoBleak | None = None
                for info in discovered:
                    info_name = info.name or info.address
                    if info_name == selected:
                        selected_info = info
                        break

                if selected_info is None:
                    return self.async_show_form(
                        step_id="user",
                        data_schema=vol.Schema({
                            vol.Required("device"): selector({
                                "select": {
                                    "options": device_options,
                                },
                            }),
                        }),
                        errors={"base": "no_device_found"},
                    )

                # Validate the selected device
                self._desk_validator = DeskValidator()
                try:
                    validated = await self._desk_validator.validate_device(selected_info, timeout=BLEAK_TIMEOUT_SECONDS)
                except (TimeoutError, Exception):
                    return self.async_show_form(
                        step_id="user",
                        data_schema=vol.Schema({
                            vol.Required("device"): selector({
                                "select": {
                                    "options": device_options,
                                },
                            }),
                        }),
                        errors={"base": "connection_failed"},
                    )

                if validated is None:
                    return self.async_show_form(
                        step_id="user",
                        data_schema=vol.Schema({
                            vol.Required("device"): selector({
                                "select": {
                                    "options": device_options,
                                },
                            }),
                        }),
                        errors={"base": "invalid_address"},
                    )

                # Device validated successfully - proceed to confirmation
                await self.async_set_unique_id(selected_info.address)
                self._abort_if_unique_id_configured()

                self._discovery_info = selected_info
                self._discovered_device = validated

                return await self.async_step_user_confirm()

        # If no devices discovered, skip straight to manual entry
        if not device_options or device_options == ["Manual entry"]:
            return await self.async_step_user_manual()

        # Show the device selection form
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("device"): selector({
                    "select": {
                        "options": device_options,
                    },
                }),
            }),
        )

    async def async_step_user_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual Bluetooth address entry."""
        if user_input is not None:
            address = user_input["address"]
            name = user_input.get("name")

            # Validate MAC address format
            try:
                address = _validate_mac_address(address)
            except vol.Invalid:
                return self.async_show_form(
                    step_id="user_manual",
                    data_schema=vol.Schema({
                        vol.Required("address"): str,
                        vol.Optional("name"): str,
                    }),
                    errors={"base": "invalid_address"},
                )

            # Construct a BLEDeviceProtocol-compatible stub for manual entry
            manual_device = _ManualBLEDevice(
                address=address,
                name=name if name else None,
            )

            # Validate the manually entered device
            self._desk_validator = DeskValidator()
            try:
                validated = await self._desk_validator.validate_device(manual_device, timeout=BLEAK_TIMEOUT_SECONDS)
            except (TimeoutError, Exception):
                return self.async_show_form(
                    step_id="user_manual",
                    data_schema=vol.Schema({
                        vol.Required("address"): str,
                        vol.Optional("name"): str,
                    }),
                    errors={"base": "connection_failed"},
                )

            if validated is None:
                return self.async_show_form(
                    step_id="user_manual",
                    data_schema=vol.Schema({
                        vol.Required("address"): str,
                        vol.Optional("name"): str,
                    }),
                    errors={"base": "invalid_address"},
                )

            # Validation succeeded - capture the real name from the validated device
            await self.async_set_unique_id(validated.address)
            self._abort_if_unique_id_configured()

            self._discovered_device = validated
            self._manual_address = validated.address
            self._manual_name = validated.name

            return await self.async_step_user_confirm()

        # Show the manual entry form
        return self.async_show_form(
            step_id="user_manual",
            data_schema=vol.Schema({
                vol.Required("address"): str,
                vol.Optional("name"): str,
            }),
        )

    async def async_step_user_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm device details before creating the config entry."""
        assert self._discovered_device is not None
        device = self._discovered_device

        # Determine the name and address to display
        if self._manual_name:
            name = self._manual_name
            address = self._manual_address or device.address
        else:
            name = device.name
            address = device.address

        if user_input is not None:
            return self.async_create_entry(
                title=name,
                data={"address": address, "name": name},
            )

        # This is a confirmation-only step - suppress the back button
        self._set_confirm_only()
        placeholders = {"name": name, "address": address}
        self.context["title_placeholders"] = placeholders
        return self.async_show_form(
            step_id="user_confirm",
            description_placeholders=placeholders,
        )
