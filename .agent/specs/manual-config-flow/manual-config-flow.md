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

**What's already correctly validated** (no changes needed):
- `BLEAK_TIMEOUT_SECONDS` location in `const.py` — all connection attempts must use this constant
- File paths (`config_flow.py`, `desk_validator.py`, `models.py`, `const.py`) — accurate
- `DeskValidator` approach — single entry point for device validation, reuse for both discovery and manual
- Unique ID = MAC address — prevents duplicate configs
- `async_step_bluetooth` discovery flow — manual flow must store data identically

### Phase 1: Config Flow Implementation

Replace the stub `async_step_user` with a real 3-step flow in `config_flow.py`:

**Step 1 — `async_step_user`**: Show discovered devices (from cached background scanner) in a dropdown, plus a "Manual entry" button for devices not yet discovered.

**Step 2 — `async_step_user_manual`**: Accept MAC address (and optional name) from the user. Construct a `BLEDeviceProtocol` stub dataclass with `.address` and `.name`, pass it to `DeskValidator.validate_device()`, and store the validated `DiscoveredDesk` on `self`. On failure, re-show the form with an error.

**Step 3 — `async_step_user_confirm`**: Display device name and address from `self` (not `user_input`), let the user confirm, then create the config entry with `{address, name}` data and MAC as unique ID.

Key details:
- Store validated device info on `self` instance attributes (`_discovered_device`, `_discovery_info`), NOT in `user_input`.
- Set unique ID to MAC address, consistent with discovery flow.
- Use `_abort_if_unique_id_configured()` to prevent duplicates.
- `BLEDeviceProtocol` stub (Approach A, recommended): a 3-line `@dataclass` with `address: str` and `name: str | None = None`. Only `.address` is actually used by `DeskValidator` (it passes it to `BleakClient(address_or_ble_device=device.address, ...)`).
- MAC format validation should happen in the form schema, not rely on `DeskValidator`.

> **See the `### Manual Entry: Constructing a BLEDeviceProtocol Object` section below** for the full stub dataclass code and rationale.

### Phase 2: Translations & UI Polish

Add all manual-flow strings to `strings.json`:
- `config.step.user.*` — title, description, address/name labels, manual entry button
- `config.step.user_manual.*` — address field label, optional name label
- `config.step.user_confirm.*` — confirmation title, description with placeholders
- `config.error.*` — `invalid_address`, `connection_failed`
- `config.abort.*` — `already_configured`, `no_devices_found`

Apply UX polish:
- `self._set_confirm_only()` on the confirm step so the back button is suppressed.
- `self.context["title_placeholders"]` for entry title display.
- Clear, user-facing error messages that do not expose internal stack traces.
- Respect `BLEAK_TIMEOUT_SECONDS` (15s) for all connection attempts.

### Phase 3: Testing

**Test scenarios:**
1. Manual entry with a valid, in-range address → success, entities created
2. Manual entry with invalid MAC format → form-level error, no connection attempt
3. Manual entry with a valid-format address that is out of range → connection timeout error
4. Attempt to configure the same device twice (unique ID = MAC) → `already_configured` abort
5. Cancel during the flow → clean abort
6. Discovery flow and manual flow converge on the same device → no conflict

**Testing steps:**
1. Settings → Devices & Services → Add Integration → Uplift Desk
2. Enter a valid Bluetooth address manually
3. Verify device found, confirmation shown, config entry created
4. Verify sensors and buttons appear
5. Repeat with a second (duplicate) address → verify abort

### Phase 4: Files & Checklist

**Files to modify** (only these):
1. `config_flow.py` — replace stub, add 3-step flow
2. `strings.json` — add translations
3. `manifest.json` — no changes (already has `config_flow: true`)

**Pre-implementation review:**
1. Confirm `DeskValidator.validate_device()` signature and return type
2. Verify it works with a raw MAC via a `BLEDeviceProtocol` stub
3. Review existing discovery flow tests — understand expected behavior to mirror

**Execution order:**
1. Implement `async_step_user`, `async_step_user_manual`, `async_step_user_confirm`
2. Add `BLEDeviceProtocol` stub dataclass
3. Add MAC format validation helper
4. Wire up `DeskValidator` calls with timeout and error handling
5. Update `strings.json` with all translations
6. Run existing test suite — verify no regression
7. Add unit tests for manual flow (mock `DeskValidator`, test form logic)
8. Test manually with actual hardware

### Manual Entry: Constructing a `BLEDeviceProtocol` Object

The `DeskValidator.validate_device()` method (desk_validator.py:74) accepts a `BLEDeviceProtocol`, not a raw MAC address string. During manual entry, the user provides a MAC address as text (`"AA:BB:CC:DD:EE:FF"`), which must be wrapped into a `BLEDeviceProtocol`-compatible object before passing it to `DeskValidator`.

**Why this matters:** The manual config flow (step 2) asks the user for a MAC address string via a text input. Meanwhile, `DeskValidator.validate_device()` expects a `BLEDeviceProtocol` object. The spec must document how to construct this object manually.

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

---
**Status:** Plan ready for review and approval before code implementation.