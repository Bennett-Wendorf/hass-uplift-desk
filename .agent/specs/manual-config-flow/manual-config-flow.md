# Manual Config Flow Implementation Plan

## Overview

Currently, the Uplift Desk integration only supports Bluetooth discovery-based setup. This document outlines the plan to implement a manual configuration flow to allow users to add Uplift desks that are not being discovered automatically.

## Current State Analysis

### Discovery Flow (Working)
- Uses Bluetooth discovery via `async_step_bluetooth`
- Validates devices using `DeskValidator`
- Stores device address and name in config entry
- Unique ID is set to Bluetooth MAC address

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

**Step 1: Bluetooth Scan**
- Automatically initiate a Bluetooth scan for Uplift Desk devices
- Display discovered devices in a list for user to select
- Allow up to 10-15 seconds for scan to complete
- Provide a "Show manual entry option" button as fallback
- On device selection, transition to Step 2

**Step 1b: Manual Entry Fallback** (shown via "Show manual entry option")
- Display manual address input form
- User enters Bluetooth address (MAC address format)
- Optional: Allow user to provide custom name
- On submit, transition to Step 2

**Step 2: Device Information Lookup/Validation**
- For scanned device: Use discovered device info, attempt brief connection to verify it's a valid Uplift Desk
- For manual entry: Attempt to connect using provided address, validate device exists and is Uplift Desk, retrieve device name
- Handle connection timeouts gracefully with clear error messages
- On success, transition to Step 3

**Step 3: Confirmation Dialog**
- Show device information (name, address) to user for confirmation
- Provide confirmation button
- On submit, create config entry and finish

#### 1.2 Key Methods to Implement

```python
async def async_step_user(self, user_input=None):
    """Handle flow initialized by user."""
    if user_input is not None:
        if user_input.get("select_device"):
            # User selected a discovered device, proceed to confirmation
            return await self.async_step_user_confirm(user_input)
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
            return await self.async_step_user_confirm(user_input)
    
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
        # Validate address and continue to confirmation
        self._manual_address = user_input["address"]
        self._manual_name = user_input.get("name")
        return await self.async_step_user_confirm(user_input)
    
    return self.async_show_form(
        step_id="user_manual",
        data_schema=vol.Schema({
            vol.Required("address"): str,
            vol.Optional("name"): str,
        })
    )

async def async_step_user_confirm(self, user_input):
    """Confirm device details."""
    # Validate device exists and is an Uplift Desk
    # Show confirmation dialog
    # On submit, create entry
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