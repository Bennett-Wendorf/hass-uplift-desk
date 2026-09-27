# Manual Config Flow — Task Breakdown

> **Goal:** Implement a 3-step manual configuration flow (`async_step_user` → `async_step_user_manual` → `async_step_user_confirm`) for the Uplift Desk integration, allowing users to add desks not discovered via Bluetooth.

## Parallelization Groups

Tasks are grouped into **independent tracks** that can be executed by separate sub-agents simultaneously. Within each track, tasks are ordered by dependency.

---

## Track A — Infrastructure & Research

These tasks prepare the foundation. Tasks A1 and A2 are independent; A3 depends on both.

### A1. Research `uplift_ble` Package API
- **File:** N/A (read-only research)
- **Scope:** Inspect the installed `uplift_ble==0.5.0` package to document:
  - `DeskValidator.validate_device()` — full signature, parameters, return type, timeout behavior
  - `BLEDeviceProtocol` — full definition (from `ble_protos.py`)
  - `DiscoveredDesk` — fields, methods (especially `create_controller()`)
  - `DeskController` — key methods used during validation
  - `DeskEventType` — enum values
- **Output:** A markdown summary of findings (can be stored in the spec dir or used as inline knowledge)
- **Dependencies:** None
- **Can run in parallel with:** A2

### A2. Review Existing Discovery Flow & Bluetooth Handling
- **File:** `config_flow.py` (read), `coordinator.py` (read)
- **Scope:**
  - Document how `async_step_bluetooth` and `async_step_bluetooth_confirm` work end-to-end
  - Document how `_discovered_device`, `_discovery_info` are stored and used
  - Note the exact data stored in `async_create_entry()` (keys, values)
  - Identify the `Uplift_Desk_DeskConfigEntry` type alias pattern
- **Output:** A clear reference of the discovery flow's data flow and entry creation pattern
- **Dependencies:** None
- **Can run in parallel with:** A1

### A3. Create `_ManualBLEDevice` Stub Dataclass
- **File:** `config_flow.py` (add to top of file, near imports)
- **Scope:** Add the recommended `@dataclass` stub:
  ```python
  from dataclasses import dataclass

  @dataclass
  class _ManualBLEDevice:
      """BLEDeviceProtocol-compatible stub for manual entry."""
      address: str
      name: str | None = None
  ```
- **Output:** Stub dataclass available for use in `async_step_user_manual`
- **Dependencies:** A1 (must know `BLEDeviceProtocol` contract)

### A4. Create MAC Address Format Validation Helper
- **File:** `config_flow.py` (add as module-level function)
- **Scope:** Write a validation function that checks MAC address format:
  - Accepts: `AA:BB:CC:DD:EE:FF` (6 hex byte pairs, colon-separated)
  - Also accepts: `AABBCCDDEEFF` (12 hex characters, no separators)
  - Returns `bool` or raises `vol.Invalid`
- **Output:** Reusable `validate_mac_address()` function
- **Dependencies:** None

---

## Track B — Config Flow Implementation

These tasks implement the 3-step flow. B1→B2→B3 is sequential; B4 is the integration step.

### B1. Implement `async_step_user` (Step 1 — Device Selection)
- **File:** `config_flow.py`
- **Scope:** Replace the stub `async_step_user` with:
  - Show a dropdown of any discovered devices (from cached scanner / Bluetooth advertisement)
  - Include a "Manual entry" option/button for devices not yet discovered
  - If no devices are discovered, skip straight to manual entry
  - On selection of "Manual entry", transition to `async_step_user_manual`
  - On selection of a discovered device, validate it and proceed to confirmation
  - Schema fields: Single select with options `[discovered_device_name_1, ..., "Manual entry"]`
- **Dependencies:** None (but benefits from A2 knowledge)
- **Can run in parallel with:** B2, B3

### B2. Implement `async_step_user_manual` (Step 2 — Manual MAC Entry)
- **File:** `config_flow.py`
- **Scope:**
  - Show form with required `address` field (MAC address) and optional `name` field
  - Apply MAC format validation (from A4) in the schema
  - Construct `_ManualBLEDevice` stub from user input
  - Call `DeskValidator.validate_device()` with the stub
  - On success: store `self._discovered_device`, `self._manual_address`, `self._manual_name`; transition to confirmation
  - On failure: re-show form with error message (`invalid_address` or `connection_failed`)
- **Schema fields:**
  ```python
  vol.Required("address"): str,  # with MAC format validation
  vol.Optional("name"): str,
  ```
- **Dependencies:** A3 (stub dataclass), A4 (MAC validation)
- **Can run in parallel with:** B1, B3

### B3. Implement `async_step_user_confirm` (Step 3 — Confirmation)
- **File:** `config_flow.py`
- **Scope:**
  - Display device name and address (from `self`, not `user_input`)
  - Let user confirm
  - On confirm: call `async_create_entry()` with `{address, name}` data, MAC as unique ID
  - Use `self._set_confirm_only()` to suppress back button
  - Set `self.context["title_placeholders"]` for entry title
- **Dependencies:** None (but benefits from A2 knowledge)
- **Can run in parallel with:** B1, B2

### B4. Wire Up `DeskValidator` Calls with Timeout & Error Handling
- **File:** `config_flow.py`
- **Scope:**
  - Ensure all `DeskValidator.validate_device()` calls use `BLEAK_TIMEOUT_SECONDS` (15s) from `const.py`
  - Handle `TimeoutError`, `Exception` gracefully with user-facing error messages
  - Ensure `_abort_if_unique_id_configured()` is called before validation to prevent duplicates
- **Dependencies:** B1, B2, B3 (integration after individual steps are implemented)

---

## Track C — Translations & UI Polish

These tasks add all user-facing strings and polish. Fully independent of Tracks A and B.

### C1. Add Config Flow Translations to `strings.json`
- **File:** `custom_components/uplift_desk/strings.json`
- **Scope:** Add all strings needed by the manual flow:
  - `config.step.user.*` — title, description, device selector label, manual entry label
  - `config.step.user_manual.*` — title, description, address/name field labels, address data_description
  - `config.step.user_confirm.*` — title, description with placeholders, confirm menu option
  - `config.error.*` — `invalid_address`, `connection_failed`, `unknown_error`
  - `config.abort.*` — `already_configured`, `no_devices_found`
- **Dependencies:** None
- **Can run in parallel with:** Everything else

### C2. Update `translations/en.json`
- **File:** `custom_components/uplift_desk/translations/en.json`
- **Scope:** Mirror the `strings.json` translations into `en.json` for the config flow section
- **Dependencies:** C1 (use as source)

---

## Track D — Testing

These tasks create the test infrastructure and test cases. Depends on Tracks A, B, C being complete.

### D1. Set Up Test Infrastructure
- **File:** New files — `tests/`, `tests/test_config_flow.py`, `tests/conftest.py`
- **Scope:**
  - Create `tests/` directory with `__init__.py`
  - Create `tests/conftest.py` with shared fixtures (mock `HomeAssistant`, mock `DeskValidator`)
  - Create `tests/test_config_flow.py` as the main test file
  - Add any needed pytest configuration (check if `pyproject.toml` or `setup.cfg` exists)
- **Dependencies:** A1, A2 (to understand what to mock)

### D2. Write Unit Tests for Manual Config Flow
- **File:** `tests/test_config_flow.py`
- **Scope:** Write tests for all scenarios from the plan:
  1. Manual entry with valid, in-range address → success, entities created
  2. Manual entry with invalid MAC format → form-level error, no connection attempt
  3. Manual entry with valid-format address that is out of range → connection timeout error
  4. Attempt to configure same device twice (unique ID = MAC) → `already_configured` abort
  5. Cancel during the flow → clean abort
  6. Discovery flow and manual flow converge on same device → no conflict
  7. `async_step_user` with no discovered devices → shows manual entry option
  8. `async_step_user` with discovered devices → shows dropdown
- **Dependencies:** D1 (test infrastructure)

---

## Track E — Integration & Verification

Final sequential steps after all implementation is complete.

### E1. Run Existing Test Suite — Verify No Regression
- **Scope:** Run `pytest` (or Home Assistant's test runner) to ensure no existing functionality is broken
- **Dependencies:** D1 (test infrastructure)

### E2. Manual Testing with Actual Hardware
- **Scope:** Follow the testing steps from the plan:
  1. Settings → Devices & Services → Add Integration → Uplift Desk
  2. Enter a valid Bluetooth address manually
  3. Verify device found, confirmation shown, config entry created
  4. Verify sensors and buttons appear
  5. Repeat with a second (duplicate) address → verify abort
- **Dependencies:** B4, C2 (full implementation complete)

---

## Execution Summary

| Track | Tasks | Parallel? | Depends On |
|-------|-------|-----------|------------|
| **A** | A1, A2 → A3, A4 | A1∥A2; then A3∥A4 | A1→A3 |
| **B** | B1∥B3, B2, B4 | B1∥B2∥B3; then B4 | A3, A4 → B2 |
| **C** | C1 → C2 | C1∥B tracks | None |
| **D** | D1 → D2 | D1→D2 | A1, A2, B, C |
| **E** | E1 → E2 | Sequential | D, B, C |

### Recommended Parallel Execution Schedule

| Round | Tasks to Start | Prerequisites to Complete |
|-------|----------------|--------------------------|
| **1** | A1, A2, C1 | None — all three start simultaneously (3 sub-agents) |
| **2** | A3, A4, C2, B1, B3 | A1, A2, C1 complete (5 sub-agents) |
| **3** | B2 | A3, A4, B1, B3 complete |
| **4** | B4 | B1, B2, B3 complete |
| **5** | D1 | A1, A2, B4, C2 complete |
| **6** | D2 | D1 complete |
| **7** | E1, E2 | D2 complete |