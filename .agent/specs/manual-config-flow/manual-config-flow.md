# Manual Config Flow Implementation Plan

## Overview

Currently, the Uplift Desk integration only supports Bluetooth discovery-based setup. This document outlines the plan to implement a manual configuration flow to allow users to add Uplift desks that are not being discovered automatically.

## Current State Analysis

### Discovery Flow (Working)
- Uses Bluetooth discovery via `async_step_bluetooth`
- Validates devices using `DeskValidator`
- Stores device address and name in config entry
- Unique ID is set to Bluetooth MAC address

### Manual Config Flow (`async_step_user`) — Current Stubs

The `async_step_user` method in `config_flow.py` is **not yet implemented**. It currently contains only placeholder/stub form fields that need to be replaced:

```python
async def async_step_user(self, user_input=None):
    """Handle a flow initialized by the user."""
    data_schema = {
        vol.Required("test1"): str,
        vol.Required("test2"): str
    }

    if self.show_advanced_options:
        data_schema[vol.Optional("test3")] = selector({
            "select": {
                "options": ["all", "light", "switch"],
            }
        })

    return self.async_show_form(step_id="user", data_schema=vol.Schema(data_schema))
```

**Baseline state summary:**
- No Bluetooth scan functionality exists — `self._discovered_devices` is always empty
- Form has two placeholder required fields (`test1`, `test2`) and one optional field (`test3`)
- These placeholder fields have no validation, no user-facing labels, and serve no purpose
- No multi-step flow, no manual address entry, no device validation, no confirmation step
- This stub was left from initial scaffold/development and must be completely replaced

### Code Locations
- Config flow logic: `/custom_components/uplift_desk/config_flow.py`
- Device validator: `/custom_components/uplift_desk/uplift_ble/desk_validator.py`
- Desk controller: `/custom_components/uplift_desk/uplift_ble/desk_controller.py`
- Models: `/custom_components/uplift_desk/models.py`
- Constants: `/custom_components/uplift_desk/const.py`

## Implementation Plan

### Phase 1: Manual Config Flow Structure

#### 1.1 Create User Step (`async_step_user`)
Implement `async_step_user` with the following flow:

> **Step Registration Mechanism**: In Home Assistant config flows, every `step_id` passed to `async_show_form()` or `async_finish_form()` **must** correspond to a registered handler method named `async_step_{step_id}`. The step_id values in this plan are:
> - `"user"` → handler `async_step_user`
> - `"user_manual"` → handler `async_step_user_manual`
> - `"user_confirm"` → handler `async_step_user_confirm`
> These are all auto-registered by Home Assistant based on method naming convention — no explicit registration is needed.

**Step 1: Bluetooth Discovery (Cached Results)**
- Home Assistant's background Bluetooth scanner runs continuously when the `bluetooth_adapters` integration is configured.
- `async_discovered_service_info(hass)` (from `homeassistant.components.bluetooth`) returns **only devices already discovered and cached** by this background scanner — it does **not** initiate a new scan.
- There is **no Home Assistant API** to programmatically trigger a Bluetooth scan from within a config flow step. The background scanner operates independently and asynchronously.
- Display already-discovered Uplift Desk devices in a dropdown list for user selection.
- If no devices are shown in the dropdown, this is expected behavior — the device may not have been discovered yet by the background scanner.
- Provide a "Manual entry" button as the primary fallback for devices not yet discovered.
- On device selection, transition to Step 2.

> **Important: Bluetooth API Limitations**
> The following Home Assistant Bluetooth APIs are available during config flow execution:
> 
> | API | Behavior | Can trigger scan? |
> |-----|----------|-------------------|
> | `async_discovered_service_info(hass)` | Returns `list[BluetoothServiceInfoBleak]` from the background scanner's cache | **No** — only returns already-discovered devices |
> | `async_ble_device_from_discovery_info(info)` | Converts discovery info to a `BLEDeviceProtocol`-compatible object | **No** — requires existing discovery info |
> | `async_start_scanning()` / `async_stop_scanning()` | Available in `hass.data[bluetooth_scanner_key]` | **No** — these control the scanner globally and are not intended for config flow use; calling them from a config flow could disrupt other integrations' discovery |
> 
> **Why there's no "scan and wait" pattern:**
> - Bluetooth LE discovery is hardware-limited (advertising intervals are 20–100ms per device, with scan windows typically 10–100ms).
> - The background scanner runs on a fixed interval and caches results internally.
> - Config flow steps are synchronous HTTP requests — blocking for 10–15 seconds waiting for a scan would hang the UI.
> - No API exists to "start scanning and report when a specific device appears."
> 
> **Recommended approach:** Use cached discovery results + manual entry fallback. This is the same pattern used by many Home Assistant Bluetooth integrations (e.g., `bluetooth_scale`, `govee_lan`).


**Step 2: Device Information Lookup/Validation**
- For scanned device: Use device info stored on `self` (from discovery scan), attempt brief connection to verify it's a valid Uplift Desk
- For manual entry: Attempt to connect using provided address, validate device exists and is Uplift Desk, retrieve device name, store results on `self`
- **Important**: Discovered/validated device info is stored on `self` (instance attributes), NOT in `user_input`. `user_input` only carries the user's form submission (e.g., address string or dropdown selection).
- Handle connection timeouts gracefully with clear error messages
- On success, transition to Step 3

**Step 3: Confirmation Dialog**
- Show device information (name, address) to user for confirmation via `async_show_form(step_id="user_confirm", ...)`
- Routes to handler `async_step_user_confirm` automatically
- Provide confirmation button
- On submit, create config entry and finish

#### 1.2 Key Methods to Implement

```python
async def async_step_user(self, user_input=None):
    """Handle flow initialized by user."""
    if user_input is not None:
        if user_input.get("select_device"):
            # User selected a discovered device: device info is already on self._discovered_device
            # from the scan. Do NOT pass it through user_input.
            return await self.async_step_user_confirm()
        elif user_input.get("manual_entry"):
            # User wants manual address entry, show manual form
            return self.async_show_form(
                step_id="user_manual",
                data_schema=vol.Schema({
                    vol.Required("address"): str,
                    vol.Optional("name"): str,
                })
            )
        else:
            # Process user input and continue flow
            # device info is already on self from the scan
            return await self.async_step_user_confirm()
    
    # Show scan results
    return self.async_show_form(
        step_id="user",
        data_schema=vol.Schema({
            vol.Required("select_device"): selector({
                "select": {
                    "options": self._discovered_devices,
                    "mode": "dropdown",
                }
            }),
            vol.Optional("manual_entry"): bool,
        }, extra_fields=[vol.Optional("description")])
    )

async def async_step_user_manual(self, user_input=None):
    """Show manual address entry form."""
    if user_input is not None:
        # Validate address and store on self for later steps
        self._discovered_device = await self._desk_validator.validate_address(
            user_input["address"]
        )
        self._discovery_info = BluetoothServiceInfoBleak(
            name=user_input.get("name", user_input["address"]),
            address=user_input["address"],
            ...
        )
        return await self.async_step_user_confirm()

    return self.async_show_form(
        step_id="user_manual",
        data_schema=vol.Schema({
            vol.Required("address"): str,
            vol.Optional("name"): str,
        })
    )

async def async_step_user_confirm(self, user_input=None):
    """Confirm device details.
    
    IMPORTANT: Device info comes from self attributes (set by prior steps),
    NOT from user_input. user_input only carries what the user typed.
    """
    # Access discovered device info from self, not user_input
    assert self._discovered_device is not None
    device = self._discovered_device
    assert self._discovery_info is not None
    discovery_info = self._discovery_info
    
    if user_input is not None:
        return self.async_create_entry(
            title=discovery_info.name,
            data={"address": discovery_info.address, "name": discovery_info.name},
        )

    self._set_confirm_only()
    placeholders = {"name": discovery_info.name, "address": discovery_info.address}
    self.context["title_placeholders"] = placeholders
    return self.async_show_form(
        step_id="user_confirm",
        description_placeholders=placeholders,
    )
```

#### 1.3 Integration with Existing Code

**Leverage Existing Components:**
- Use `DeskValidator` to validate device connectivity
- Store same structure as discovery flow: `{"address": ..., "name": ...}`
- Use same `DiscoveredDesk` model class
- Use same timeout constant `BLEAK_TIMEOUT_SECONDS`

**Unique ID Handling:**
- Set unique ID to the MAC address (consistent with discovery flow)
- Prevent duplicate configurations with `_abort_if_unique_id_configured()`

### Phase 2: Data Model Alignment

Ensure manual entry stores data identically to discovery:
- Config entry data: `{"address": "XX:XX:XX:XX:XX:XX", "name": "Desk Name"}`
- Unique ID: MAC address
- Entry title: Device name

### Phase 3: Translations

Update `strings.json` to include manual flow translations:

**strings.json additions:**
- `config.step.user.title`: "Configure Uplift Desk"
- `config.step.user.description`: "Enter the Bluetooth address of your Uplift Desk"
- `config.step.user.data.address.name`: "Bluetooth Address"
- `config.step.user.data.name.name`: "Device Name (Optional)"
- `config.step.user_confirm.title`: "Confirm Device"
- `config.step.user_confirm.description`: "Configure {name} at {address}"
- `config.abort.no_devices_found`: "Device not found or not accessible"
- `config.abort.already_configured`: "Device is already configured"

### Phase 4: Testing Considerations

**Test Scenarios:**
1. Manual entry with valid address that is discoverable
2. Manual entry with invalid address format
3. Manual entry with address that is not in range
4. Attempt to configure same device twice (already configured)
5. Cancellation during the flow
6. Integration with existing discovery flow (same device)

**Testing Steps:**
1. Access Home Assistant UI → Settings → Devices & Services
2. Click "Add Integration" → "Uplift Desk"
3. Enter a valid Bluetooth address
4. Verify device is found and configuration completes
5. Verify sensor and button entities are created

### Phase 5: Required Files to Modify

1. **config_flow.py**
   - Modify `async_step_user` to support multi-step flow
   - Add `async_step_user_confirm` method
   - Implement device validation logic when user provides address

2. **strings.json**
   - Add translations for user and confirm steps
   - Add abort reasons (no_devices_found, already_configured)

3. **manifest.json** (no changes required)
   - Already has `config_flow: true`

### Phase 6: Dependencies

- `uplift-ble` package (already declared in requirements)
- `asyncio` for async operations
- `homeassistant.components.bluetooth` for Bluetooth utilities
- `voluptuous` for schema validation

### Phase 7: Security Considerations

- **Timeout Handling:** Connection attempts must respect `BLEAK_TIMEOUT_SECONDS` (15s)
- **Error Messages:** Do not expose internal error details to user
- **Input Validation:** Validate MAC address format before attempting connection

### Phase 8: Pre-Implementation Tasks

Before implementing, review the existing code:
1. Read `desk_validator.py` to understand validation method signature
2. Check if `DeskValidator.validate_device()` accepts address directly or requires `BluetoothServiceInfoBleak`
3. Verify if device name can be retrieved post-discovery for manual entries
4. Review existing discovery flow tests to understand expected behavior

### Manual Entry: Constructing a `BLEDeviceProtocol` Object

The `DeskValidator.validate_device()` method (desk_validator.py:74) accepts a `BLEDeviceProtocol`, not a raw MAC address string. During manual entry, the user provides a MAC address as text (`"AA:BB:CC:DD:EE:FF"`), which must be wrapped into a `BLEDeviceProtocol`-compatible object before passing it to `DeskValidator`.

**Why this matters:** The manual config flow (phase 1, step 1b) asks the user for a MAC address string via a text input. Meanwhile, `DeskValidator.validate_device()` expects a `BLEDeviceProtocol` object. The spec must document how to construct this object manually.

**`BLEDeviceProtocol` definition** (ble_protos.py:40-45):
```python
class BLEDeviceProtocol(Protocol):
    @property
    def name(self) -> str | None: ...

    @property
    def address(self) -> str: ...
```

There are two approaches for the manual flow:

#### Approach A: Simple Stub Dataclass (Recommended)

Create a lightweight `dataclass` that provides the two required attributes. Since `BLEDeviceProtocol` is a [structural protocol](https://peps.python.org/pep-0544/) (duck-typed), any object with `.address` and `.name` works — no inheritance required.

```python
from dataclasses import dataclass

@dataclass
class _ManualBLEDevice:
    """BLEDeviceProtocol-compatible stub for manual entry."""
    address: str
    name: str | None = None
```

Usage in config flow (inside `async_step_user_manual`, after collecting user input):
```python
# user_input["address"] is the MAC string from the UI
manual_device = _ManualBLEDevice(
    address=user_input["address"],
    name=user_input.get("name"),  # may be None or user-provided
)

self._discovered_device = await self._desk_validator.validate_device(manual_device)
if self._discovered_device is None:
    return self.async_show_form(
        step_id="user_manual",
        data_schema=vol.Schema({
            vol.Required("address"): str,
            vol.Optional("name"): str,
        }),
        errors={"base": "invalid_address"},
    )

# Discovery succeeded - capture the real name from the validated device
self._manual_address = self._discovered_device.address
self._manual_name = self._discovered_device.name
```

#### Approach B: Home Assistant's `BluetoothServiceInfoBleak`

Alternatively, construct a `BluetoothServiceInfoBleak` object from the user-entered address and pass it to `DeskValidator`. This requires accessing Home Assistant internals:

```python
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from bleak import BLEDevice

# Build a Bleak BLEDevice from the MAC address, then wrap it
bleak_device = BLEDevice(
    address=user_input["address"],
    name=user_input.get("name"),
)
manual_device = BluetoothServiceInfoBleak(
    name=user_input.get("name", "") or "manual",
    address=user_input["address"],
    rssi=-100,  # placeholder since we don't have radio data
    device=bleak_device,
    details=None,
    advertisement=None,
)
```

**Approach A is preferred** because:
- `BLEDeviceProtocol` is a structural `Protocol` — it only requires `.address` and `.name` properties.
- `DeskValidator` calls `self._client_factory(device, timeout)` which invokes `BleakClient(address_or_ble_device=device.address, ...)` (desk_validator.py:139). This only uses `device.address`, so a minimal stub suffices.
- Approach B adds unnecessary dependencies on Home Assistant internals and requires more boilerplate.
- The stub dataclass is self-contained, easy to test, and clearly documents the contract.

**Important notes for implementation:**
- The user's optional `name` input should be passed as the stub's `name` attribute, but if `DeskValidator` successfully connects, the real device name from the validated `DiscoveredDesk` should override it (since the user input may be incorrect or empty).
- MAC address format validation should happen in the form schema (e.g., via a `Voluptuous` regex or a helper function), not rely on `DeskValidator` for this.

### Implementation Sequence (Future Work)

1. Implement `async_step_user` and `async_step_user_confirm` methods
2. Add validation logic for manual address input
3. Add connection attempt to verify device exists
4. Add confirmation step with proper error handling
5. Update strings.json with translations
6. Run existing tests to ensure no regression
7. Add tests for manual flow
8. Test manually with actual hardware

## Summary

This plan outlines a 3-step user flow for manual configuration that mirrors the discovery flow's data structure and validation approach. The key is leveraging existing validation logic while providing a user-friendly interface for manual address entry.

## Questions to Resolve

1. Does `DeskValidator.validate_device()` work with manual address or only with `BluetoothServiceInfoBleak`?
2. Can we retrieve device name from hardware for non-discoverable devices?
3. What is the preferred behavior if device is found but not responding properly?

---
**Status:** Plan ready for review and approval before code implementation.