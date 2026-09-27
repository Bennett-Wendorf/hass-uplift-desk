# Plan: 01 - Height setpoint `number` entity (move desk to an arbitrary height)

**Project:** `number-height-setpoint` (project number 2 — follows `1-fix-client-disconnect`; legacy unnumbered specs under `.agent/specs/manual-config-flow/` predate the numbered convention)
**Task type:** feature | **Complexity:** medium (multi-file, but no new BLE lifecycle work — reuses the existing (re)connect cycle, controller access pattern, and coordinator pub/sub flow)

## Task Description

Add ONE new entity to the `uplift_desk` Home Assistant integration: a `number` platform entity that lets the user command the desk to move to an arbitrary height. The entity displays the desk's **current** height as its state and sends a "move to specified height" BLE command when a value is set.

**In scope (exactly one entity):**
- A `number` entity (`number.py`), key `desk_height_setpoint`, `NumberMode.BOX`, `NumberDeviceClass.DISTANCE`, native unit mm, native step 1 mm.
- Coordinator support: subscribe to height-limit notifications, expose min/max (mm) on the coordinator, and a new `async_move_to_height(mm)` command method with single-in-flight coalescing and a locked-desk guard.
- Platform registration, translations, icon, and the test suite.

**Explicitly OUT of scope (do NOT spec or implement):**
- A move/commit button and a stop button.
- A lock switch / lock `binary_sensor` (the lock *state* is only read defensively at set-time; no lock entity is created).
- Min/max limit-*setting* entities (limits are read-only inputs to this feature).
- A "desk is moving" `binary_sensor`.
- Any changes to `config_flow.py`, `manifest.json`, the pinned `uplift-ble` library, or the (re)connect lifecycle established by project 1.

**Design decisions (agreed — spec these, do not re-litigate):**
1. Entity: `number` platform, key `desk_height_setpoint`, translation key `desk_height_setpoint`, icon in `icons.json`, entries in `strings.json` + `translations/en.json`, registered in `__init__.py` `_PLATFORMS`.
2. Mode: `NumberMode.BOX` — deliberate type-and-confirm input, **not** a slider (accidental slider bumps are a physical-safety concern the user raised).
3. Device class / units: `NumberDeviceClass.DISTANCE`, `native_unit_of_measurement = UnitOfLength.MILLIMETERS`, matching the existing `desk_height` sensor. HA's number component auto-converts to locale units and validates range on `number.set_value`.
4. State semantics: the number's state mirrors the desk's **current** height (HEIGHT notifications, same coordinator flow as the sensor). Setting a value commands a move; the state then tracks the desk as it moves.
5. Min/max: dynamic, from the desk's configured height limits. The coordinator subscribes to `DeskEventType.HEIGHT_LIMITS_CONFIGURATION` (plus the live `HEIGHT_LIMIT_MAX` / `HEIGHT_LIMIT_MIN` events) and exposes min/max (mm). The entity sets `_attr_native_min_value` / `_attr_native_max_value` in `_handle_coordinator_update`. Fallback range **500–1300 mm** before the desk reports limits; `None` handling specified below.
6. Step: 1 mm native.
7. Set path: `async_set_native_value(value_mm)` → new coordinator method `async_move_to_height(mm)` → `controller.move_to_specified_height(int(mm))` via the existing `_get_or_establish_controller()` pattern. Round to int mm; HA already range-validates.
8. Command coalescing: at most one in-flight move command (each BLE command costs ~1 s `notification_timeout` plus a wake preamble on some variants). Mechanism and observable behavior specified below.
9. Edge cases: locked desk, target ≈ current height, desk unavailable, out-of-range values — behavior specified below.

## Objective

A user can open the `uplift_desk.desk_height_setpoint` number entity, type a height (in their locale unit — HA converts to mm), confirm, and the desk moves to that height. The entity's bounds always reflect the desk's configured min/max limits (with a sane fallback before the limits are known), rapid repeated sets never queue BLE writes, and a locked desk produces a clear error instead of a silently-dropped command.

## Problem Statement

Today the integration can only move the desk to one of four hardware presets (`button.py` → `coordinator.async_preset_N()` → `controller.move_to_height_preset_N()`). There is no way to command an arbitrary height, even though the hardware supports it (`DeskController.move_to_specified_height(height: int)`, opcode 0x1B, 2-byte BE payload, 0–65535 mm) and the desk already reports its configured height limits (opcode 0x07 → `HEIGHT_LIMITS_CONFIGURATION` with a `(max_mm, min_mm)` tuple; opcodes 0x21/0x22 → `HEIGHT_LIMIT_MAX` / `HEIGHT_LIMIT_MIN`).

Current state of the pieces this feature builds on (verified against the repo):
- `coordinator.py:188-241` — `_establish_and_start()` builds the controller and registers exactly one listener today: `controller.on(DeskEventType.HEIGHT, self._async_height_notify_callback)` (line 222). The height callback (`:382-385`) stores `self.height_mm` and calls `async_set_updated_data(self._desk)` — the coordinator is used as a push-only pub/sub hub (entities read coordinator attributes, the "data" object is a token).
- `coordinator.py:350-354` — `async_read_desk_height()` already calls `controller.request_height_limits()` on (re)connect; the desk's 0x07 reply currently arrives, gets parsed by the library, and is **discarded** because no `HEIGHT_LIMITS_CONFIGURATION` handler is registered.
- `coordinator.py:255-259` — `_get_or_establish_controller()` is the established "get the live controller or run the (re)connect cycle" entry point used by all command methods (`async_preset_N`, `async_wake`).
- `sensor.py:35-76` — `DeskHeightSensor` is the template: `CoordinatorEntity` + `available = coordinator.is_connected`, `native_value = coordinator.height_mm`, `_handle_coordinator_update` writes `_attr_native_value` and calls `async_write_ha_state()`, `unique_id = f"{coordinator.desk_address}_{key}"`.
- `__init__.py:23` — `_PLATFORMS = [Platform.SENSOR, Platform.BUTTON]`.
- `strings.json` / `icons.json` / `translations/en.json` — entity names/icons live under `entity.<platform>.<key>`.
- `tests/conftest.py` — full fake-BLE harness: `FakeBleakClient` (records `writes`, can `simulate_notification`), `make_notification_packet(opcode, payload)`, `wait_until`, `fake_ble` / `coordinator` fixtures, `instant_sleep` (collapses `asyncio.sleep`).

## Solution Approach

### A. Coordinator: height-limit state and event subscriptions

Add to `UpliftDeskBluetoothCoordinator`:

**State (initialized in `__init__`, next to `height_mm` at `coordinator.py:60`):**
- `self.height_limit_min_mm: int | None = None`
- `self.height_limit_max_mm: int | None = None`
- `self._move_in_flight: bool = False` (see D)

**Event subscriptions** — in `_establish_and_start()`, immediately after the existing HEIGHT registration (line 222), register three more handlers on the fresh controller (so every (re)connect re-subscribes, same as HEIGHT):
- `DeskEventType.HEIGHT_LIMITS_CONFIGURATION` → `_async_height_limits_configuration_callback(max_mm, min_mm)` — the primary source; emitted by the desk's reply to `request_height_limits()` (opcode 0x07, payload = max 2B BE + min 2B BE, event args are the tuple `(max_mm, min_mm)`).
- `DeskEventType.HEIGHT_LIMIT_MAX` → `_async_height_limit_max_callback(max_mm)` — live update (opcode 0x21) when the max limit changes (e.g. user changes it on the keypad).
- `DeskEventType.HEIGHT_LIMIT_MIN` → `_async_height_limit_min_callback(min_mm)` — live update (opcode 0x22).

**Callback shape** (mirror `_async_height_notify_callback`, `coordinator.py:382-385` — sync handlers, store on the coordinator, then notify entities):
```python
def _async_height_limits_configuration_callback(self, max_mm: int, min_mm: int) -> None:
    self.height_limit_max_mm = max_mm
    self.height_limit_min_mm = min_mm
    self.async_set_updated_data(self._desk)
```
(each of the three follows this exact shape; debug-log the received values like the height callback does.)

Notes:
- The library's own `controller.height_limit_min_mm` / `height_limit_max_mm` properties are updated by the same notifications, but the integration's established pattern is coordinator-owned state fed by events (as with `height_mm`); entities depend only on the coordinator. Follow that pattern.
- Because `async_read_desk_height()` (called during setup and `_refresh_state()` after every (re)connect) issues `request_height_limits()`, limits are normally known shortly after (re)connect — no new read command is needed.

### B. Coordinator: `async_move_to_height` (set path, coalescing, lock guard)

New exceptions (module level in `coordinator.py`, next to `UpliftDeskServicesError` at line 36). Both subclass **`HomeAssistantError`** (from `homeassistant.exceptions`) so that when they propagate out of `async_set_native_value`, the failed `number.set_value` service call shows a clean message in HA rather than a generic/traceback error:
- `class UpliftDeskMoveInFlightError(HomeAssistantError)`
- `class UpliftDeskLockedError(HomeAssistantError)`

New method (behaviorally):
```python
async def async_move_to_height(self, height_mm: int | float) -> None:
    """Command the desk to move to a specific height (mm).

    At most one move command may be in flight (coalescing, below);
    a second concurrent set raises UpliftDeskMoveInFlightError.
    Raises UpliftDeskLockedError if the desk last reported LOCKED.
    """
```
Steps, in order:
1. **Coalescing guard (reject semantics):** `if self._move_in_flight: raise UpliftDeskMoveInFlightError(...)` with a message naming the desk. The check-and-set of the flag is synchronous (no `await` between), so under the single-threaded HA event loop there is no race between two concurrent `set_value` calls.
2. **Set the flag**, then run the real work inside `try/.../finally` so the flag is **always** cleared (including on reconnect failure, lock error, or BLE error):
   1. `controller = await self._get_or_establish_controller()` — reuses the project-1 (re)connect cycle, exactly like `async_preset_N` (`coordinator.py:367-377`).
   2. **Lock guard:** `if controller.lock_status is DeskLockStatus.LOCKED: raise UpliftDeskLockedError(...)` (message: desk is locked, cannot move). If `lock_status is None` (the desk has not reported one since we connected — there is no "request lock status" command in the protocol), proceed; the firmware will reject a move to a locked desk. (No `LOCK_STATUS` subscription and no lock entity — out of scope; reading the controller property at set-time is the minimal, sufficient check.)
   3. `await controller.move_to_specified_height(int(round(height_mm)))`.
   4. Debug-log the commanded height (and note that HA already validated the value against min/max before this call).
   5. **Do not** update `self.height_mm` or call `async_set_updated_data` here: the desk replies with live HEIGHT notifications (0x01) as it moves, which flow through the existing `_async_height_notify_callback` and update the sensor *and* the number state.

**Coalescing decision — in-flight flag with REJECT semantics (chosen over the alternatives):**
- **Not an `asyncio.Lock` with `await acquire()`:** that is *queue* semantics — the second `set_value` would block until the first command's ~1 s window elapses, then issue a second BLE write. The goal is explicitly to *not* queue BLE writes.
- **Not silent drop (ignore):** the user's latest confirmed value would be lost with no feedback; the box would appear to accept a value that never took effect.
- **Reject (raise) is chosen because:** it is the simplest correct mechanism (one bool), it is honest (the failed service call tells the caller/automation the set did not take effect), and the window it guards is short — the BLE command duration only (wake preamble + `notification_timeout` ≈ 1–1.5 s), **not** the full physical move. A new set a few seconds later (desk still physically moving) is accepted, and the desk firmware retargets to the new height — the desired behavior.

**Observable behavior when a second set arrives while one is in flight:** the second `number.set_value` service call fails with the `UpliftDeskMoveInFlightError` message; no second BLE write is issued; the desk continues moving toward the first target; the in-flight flag clears when the first command's write+wait completes.

### C. The `number` entity (`number.py`, new file)

`DeskHeightSetpointNumber(CoordinatorEntity[UpliftDeskBluetoothCoordinator], NumberEntity)`, modeled on `DeskHeightSensor` (`sensor.py:35-76`):

- `async_setup_entry(hass, config_entry, async_add_entities)` — add `DeskHeightSetpointNumber(config_entry.runtime_data)` (same shape as `sensor.py:25-33`).
- `entity_description = NumberEntityDescription(`
  - `key="desk_height_setpoint"`, `translation_key="desk_height_setpoint"`, `has_entity_name=True`
  - `device_class=NumberDeviceClass.DISTANCE`
  - `native_unit_of_measurement=UnitOfLength.MILLIMETERS`
  - `native_step=1`
  - `mode=NumberMode.BOX`
  - **no** `native_min_value` / `native_max_value` in the description — they are dynamic (below).
- `self._attr_unique_id = f"{coordinator.desk_address}_{self.entity_description.key}"` (matches the sensor/button pattern).
- `device_info` and `available` properties copied in shape from `sensor.py:57-65` (`identifiers={(DOMAIN, coordinator.desk_address)}`, `name=coordinator.desk_name`; `available = coordinator.is_connected`).
- **`__init__` initializes the dynamic attrs to the fallback** so the entity is valid before the first coordinator update:
  - `self._attr_native_min_value = DEFAULT_HEIGHT_LIMIT_MIN_MM` (500)
  - `self._attr_native_max_value = DEFAULT_HEIGHT_LIMIT_MAX_MM` (1300)
  - `self._attr_native_value = coordinator.height_mm` (may be `None` → HA shows unknown)
- **`_handle_coordinator_update`** (the pattern the brief calls for; works because `NumberEntity.native_min_value` / `native_max_value` / `native_step` / `native_value` / `mode` / `device_class` are in `CACHED_PROPERTIES_WITH_ATTR_`, so assigning the `_attr_` forms invalidates the cached properties):
  ```python
  @callback
  def _handle_coordinator_update(self) -> None:
      self._attr_native_value = self.coordinator.height_mm
      self._attr_native_min_value, self._attr_native_max_value = self._effective_limits()
      self.async_write_ha_state()
  ```
  where `_effective_limits()` implements the min/max resolution rules below.
- **`async_set_native_value(self, value: float)`** — the async hook HA's `number.set_value` service calls *after* unit conversion and range validation (the value arrives in native mm):
  ```python
  async def async_set_native_value(self, value: float) -> None:
      await self.coordinator.async_move_to_height(value)
  ```
  Nothing else: do **not** write `_attr_native_value = value` (state mirrors the *actual* desk height, which updates via HEIGHT notifications as the desk moves); do **not** swallow exceptions (let the in-flight/locked/`BleakError` errors propagate to the service caller).

**Min/max resolution rules (in `_effective_limits()`):**
1. `min = coordinator.height_limit_min_mm` if not `None`, else `DEFAULT_HEIGHT_LIMIT_MIN_MM` (500).
2. `max = coordinator.height_limit_max_mm` if not `None`, else `DEFAULT_HEIGHT_LIMIT_MAX_MM` (1300).
3. Defensive guard: if `min >= max` (a desk misreporting inverted limits would otherwise produce an invalid HA number), fall back to the full default range `(500, 1300)`.
4. Partial knowledge is fine: if the desk has only reported a max (e.g. a lone 0x21 event), the min stays at the fallback and vice versa.

**State semantics recap:** displayed value = `coordinator.height_mm` (current height). After a successful set, the value tracks the desk's live height until it arrives at the target. If the desk is already at the target (no-op move), the value is unchanged — acceptable per the brief.

### D. Wiring, constants, translations

- `const.py`: add `DEFAULT_HEIGHT_LIMIT_MIN_MM: int = 500` and `DEFAULT_HEIGHT_LIMIT_MAX_MM: int = 1300`.
- `__init__.py:23`: `_PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON, Platform.NUMBER]`.
- `strings.json` — add under `entity`:
  ```json
  "number": { "desk_height_setpoint": { "name": "Height Setpoint" } }
  ```
- `translations/en.json` — mirror the same addition (kept in lockstep with `strings.json`, as today).
- `icons.json` — add under `entity`:
  ```json
  "number": { "desk_height_setpoint": { "default": "mdi:arrow-up-down" } }
  ```
  (`mdi:arrow-up-down` is the suggested default — it reads as "adjustable height"; the builder may substitute an equivalent sensible icon, but it must be a valid Material Design icon.)
- `manifest.json`: **no change** (`uplift-ble==0.7.0` stays pinned; `move_to_specified_height` and the limit events exist in the pinned library).

## Relevant Files

- `custom_components/uplift_desk/coordinator.py` — **primary file.**
  - `:36` `UpliftDeskServicesError` — new exceptions go next to it
  - `:46-63` `__init__` — add `height_limit_min_mm` / `height_limit_max_mm` (both `None`), `_move_in_flight` (`False`)
  - `:188-241` `_establish_and_start` — add the three limit-event subscriptions after line 222 (HEIGHT)
  - `:255-259` `_get_or_establish_controller` — reused, unchanged
  - `:350-354` `async_read_desk_height` — unchanged (its `request_height_limits()` call is what feeds the new `HEIGHT_LIMITS_CONFIGURATION` handler)
  - `:367-377` `async_preset_1..4` — template for `async_move_to_height`
  - `:382-385` `_async_height_notify_callback` — template for the three limit callbacks
- `custom_components/uplift_desk/number.py` — **new** (see C)
- `custom_components/uplift_desk/__init__.py` — `:23` add `Platform.NUMBER` to `_PLATFORMS`
- `custom_components/uplift_desk/const.py` — add the two fallback-limit constants
- `custom_components/uplift_desk/strings.json` — `entity.number.desk_height_setpoint.name`
- `custom_components/uplift_desk/icons.json` — `entity.number.desk_height_setpoint.default`
- `custom_components/uplift_desk/translations/en.json` — mirror of the strings addition
- `custom_components/uplift_desk/sensor.py` — **read-only reference** (entity template); no changes
- `custom_components/uplift_desk/button.py` — **read-only reference**; no changes
- `tests/conftest.py` — **read-only reference**; the existing fakes (`FakeBleakClient.writes`, `simulate_notification`, `make_notification_packet`, `wait_until`, `fake_ble`, `coordinator`) cover everything needed — no new fixtures expected (a limit-packet builder helper may be added to the new test module, not conftest)
- `tests/test_number.py` — **new** (see Task 6)

### Library references (READ-ONLY — do not modify; per `.agent/docs/uplift_ble_0.5.0.md`)

- `DeskController.move_to_specified_height(height: int)` — opcode 0x1B, 2-byte BE payload, 0–65535 mm; `@command_writer` = optional wake preamble + write + `notification_timeout` wait.
- `DeskController.request_height_limits()` — opcode 0x07; the desk replies with a 0x07 notification → `HEIGHT_LIMITS_CONFIGURATION` event with `(max_mm, min_mm)`.
- `DeskEventType.HEIGHT_LIMITS_CONFIGURATION` (0x07, `(int, int)`), `HEIGHT_LIMIT_MAX` (0x21, `int`), `HEIGHT_LIMIT_MIN` (0x22, `int`).
- `DeskController.lock_status` — `DeskLockStatus.LOCKED | DeskLockStatus.UNLOCKED | None` (None until a 0x1F notification is seen; no query command exists).
- No `stop_movement()` usage in this feature (stop button out of scope).

### New Files

- `custom_components/uplift_desk/number.py` — the number platform.
- `tests/test_number.py` — mocked-coordinator/entity test suite for the feature.

## Team Orchestration

> **Worktree Isolation**: The team-lead creates an isolated git worktree for this spec using `~/.config/opencode/scripts/worktree-create.sh`. All builders work inside this worktree. After final validation, changes are merged back via `~/.config/opencode/scripts/worktree-merge.sh`.

The team-lead agent will orchestrate execution using these team members:

### Team Members

- **Builder**
  - Name: `setpoint-builder`
  - Role: Implement the coordinator limit subscriptions and `async_move_to_height` (coalescing + lock guard), the `number.py` entity, platform/constant wiring, translations, and the test suite per the step-by-step tasks.
  - Agent: builder

- **Validator**
  - Name: `setpoint-validator`
  - Role: Verify implementation meets criteria — run the test suite, static/JSON checks, and review the diff against every acceptance criterion (including out-of-scope items staying untouched).
  - Agent: validator

- **Documenter**
  - Name: `setpoint-documenter`
  - Role: Generate user-facing documentation for the new entity in `app_docs/`.
  - Agent: documenter

## Step by Step Tasks

### 1. Coordinator: height-limit state and event subscriptions
- **Task ID**: coordinator-height-limits
- **Depends On**: none
- **Assigned To**: setpoint-builder
- **Agent**: builder
- **Actions**:
  - In `coordinator.py` `__init__`: initialize `self.height_limit_min_mm: int | None = None`, `self.height_limit_max_mm: int | None = None`, `self._move_in_flight: bool = False`.
  - In `_establish_and_start()`, after the existing `controller.on(DeskEventType.HEIGHT, ...)` (line 222), register:
    - `DeskEventType.HEIGHT_LIMITS_CONFIGURATION` → new `_async_height_limits_configuration_callback(self, max_mm, min_mm)` (stores both, then `self.async_set_updated_data(self._desk)`),
    - `DeskEventType.HEIGHT_LIMIT_MAX` → new `_async_height_limit_max_callback(self, max_mm)`,
    - `DeskEventType.HEIGHT_LIMIT_MIN` → new `_async_height_limit_min_callback(self, min_mm)`.
  - Each callback: sync, stores the value(s) on the coordinator, debug-logs (matching the height callback's log style), calls `async_set_updated_data(self._desk)`.
  - Do **not** touch the (re)connect cycle, validation, or reconnect logic (project-1 territory).
- **Acceptance Criteria**:
  - A simulated 0x07 notification with (max=1200, min=600) updates `coordinator.height_limit_max_mm == 1200` and `coordinator.height_limit_min_mm == 600` and notifies coordinator listeners.
  - Simulated 0x21 / 0x22 notifications update `height_limit_max_mm` / `height_limit_min_mm` individually.
  - Before any limit notification, both attributes are `None`.
  - Subscriptions are re-registered on every (re)connect cycle (same code path as the HEIGHT listener).

### 2. Coordinator: `async_move_to_height` with coalescing and lock guard
- **Task ID**: coordinator-move-to-height
- **Depends On**: coordinator-height-limits
- **Assigned To**: setpoint-builder
- **Agent**: builder
- **Actions**:
  - Add `UpliftDeskMoveInFlightError(HomeAssistantError)` and `UpliftDeskLockedError(HomeAssistantError)` at module level in `coordinator.py` (import `HomeAssistantError` from `homeassistant.exceptions`).
  - Implement `async_move_to_height(self, height_mm: int | float) -> None` exactly per Solution Approach B:
    - reject with `UpliftDeskMoveInFlightError` if `self._move_in_flight`;
    - set the flag; in `try/finally` (flag cleared in `finally`): `_get_or_establish_controller()` → if `controller.lock_status is DeskLockStatus.LOCKED` raise `UpliftDeskLockedError` → `await controller.move_to_specified_height(int(round(height_mm)))`.
  - Import `DeskLockStatus` from `uplift_ble.desk_enums`.
  - Debug-log the commanded height; do not mutate `height_mm` or push coordinator updates from this method.
- **Acceptance Criteria**:
  - `async_move_to_height(800)` on a connected (fake) desk results in exactly one write to the input characteristic whose payload encodes opcode 0x1B and 800 mm BE; `move_to_specified_height` receives an `int`.
  - A call made while `self._move_in_flight is True` raises `UpliftDeskMoveInFlightError` and issues **no** BLE write; the flag is left untouched.
  - After any completed call (success or exception), `_move_in_flight` is `False` again.
  - With `controller.lock_status == DeskLockStatus.LOCKED`, the call raises `UpliftDeskLockedError` and issues no write; with `lock_status is None` it proceeds.
  - `BleakError` from the underlying write propagates (not swallowed) and the flag is still cleared.

### 3. Number entity (`number.py`)
- **Task ID**: number-entity
- **Depends On**: coordinator-move-to-height
- **Assigned To**: setpoint-builder
- **Agent**: builder
- **Actions**:
  - Create `custom_components/uplift_desk/number.py` per Solution Approach C: `async_setup_entry` + `DeskHeightSetpointNumber(CoordinatorEntity, NumberEntity)`.
  - `NumberEntityDescription(key="desk_height_setpoint", translation_key="desk_height_setpoint", has_entity_name=True, device_class=NumberDeviceClass.DISTANCE, native_unit_of_measurement=UnitOfLength.MILLIMETERS, native_step=1, mode=NumberMode.BOX)` — no min/max in the description.
  - `__init__`: `unique_id = f"{coordinator.desk_address}_desk_height_setpoint"`; initialize `_attr_native_min_value` / `_attr_native_max_value` to the fallback constants and `_attr_native_value` to `coordinator.height_mm`.
  - `device_info` + `available` properties in the sensor's shape (`available = coordinator.is_connected`).
  - `_handle_coordinator_update`: set `_attr_native_value = coordinator.height_mm` and `_attr_native_min_value` / `_attr_native_max_value` from the min/max resolution rules (reported value if not `None`, else fallback; if `min >= max`, full fallback range), then `async_write_ha_state()`.
  - `async_set_native_value(value)`: `await self.coordinator.async_move_to_height(value)` — no state write, no exception swallowing.
- **Acceptance Criteria**:
  - The entity reports `mode == NumberMode.BOX`, `device_class == NumberDeviceClass.DISTANCE`, native unit mm, native step 1, and the expected unique id.
  - Before any limit event: `native_min_value == 500`, `native_max_value == 1300`.
  - After a (1200, 600) limits event: `native_max_value == 1200`, `native_min_value == 600`.
  - After a HEIGHT notification (e.g. 750 mm): `native_value == 750.0`.
  - `async_set_native_value(800)` delegates to `coordinator.async_move_to_height` and does not by itself change `native_value`.
  - `available` tracks `coordinator.is_connected`.

### 4. Platform registration and constants
- **Task ID**: platform-wiring
- **Depends On**: number-entity
- **Assigned To**: setpoint-builder
- **Agent**: builder
- **Actions**:
  - `const.py`: add `DEFAULT_HEIGHT_LIMIT_MIN_MM: int = 500` and `DEFAULT_HEIGHT_LIMIT_MAX_MM: int = 1300`.
  - `__init__.py`: extend `_PLATFORMS` to `[Platform.SENSOR, Platform.BUTTON, Platform.NUMBER]`.
  - No changes to setup/unload logic beyond the list edit.
- **Acceptance Criteria**:
  - After a successful config-entry setup, the number platform is loaded (a `uplift_desk`-owned number entity exists for the device) alongside the sensor and buttons.
  - `const.py` holds both fallback constants; `number.py` imports them from `.const`.

### 5. Translations and icon
- **Task ID**: translations
- **Depends On**: none (touches only JSON files; may run in parallel with Tasks 1–4)
- **Assigned To**: setpoint-builder
- **Agent**: builder
- **Actions**:
  - `strings.json`: add `entity.number.desk_height_setpoint.name = "Height Setpoint"`.
  - `translations/en.json`: mirror the identical addition.
  - `icons.json`: add `entity.number.desk_height_setpoint.default = "mdi:arrow-up-down"` (or another valid MDI icon if the builder justifies a better fit).
  - Keep both JSON files valid and in lockstep (as they are today).
- **Acceptance Criteria**:
  - All three JSON files parse; `strings.json` and `translations/en.json` contain identical `entity.number` blocks; the icon value is a valid Material Design icon id.

### 6. Test suite
- **Task ID**: tests
- **Depends On**: platform-wiring, translations
- **Assigned To**: setpoint-builder
- **Agent**: builder
- **Actions**:
  - Create `tests/test_number.py` using the existing conftest fakes (no new fixtures; add small local packet builders):
    - `make_limits_config_packet(max_mm, min_mm)` → `make_notification_packet(0x07, max_mm.to_bytes(2, "big") + min_mm.to_bytes(2, "big"))`
    - `make_limit_max_packet(max_mm)` → `make_notification_packet(0x21, max_mm.to_bytes(2, "big"))`
    - `make_limit_min_packet(min_mm)` → `make_notification_packet(0x22, min_mm.to_bytes(2, "big"))`
  - Implement at minimum these tests (names are the contract):
    1. `test_number_entity_created_with_fallback_limits` — full entry setup (`hass.config_entries.async_setup` or direct `async_setup_entry` + platform setup, matching existing test style); locate the number entity in the entity registry/state machine; assert key/unique id, `mode == BOX`, `device_class == DISTANCE`, unit mm, step 1, and `native_min_value == 500` / `native_max_value == 1300` before any limit notification.
    2. `test_height_limits_configuration_updates_coordinator_and_number` — connect via the fake client, push the 0x07 packet (1200, 600); `wait_until` coordinator attrs; assert `coordinator.height_limit_max_mm == 1200`, `height_limit_min_mm == 600`, and the number's `native_max_value == 1200` / `native_min_value == 600`.
    3. `test_height_limit_max_min_events_update_individually` — push 0x21 (1100) then 0x22 (650); assert each coordinator attr and the number's bounds update independently.
    4. `test_set_value_issues_move_command` — with the coordinator connected, call `number.async_set_native_value(800)`; assert the fake client's `writes` contain exactly one write to `DESK_CONFIG.input_char_uuid` whose bytes encode opcode 0x1B with payload `800.to_bytes(2, "big")`; assert `coordinator._move_in_flight is False` afterwards.
    5. `test_second_set_while_in_flight_is_rejected` — set `coordinator._move_in_flight = True` (white-box guard check); call `async_move_to_height(800)`; assert `UpliftDeskMoveInFlightError` is raised and no new write was recorded; assert the flag is still `True` (the in-flight command owns it).
    6. `test_locked_desk_rejects_move` — set `coordinator._desk.lock_status = DeskLockStatus.LOCKED`; call `async_move_to_height(800)`; assert `UpliftDeskLockedError` raised and no 0x1B write issued. (Optional variant: `lock_status = None` → proceeds.)
    7. `test_number_state_mirrors_current_height` — push a 0x01 height packet (750 mm) via `client.simulate_notification`; `wait_until`; assert `number.native_value == 750.0` (and `coordinator.height_mm == 750.0`).
    8. `test_number_unavailable_when_disconnected` — `coordinator._desk = None` (or simulate a drop and let teardown run); assert `number.available is False`; after reconnect, `True`.
  - Keep the suite green with the existing `instant_sleep` fixture (no real waits).
- **Acceptance Criteria**:
  - `python -m pytest tests/ -v` passes (new + pre-existing tests).
  - Tests 4–6 would fail if coalescing or the lock guard were removed; test 2 would fail if the `HEIGHT_LIMITS_CONFIGURATION` subscription were missing.
  - No changes to `tests/conftest.py` are required; if a shared helper is genuinely needed, it must be additive.

### 7. Final Validation
- **Task ID**: validate-all
- **Depends On**: coordinator-height-limits, coordinator-move-to-height, number-entity, platform-wiring, translations, tests
- **Assigned To**: setpoint-validator
- **Agent**: validator
- **Checks**:
  - Run all validation commands (pytest, compileall, import smoke, JSON validation, hassfest if available).
  - Review the full diff against every acceptance criterion in Tasks 1–6.
  - Verify out-of-scope items are untouched: no stop/move buttons, no lock entity, no limit-setting entities, no "moving" sensor, no changes to `config_flow.py`, `manifest.json`, `sensor.py`, `button.py`, or the project-1 (re)connect/reconnect code paths.
  - Verify by inspection: the `_move_in_flight` flag is cleared on every path out of `async_move_to_height` (including exceptions); the three limit subscriptions are inside the (re)connect cycle (re-registered per cycle); `async_set_native_value` neither writes state nor swallows errors.
  - Verify `strings.json` / `translations/en.json` / `icons.json` are valid JSON and in lockstep.
- **Acceptance Criteria**:
  - All validation commands pass; all acceptance criteria from Tasks 1–6 met; any deviation filed as blocking or documented non-blocking.

### 8. Documentation
- **Task ID**: generate-docs
- **Depends On**: validate-all
- **Assigned To**: setpoint-documenter
- **Agent**: documenter
- **Actions**:
  - Read the plan file and implementation files.
  - Generate documentation in `app_docs/`: a short "Setting a Custom Height" guide — what the `Height Setpoint` entity is, that it shows the current height and moves the desk when a value is confirmed, that its bounds come from the desk's configured limits (fallback 500–1300 mm until known), that a second set within ~1–2 s of the previous one is rejected while the command is in flight, and that a locked desk reports a clear error.
- **Acceptance Criteria**:
  - `app_docs/` contains the guide; it documents the in-flight rejection and locked-desk behaviors; no implementation code is included.

## Acceptance Criteria

1. **One new entity:** a `number` entity `desk_height_setpoint` is created per config entry, with `NumberMode.BOX`, `NumberDeviceClass.DISTANCE`, mm native unit, 1 mm step, unique id `<address>_desk_height_setpoint`, and `available` in lockstep with `coordinator.is_connected`.
2. **Dynamic limits:** the coordinator stores height limits from `HEIGHT_LIMITS_CONFIGURATION` and live-updates from `HEIGHT_LIMIT_MAX`/`HEIGHT_LIMIT_MIN`; the number's min/max follow the coordinator (reported values, else 500/1300 fallback, with the `min >= max` defensive guard).
3. **Set path:** `number.set_value` → `async_set_native_value` → `coordinator.async_move_to_height` → `controller.move_to_specified_height(int mm)` via `_get_or_establish_controller()`; exactly one 0x1B write per accepted set.
4. **Coalescing:** at most one move command in flight; a second concurrent set raises `UpliftDeskMoveInFlightError` (clean service error), issues no write, and the desk keeps moving to the first target; the flag is always cleared when the in-flight command finishes.
5. **Locked desk:** `async_move_to_height` raises `UpliftDeskLockedError` (no write) when the controller last reported `DeskLockStatus.LOCKED`; `None` (unknown) proceeds.
6. **State semantics:** the number's state mirrors the desk's current height via HEIGHT notifications; setting a value does not by itself change the state.
7. **No regressions:** full pre-existing test suite passes; sensor/buttons/config-flow/manifest untouched; hassfest/JSON validation pass.
8. **Docs:** `app_docs/` documents the new entity and its edge-case behaviors.

## Validation Commands

- `python -m pytest tests/ -v` — run the full mocked-BLE test suite (new `tests/test_number.py` + pre-existing).
- `python -m compileall custom_components/uplift_desk -q` — byte-compile check.
- `python -c "import custom_components.uplift_desk.number"` (in the HA test venv) — module import smoke test.
- `python -m json.tool custom_components/uplift_desk/strings.json > /dev/null && python -m json.tool custom_components/uplift_desk/icons.json > /dev/null && python -m json.tool custom_components/uplift_desk/translations/en.json > /dev/null` — JSON validity.
- `hassfest --action validate` (or the existing `Validate with hassfest` GitHub workflow) — manifest/strings validation.
- Manual (optional, physical desk): set a value in the UI, watch the desk move and the number track it; set a second value within ~2 s and confirm the rejected-service-call message; lock the desk (keypad) and confirm the clear locked error.

## Notes

- **Why BOX mode is load-bearing:** the user explicitly rejected a slider because an accidental bump would physically move the desk. BOX requires typing and confirming; combined with the in-flight rejection, the worst case for a burst of sets is a visible service error, never a queued pile-up of BLE writes.
- **In-flight window ≠ physical move:** the guard covers only the BLE command duration (wake preamble + `notification_timeout`, ~1–1.5 s). The desk may still be moving minutes later; a later set is accepted and the firmware retargets. Do not "improve" the guard to track the whole physical move — there is no reliable "movement complete" event in the protocol, and it would block legitimate retargets.
- **`async_set_updated_data(self._desk)` as a token:** consistent with the existing coordinator usage (the "data" is the controller; entities read coordinator attributes). The new limit callbacks follow the same pattern — do not switch to a data-class payload in this feature.
- **Lock-state staleness:** the protocol has no "request lock status" command; `lock_status` is `None` until a 0x1F notification arrives. We accept that a desk locked *before* our (re)connect and never changed is treated as unknown (firmware rejects the move if truly locked). Tracking `LOCK_STATUS` on the coordinator for this check was considered and rejected as out of scope (lock *entities* are out of scope; the at-set-time check is the minimal correct guard).
- **Fallback range rationale:** 500–1300 mm brackets the mechanical range of common Uplift desks; it is used only until the desk reports its real limits, which normally happens during setup (the existing `request_height_limits()` call in `async_read_desk_height`).
- **Version caveat:** the local library docs are for `uplift-ble==0.5.0` while the manifest pins `0.7.0`; the APIs this plan uses (`move_to_specified_height`, `request_height_limits`, `HEIGHT_LIMITS_CONFIGURATION`/`HEIGHT_LIMIT_MAX`/`HEIGHT_LIMIT_MIN` events, `lock_status`) are present in both. If a signature differs in 0.7.0, the builder should confirm against the installed package before finalizing mocks.
- **HA number component facts relied on (verified against current core):** `NumberEntityDescription` supports `device_class`, `native_min_value`, `native_max_value`, `native_step`, `native_unit_of_measurement`, `mode`; `NumberMode` = AUTO/BOX/SLIDER; `NumberDeviceClass.DISTANCE` accepts `UnitOfLength`; `number.set_value` validates min/max and converts locale→native before calling `async_set_native_value`; `native_min_value`/`native_max_value`/`native_step`/`native_value`/`mode`/`device_class` are in `CACHED_PROPERTIES_WITH_ATTR_`, so `_attr_` assignment in `_handle_coordinator_update` correctly invalidates the caches.
