# Plan: 3.01 - Redesign Height Setpoint to True Setpoint Semantics

## Task Description

Redesign the existing `DeskHeightSetpointNumber` entity (`custom_components/uplift_desk/number.py`) so it behaves like the setpoint entities of reference HA integrations (Bambu Lab, Creality): the **number entity holds the commanded *target* height**, the **existing height sensor stays the live current position**, and the number shows `unknown` whenever no move toward a target is in progress.

The Uplift desk firmware does not report a target height. The only movement-related notification is opcode `0x01` (current height). Verified against the `uplift-ble` library (`DeskController._process_notification_packet`): notification opcodes are `0x01` height, `0x02` error, `0x04` reset, `0x07` limits-config, `0x0E` units, `0x19` touch mode, `0x1F` lock, `0x21`/`0x22` min/max limits, `0x25`-`0x28` presets; move-to-height is the write-only command `0x1B` via `DeskController.move_to_specified_height` (2-byte big-endian mm payload; the `command_writer` decorator sends a wake preamble, writes, then waits `notification_timeout` = 1.0 s). There is no target-height notification to mirror, so the integration must track the setpoint itself.

This is a **redesign** (user-approved "Option 4") of an already-shipped entity. The original addition is described by the stale spec at `.agent/specs/2-number-height-setpoint/unimplemented/01-number-height-setpoint/plan.md` (Sep 7) - that spec is **superseded and must not be read, followed, extended, or written to**. This plan lives in its own fresh project directory.

Task type: refactor. Complexity: medium.

## Objective

After this change:

1. Setting a value on the **Height Setpoint** number immediately shows the commanded target (optimistic), even while an on-demand (re)connect cycle is still in progress.
2. While the desk moves toward the target, the number keeps showing the target while the Height sensor streams the live position.
3. The setpoint returns to `unknown` (coordinator state `None`) when:
   - the desk **arrives** (reported height within the arrival tolerance of the target, or the target is crossed between two notifications);
   - the move is **interrupted** (a height notification moves away from the target beyond the jitter epsilon - keypad press, preset button, another control);
   - the **command fails** (BLE write error, locked desk, reconnect failure) - a failed set never leaves a stale target, and a previously active setpoint from an earlier successful command is preserved;
   - the desk **disconnects** (unexpected drop or intentional unload) - and therefore is `unknown` again after a reconnect.
4. The setpoint is **not persisted**: after a Home Assistant restart or a reconnect it is `unknown` (initial state after setup is `unknown`).
5. Everything else about the entity is unchanged: `available` = connected, dynamic min/max from the desk's reported limits (500-1300 mm fallback), BOX mode, DISTANCE device class, mm native unit, 1 mm step, unique_id `"{address}_desk_height_setpoint"`, translation key `desk_height_setpoint`, icon.
6. The full existing test suite passes; reworked and new tests cover the lifecycle above.
7. User docs (`app_docs/setting-a-custom-height.md`, `app_docs/whats-changed.md`) describe the new semantics.

## Problem Statement

Today `DeskHeightSetpointNumber._handle_coordinator_update` mirrors the desk's *current* height into `_attr_native_value` on every coordinator update (`number.py` line 76, initialized at line 61 from `coordinator.height_mm`). When the user types a target, `async_set_native_value` -> `coordinator.async_move_to_height(value)` writes the `0x1B` command, the desk starts moving, and its live `0x01` height notifications update `coordinator.height_mm` - so the entity's value immediately starts tracking the desk's live position and the user loses sight of the target they commanded. `app_docs/setting-a-custom-height.md` even documents this as intended ("It is not a target display"). This diverges from how users understand "setpoint" (and from reference integrations' temperature-setpoint behavior: number = target, sensor = current) and makes the entity useless for watching a move progress toward the target.

## Solution Approach

**Single source of truth in the coordinator.** Add `_height_setpoint_mm: int | None` (read-only property `height_setpoint_mm`) to `UpliftDeskBluetoothCoordinator`. The number entity becomes a thin view of that value (it stops mirroring `coordinator.height_mm`).

**Optimistic set, before the BLE write.** In `async_move_to_height`, after the in-flight check passes, store the target and push a coordinator update *before* acquiring the controller / writing the command. Why not set-after-success:

- `DeskController.move_to_specified_height` is wrapped by `uplift_ble`'s `command_writer`, which sends a wake preamble, writes the packet, and then **waits `notification_timeout` (1.0 s)** before returning. In real operation the desk emits height notifications during that window. If the setpoint were stored only after the call returns, a short move (e.g. 5 mm - the desk moves ~30 mm/s) could fully arrive inside the 1 s window: the arrival notifications would be processed while the setpoint is still `None` (so nothing could be "cleared" later) and no further notifications arrive once the desk is at rest - a stale setpoint that never clears. Setting optimistically before the write guarantees every subsequent notification reconciles against the active target.
- Immediate optimistic feedback matches the reference integrations and gives the user visible confirmation even during an on-demand reconnect (which can take seconds).

**Save/restore on failure.** Capture the previous setpoint before the optimistic set; on any exception (locked desk, BLE write failure, reconnect failure, unload) restore the previous value and push an update. Guarantees: a failed fresh set leaves `None`; a failed *retarget* restores the still-valid earlier target instead of wiping it; an in-flight rejection (raised before any setpoint mutation) leaves the active setpoint untouched.

**Reconcile in the height callback.** `_async_height_notify_callback` captures the previous height (current `self.height_mm`) before updating it, then runs a small reconciliation helper against the active setpoint:

- **Arrival (tolerance):** clear when `abs(height_mm - setpoint) <= HEIGHT_SETPOINT_ARRIVAL_TOLERANCE_MM`.
- **Arrival (crossing):** clear when the desk *crossed* the target between two notifications - `(previous - setpoint) * (new - setpoint) < 0`. Sparse notifications can jump over the tolerance window (e.g. a superseding preset move passing through the target).
- **Interruption:** clear when the desk moved *away* from the target: `delta = new - previous`, `to_target = setpoint - previous`, and `abs(delta) > HEIGHT_SETPOINT_INTERRUPTION_EPSILON_MM and delta * to_target < 0`. The epsilon filters single-quantum encoder jitter (see tolerance values below).

**Tolerance values (deviation from the recommended 2 mm - justified).** The desk reports height in tenths of its *display unit*: 1 mm steps in cm mode, **2.54 mm steps in inch mode** (`uplift_ble.utils.convert_in_to_mm`). The desk may stop 1-2 mm short of the target. Worst case in inch mode: 2 mm (stop short) + 1.27 mm (half a 2.54 mm quantum) = 3.27 mm away from the commanded value - a 2 mm tolerance would fail to clear the setpoint on arrival for inch-mode desks. Therefore:

- `HEIGHT_SETPOINT_ARRIVAL_TOLERANCE_MM = 4.0` (covers the 3.27 mm worst case with margin; cost is the setpoint may clear up to 4 mm before the physical stop, which is cosmetically fine since the desk stops within ~2 mm).
- `HEIGHT_SETPOINT_INTERRUPTION_EPSILON_MM = 1.0` (1 mm is the smallest cm-mode quantum; a single-quantum backward blip is indistinguishable from encoder noise, while a real manual interruption produces multi-quantum movement; inch-mode quantums of 2.54 mm always exceed it).

Both are named constants in `const.py` so they can be tuned from field data without hunting through logic.

**Lifecycle.** The setpoint is cleared on disconnect in both disconnect paths: `_async_handle_unexpected_disconnect` (unexpected drop - cleared just before the existing `async_update_listeners()` so one push delivers unavailability + setpoint clear) and `async_disconnect` (intentional unload - defensive; entities are already unloaded at that point, no push needed). No clear is needed in `async_connect`: the setpoint is always `None` at connect time (it is only set by `async_move_to_height`, which requires or triggers a connection, and any disconnect has already cleared it). After a successful reconnect, `_refresh_state` pushes an update and the entity re-reads `None` -> `unknown`.

**Edge cases (resolved):**

- *Setpoint equal to (or within tolerance of) current height:* the command is still sent (unchanged behavior - the firmware accepts a move to the current position harmlessly), but after the write succeeds, if `coordinator.height_mm` is known and within the arrival tolerance of the target, the setpoint is cleared synchronously and a listener update is pushed (treated as immediate arrival). If `height_mm` is unknown (`None`), the setpoint stays and the first height notification reconciles it (within tolerance -> cleared on that tick).
- *Commanding beyond desk limits:* HA's number service validates the value against the entity's min/max (verified in the installed HA 2026.2 `number` component: `async_set_value` raises `ServiceValidationError` out of range and clamps to the native range before calling `async_set_native_value`), so this is mostly prevented. Residual: a value set while the fallback range (500-1300) is still in effect that the firmware clamps to a different position leaves the setpoint lingering until the next set - documented as a known limitation (see docs task), not handled with timers in this change.
- *Command failure:* covered by save/restore above.
- *Retarget while moving (e.g. 800 -> 700 while rising):* the new target overwrites the old. During the firmware's reversal transient the desk may still be moving away from the new target, so the interruption check may clear the new setpoint early (the entity briefly goes `unknown`, then stays `unknown` until arrival). This is acceptable: `unknown` is an honest state, and the desk still arrives. Documented in the plan notes; not a defect.
- *Superseding move passing through the target (preset in the same direction):* the crossing check clears the setpoint when the desk passes the target.
- *Mid-command link drop:* the in-flight write raises `BleakError`, save/restore clears the setpoint, the error propagates to the service caller, and the disconnect handler clears again (no-op). The entity goes unavailable; the user can set again after reconnect.

## Relevant Files

- `custom_components/uplift_desk/coordinator.py` - `UpliftDeskBluetoothCoordinator`: gains setpoint state, the reconciliation helper, optimistic set/save-restore in `async_move_to_height`, and disconnect clears. Key existing members: `height_mm` (current height, public attr), `height_limit_min_mm`/`height_limit_max_mm`, `async_move_to_height` (in-flight guard `_move_in_flight`, lock guard, `int(round())`, `controller.move_to_specified_height`), `_async_height_notify_callback` (registered on `DeskEventType.HEIGHT`; sets `height_mm` + `async_set_updated_data(self._desk)`), `_async_handle_unexpected_disconnect` (teardown + `async_update_listeners()` + reconnect loop), `async_disconnect`, `is_connected`.
- `custom_components/uplift_desk/number.py` - `DeskHeightSetpointNumber` (CoordinatorEntity + NumberEntity): stops mirroring `coordinator.height_mm`; reads `coordinator.height_setpoint_mm` instead. `async_set_native_value` (delegates to the coordinator), `available`, `_effective_limits`, `device_info`, unique_id, translation key, mode, device class, unit, step all unchanged.
- `custom_components/uplift_desk/const.py` - gains the two tolerance constants. Existing: `DEFAULT_HEIGHT_LIMIT_MIN_MM = 500`, `DEFAULT_HEIGHT_LIMIT_MAX_MM = 1300`.
- `custom_components/uplift_desk/sensor.py` - **unchanged** (current-height sensor is exactly what "sensor = current value" wants).
- `custom_components/uplift_desk/button.py`, `config_flow.py`, `strings.json`, `translations/en.json`, `icons.json` - unchanged (entity name "Height Setpoint", translation key `desk_height_setpoint`, icon all stay).
- `tests/conftest.py` - fake BLE harness (read-only for this task): `FakeBleakClient` (records `writes`, `simulate_notification`, `simulate_disconnect`), `make_notification_packet(opcode, payload)` (frame `F2 F2 <opcode> <len> <payload> <checksum> 7E`), `wait_until`, `fake_ble`/`coordinator` fixtures, `instant_sleep` (collapses `asyncio.sleep`).
- `tests/test_number.py` - reworked (see test plan). Existing helpers to reuse: `make_height_packet(tenths)` (opcode `0x01`, 2-byte BE tenths + 1 unknown byte), `make_units_packet(byte)` (`0x0E`), `make_lock_packet(byte)` (`0x1F`), `make_command_packet(opcode, payload)` (F1 F1 header), `_setup_entry` (full config-entry setup; stubs the bluetooth component), `_number_entity_id` (registry lookup of `"{DESK_ADDRESS}_desk_height_setpoint"`).
- `tests/test_setpoint.py` - **new file** (coordinator-level setpoint lifecycle).
- `app_docs/setting-a-custom-height.md` - rewritten for the new semantics.
- `app_docs/whats-changed.md` - new upgrade-notes bullet.
- `app_docs/disconnect-reconnect-behavior.md` - optional one-line update (include the Height Setpoint in the "unavailable" list).
- `README.md` - optional: the entity list says "5 entities" and omits the Height Setpoint number (stale since the original addition); add it as entity 6.

### New Files

- `tests/test_setpoint.py`
- `.agent/specs/3-height-setpoint-redesign/unimplemented/01-height-setpoint-redesign/plan.md` (this document)

## Team Orchestration

> **Worktree Isolation**: The team-lead creates an isolated git worktree for this spec using `~/.config/opencode/scripts/worktree-create.sh`. All builders work inside this worktree. After final validation, changes are merged back via `~/.config/opencode/scripts/worktree-merge.sh`.

The team-lead agent will orchestrate execution using these team members:

### Team Members

- **Builder**
  - Name: setpoint-builder
  - Role: Implement coordinator setpoint tracking, number entity changes, and the test rework
  - Agent: builder

- **Validator**
  - Name: setpoint-validator
  - Role: Verify the full test suite passes and the acceptance criteria are met
  - Agent: validator

- **Documenter**
  - Name: setpoint-documenter
  - Role: Rewrite `app_docs/setting-a-custom-height.md`, add the `whats-changed.md` bullet, apply the optional doc touch-ups
  - Agent: documenter

## Step by Step Tasks

### 1. Coordinator setpoint tracking
- **Task ID**: coordinator-setpoint
- **Depends On**: none
- **Assigned To**: setpoint-builder
- **Agent**: builder
- **Actions**:
  - In `custom_components/uplift_desk/const.py`, add two module constants with docstring-style comments explaining the quantization math:
    - `HEIGHT_SETPOINT_ARRIVAL_TOLERANCE_MM: float = 4.0`
    - `HEIGHT_SETPOINT_INTERRUPTION_EPSILON_MM: float = 1.0`
  - In `custom_components/uplift_desk/coordinator.py`:
    - In `__init__`, add `self._height_setpoint_mm: int | None = None` (next to `self.height_mm`).
    - Add a read-only property:
      ```python
      @property
      def height_setpoint_mm(self) -> int | None:
          """The height (mm) the desk was last commanded to move to, or None."""
          return self._height_setpoint_mm
      ```
    - Rework `async_move_to_height` per the design sketch below. Keep the in-flight guard, the lock guard, `int(round(height_mm))`, and the `finally: self._move_in_flight = False` structure; update the docstring (drop the old "no state update is needed here" comment - a push is now required for the optimistic set and for clears).
    - Add a private helper `_reconcile_height_setpoint(self, previous_height_mm: float | None, height_mm: float) -> None` per the design sketch below.
    - Rework `_async_height_notify_callback` per the design sketch below (also fix the parameter annotation from `int` to `float` - the controller emits converted mm floats).
    - In `_async_handle_unexpected_disconnect`, clear `self._height_setpoint_mm = None` (with a `_LOGGER.debug` line) just before the existing `self.async_update_listeners()` call.
    - In `async_disconnect`, clear `self._height_setpoint_mm = None` (with a `_LOGGER.debug` line) near the top; no listener push needed (entity platforms are already unloaded before `async_unload_entry` calls this).
  - Design sketch for `async_move_to_height` (builder adapts to existing style; logging via `_LOGGER`):
    ```python
    async def async_move_to_height(self, height_mm: int | float) -> None:
        if self._move_in_flight:
            raise UpliftDeskMoveInFlightError(...)  # unchanged message
        self._move_in_flight = True
        target_mm = int(round(height_mm))
        previous_setpoint_mm = self._height_setpoint_mm
        # Optimistic: show the commanded target immediately, even while a
        # (re)connect cycle is still in progress.
        self._height_setpoint_mm = target_mm
        self.async_set_updated_data(self._desk)
        try:
            controller = await self._get_or_establish_controller()
            if controller.lock_status is DeskLockStatus.LOCKED:
                raise UpliftDeskLockedError(...)  # unchanged message
            await controller.move_to_specified_height(target_mm)
            _LOGGER.debug("Commanded desk %s to move to %d mm", self.desk_info, target_mm)
            if (self.height_mm is not None
                    and abs(self.height_mm - target_mm) <= HEIGHT_SETPOINT_ARRIVAL_TOLERANCE_MM):
                # Desk already at (within tolerance of) the commanded height:
                # nothing left to track.
                self._height_setpoint_mm = None
                self.async_update_listeners()
        except Exception:
            # Command failed (write error, locked, reconnect failure): restore
            # the previous setpoint so a failed set never leaves a stale target.
            self._height_setpoint_mm = previous_setpoint_mm
            self.async_update_listeners()
            raise
        finally:
            self._move_in_flight = False
    ```
  - Design sketch for the reconciliation helper:
    ```python
    def _reconcile_height_setpoint(
        self, previous_height_mm: float | None, height_mm: float
    ) -> None:
        setpoint_mm = self._height_setpoint_mm
        if setpoint_mm is None:
            return
        # Arrival: reported height within tolerance of the target.
        if abs(height_mm - setpoint_mm) <= HEIGHT_SETPOINT_ARRIVAL_TOLERANCE_MM:
            self._height_setpoint_mm = None
            _LOGGER.debug("Desk %s arrived at setpoint %d mm (reported %s mm)",
                          self.desk_info, setpoint_mm, height_mm)
            return
        if previous_height_mm is None:
            return
        # Arrival: the desk crossed the target between two notifications.
        if (previous_height_mm - setpoint_mm) * (height_mm - setpoint_mm) < 0:
            self._height_setpoint_mm = None
            _LOGGER.debug("Desk %s crossed setpoint %d mm (%s -> %s mm)",
                          self.desk_info, setpoint_mm, previous_height_mm, height_mm)
            return
        # Interruption: desk moved away from the target (keypad/preset/etc.).
        delta = height_mm - previous_height_mm
        to_target = setpoint_mm - previous_height_mm
        if (abs(delta) > HEIGHT_SETPOINT_INTERRUPTION_EPSILON_MM
                and delta * to_target < 0):
            self._height_setpoint_mm = None
            _LOGGER.debug("Desk %s moved away from setpoint %d mm (%s -> %s mm)",
                          self.desk_info, setpoint_mm, previous_height_mm, height_mm)
    ```
  - Design sketch for the height callback:
    ```python
    def _async_height_notify_callback(self, height_mm: float) -> None:
        previous_height_mm = self.height_mm
        self.height_mm = height_mm
        self._reconcile_height_setpoint(previous_height_mm, height_mm)
        _LOGGER.debug("Height notify callback received height: %s mm", height_mm)
        self.async_set_updated_data(self._desk)
    ```
- **Acceptance Criteria**:
  - `coordinator.height_setpoint_mm` is `None` on a fresh coordinator and after `async_disconnect`.
  - A successful `async_move_to_height(800)` leaves `height_setpoint_mm == 800` and issues the `0x1B` write (existing write behavior unchanged, including the wake preamble).
  - A second concurrent call still raises `UpliftDeskMoveInFlightError` without mutating the setpoint; a locked desk still raises `UpliftDeskLockedError` and the setpoint is restored to its pre-call value.
  - A BLE write failure raises and leaves the setpoint at its pre-call value.
  - Height notifications clear the setpoint on arrival (within 4 mm or on crossing) and on interruption (away-move > 1 mm); a 1 mm backward blip does not clear.
  - `async_move_to_height` to a height within 4 mm of the known current height sends the command but leaves `height_setpoint_mm` `None` afterwards.
  - No other coordinator behavior changes (reconnect lifecycle, GATT validation, limits callbacks untouched).

### 2. Number entity setpoint semantics
- **Task ID**: number-entity
- **Depends On**: coordinator-setpoint
- **Assigned To**: setpoint-builder
- **Agent**: builder
- **Actions**:
  - In `custom_components/uplift_desk/number.py`:
    - `__init__`: replace `self._attr_native_value = coordinator.height_mm` with `self._attr_native_value = None` (no active setpoint at setup; the entity shows `unknown`).
    - `_handle_coordinator_update`: replace `self._attr_native_value = self.coordinator.height_mm` with `self._attr_native_value = self.coordinator.height_setpoint_mm`. Keep the `_effective_limits()` min/max refresh and the `self.async_write_ha_state()` call.
    - Update the class docstring to describe the entity as the commanded target height (`unknown` when the desk is at rest / no move in progress).
    - Do NOT change: `async_set_native_value`, `available`, `_effective_limits`, `device_info`, unique_id, translation key, mode, device class, unit, step.
- **Acceptance Criteria**:
  - At entry setup, the number state is `unknown` with min/max 500/1300 fallback and the existing attribute set (unchanged).
  - After a successful set, the state shows the target; after the coordinator clears the setpoint (arrival/interruption/disconnect/failure), the state is `unknown`.
  - Height notifications alone (no set active) leave the number state `unknown` - the old mirroring is gone.
  - min/max still track the 0x07/0x21/0x22 limit notifications via the coordinator.

### 3. Rework tests/test_number.py
- **Task ID**: rework-number-tests
- **Depends On**: number-entity
- **Assigned To**: setpoint-builder
- **Agent**: builder
- **Actions**:
  - Keep, with the noted updates:
    - `test_number_entity_created_with_fallback_limits` - update the comment: state is `unknown` because no setpoint has been set (not "no height notification yet"); keep all attribute assertions.
    - `test_height_limits_configuration_updates_coordinator_and_number` - unchanged.
    - `test_height_limit_max_min_events_update_individually` - unchanged.
    - `test_set_value_issues_move_command` - keep; add `assert coordinator.height_setpoint_mm == 800` after the set.
    - `test_second_set_while_in_flight_is_rejected` - keep; add `assert coordinator.height_setpoint_mm is None` (no setpoint was ever set in this test).
    - `test_locked_desk_rejects_move` - keep; add assertions: after the first successful move `coordinator.height_setpoint_mm == 800`; after the locked rejection it is still `800` (the rejected set did not wipe the earlier target).
  - Delete `test_number_state_mirrors_current_height` (old semantics) and add:
    - `test_set_value_shows_target_while_height_streams_in` - full entry setup; `await hass.services.async_call("number", "set_value", {"entity_id": entity_id, "value": 800}, blocking=True)`; `wait_until` state float == 800.0; push `make_units_packet(0x00)` then `make_height_packet(750)`, `760`, `770` (waiting on `coordinator.height_mm` between pushes); assert the number state is still 800.0 and `coordinator.height_setpoint_mm == 800` while the coordinator height is 770.0.
    - `test_setpoint_clears_on_arrival` - set 800 (state 800); push units cm; heights 750 (state still 800), 790 (state still 800), 798 (|798-800| = 2 <= 4 -> clear); `wait_until` state == `STATE_UNKNOWN`; assert `coordinator.height_setpoint_mm is None`.
    - `test_setpoint_clears_on_interruption` - set 800; push units cm; heights 750, 760 (state 800), 755 (delta -5, away -> clear); `wait_until` state == `STATE_UNKNOWN`; assert setpoint `None`.
    - `test_setpoint_survives_single_mm_jitter` - set 800; push units cm; heights 750, 760, 759 (1 mm backward blip, |delta| == 1 is not > 1); assert state still 800.0 and setpoint 800.
    - `test_setpoint_equal_to_current_height_clears_immediately` - push units cm + `make_height_packet(800)` first (wait `coordinator.height_mm == 800.0`); set 800 via the service (blocking); `wait_until` state == `STATE_UNKNOWN`; assert exactly one `0x1B` move write was issued (command still sent).
  - Strengthen `test_number_unavailable_when_disconnected`:
    - Before the drop, set 800 via the service and wait for state 800.
    - After the drop (unavailable): assert `coordinator.height_setpoint_mm is None` (cleared on disconnect).
    - After the reconnect (available): assert state == `STATE_UNKNOWN` exactly (no stale setpoint), replacing the loose `!= STATE_UNAVAILABLE` assertion.
  - Use `wait_until` (from conftest) for all state assertions after coordinator pushes, because `async_update_listeners` schedules listener tasks rather than running them synchronously.
- **Acceptance Criteria**:
  - All tests in `tests/test_number.py` pass and each asserts the new setpoint semantics (no test asserts the old current-height mirroring).
  - Service-level sets go through `hass.services.async_call("number", "set_value", ..., blocking=True)` and are asserted via `hass.states` + `coordinator.height_setpoint_mm`.

### 4. New tests/test_setpoint.py (coordinator-level lifecycle)
- **Task ID**: setpoint-lifecycle-tests
- **Depends On**: number-entity
- **Assigned To**: setpoint-builder
- **Agent**: builder
- **Actions**:
  - Create `tests/test_setpoint.py` using the `coordinator` + `fake_ble` fixtures (no full entry setup). Pattern for every test: `client = fake_ble.valid_client(); fake_ble.queue_client(client); await coordinator.async_connect()`, then push `make_units_packet(0x00)` (cm) before any height packet (the controller only emits `HEIGHT` once the display unit is known), then `await coordinator.async_move_to_height(...)` and `client.simulate_notification(make_height_packet(tenths))`. Reuse packet builders by importing from `tests/test_number.py` (as `test_fallback_unit.py` does with `from .test_notifications import ...`) or re-define locally if the import is awkward.
  - Tests (each a separate test function):
    1. `test_setpoint_set_on_successful_move` - units cm, height 700; `async_move_to_height(800)`; assert `coordinator.height_setpoint_mm == 800` and one `0x1B` write (reuse the `make_command_packet` check from `test_number.py`).
    2. `test_setpoint_cleared_on_arrival` - move to 800; heights 750, 798; assert setpoint `None` after 798 (still 800 after 750).
    3. `test_setpoint_cleared_on_crossing_target` - move to 800; heights 700 (known start), 790 (still 800), 810 (crosses 800 without landing within 4 mm -> clear); assert `None`.
    4. `test_setpoint_cleared_on_interruption` - move to 800; heights 750, 760, 755; assert `None` after 755.
    5. `test_setpoint_survives_single_mm_jitter` - move to 800; heights 750, 760, 759; assert still 800.
    6. `test_setpoint_clears_immediately_when_desk_at_target` - units cm, height 800; `async_move_to_height(800)`; assert setpoint `None` afterwards and one `0x1B` write.
    7. `test_failed_move_restores_previous_setpoint` - parameterize `previous` over `(None, 800)`: if 800, do a successful `async_move_to_height(800)` first (desk silent, setpoint stays 800); patch the client's `write_gatt_char` to raise `BleakError` (e.g. `monkeypatch.setattr(client, "write_gatt_char", AsyncMock(side_effect=BleakError("write failed")))`); `with pytest.raises(BleakError): await coordinator.async_move_to_height(700)`; assert `coordinator.height_setpoint_mm == previous`.
    8. `test_locked_move_preserves_previous_setpoint` - move to 800 (setpoint 800); push `make_lock_packet(0x01)` and wait for `coordinator._desk.lock_status is DeskLockStatus.LOCKED`; `with pytest.raises(UpliftDeskLockedError): await coordinator.async_move_to_height(700)`; assert setpoint still 800.
    9. `test_in_flight_rejection_preserves_setpoint` - move to 800 (setpoint 800); white-box `coordinator._move_in_flight = True`; `with pytest.raises(UpliftDeskMoveInFlightError): await coordinator.async_move_to_height(700)`; assert setpoint still 800.
    10. `test_setpoint_cleared_on_unexpected_disconnect` - move to 800; `client.simulate_disconnect()`; `wait_until(lambda: coordinator._desk is None)`; assert setpoint `None`.
    11. `test_setpoint_cleared_on_intentional_disconnect` - move to 800; `await coordinator.async_disconnect()`; assert setpoint `None`.
    12. `test_setpoint_none_after_reconnect` - first + second `fake_ble.valid_client()` queued; move to 800 on client 1; `client1.simulate_disconnect()`; `wait_until(lambda: coordinator.is_connected and coordinator._desk.client is client2)`; assert setpoint `None`.
    13. `test_arrival_in_inch_mode_within_quantization` - push `make_units_packet(0x01)` (inches); `make_height_packet(300)` (30.0 in = 762 mm); `async_move_to_height(800)`; push `make_height_packet(310)` (787.4 mm - still 800), then `make_height_packet(315)` (799.5 mm; |799.5-800| = 0.5 <= 4 -> clear); assert `None`. This test documents the tolerance choice for inch-mode desks.
- **Acceptance Criteria**:
  - All 13 tests pass; each exercises the real coordinator + real `DeskController` against the fake BLE client (no mocking of coordinator internals except the documented white-box `_move_in_flight` flag and the `write_gatt_char` failure injection).
  - The suite still passes with `instant_sleep` (no real-time waits).

### 5. Run the full test suite
- **Task ID**: run-suite
- **Depends On**: rework-number-tests, setpoint-lifecycle-tests
- **Assigned To**: setpoint-validator
- **Agent**: validator
- **Checks**:
  - Ensure test dependencies are installed for the local interpreter (check first: `python -c "import pytest_homeassistant_custom_component"`; if missing: `python -m pip install -r requirements_test.txt`). CI uses Python 3.13; use whatever local interpreter has the dependencies installed.
  - `python -m pytest tests/ -v` - the **full** suite must pass (all of `test_connection_serialization.py`, `test_coordinator_reconnect.py`, `test_fallback_unit.py`, `test_notifications.py`, `test_number.py`, `test_setpoint.py`, `test_setup.py`, `test_unload.py`), not just the new/changed files.
  - If a pre-existing test breaks, diagnose whether the implementation or the test is wrong against this plan; fix accordingly and re-run.

### 6. Final Validation
- **Task ID**: validate-all
- **Depends On**: coordinator-setpoint, number-entity, rework-number-tests, setpoint-lifecycle-tests, run-suite
- **Assigned To**: setpoint-validator
- **Agent**: validator
- **Checks**:
  - Re-run `python -m pytest tests/ -v` and confirm the full suite is green.
  - Verify each acceptance criterion in the "Acceptance Criteria" section below against the final code (read `coordinator.py`, `number.py`, `const.py`, the two test files).
  - Confirm no changes were made to: `sensor.py`, `button.py`, `config_flow.py`, `strings.json`, `translations/`, `icons.json`, `manifest.json`, `tests/conftest.py`, and the stale spec directory `.agent/specs/2-number-height-setpoint/`.
  - Confirm style matches the repo: type hints on new functions/properties, `_LOGGER` for logging, docstrings on new public members, no new dependencies.

### 7. Documentation
- **Task ID**: generate-docs
- **Depends On**: validate-all
- **Assigned To**: setpoint-documenter
- **Agent**: documenter
- **Actions**:
  - Read the plan file and the implementation files, then:
  - Rewrite `app_docs/setting-a-custom-height.md` for the new semantics:
    - "What the Height Setpoint shows" -> it is now a true **target display**: `unknown` at rest; shows the commanded height while the desk is moving toward it; returns to `unknown` when the desk arrives (within a few mm), when the move is interrupted (keypad, preset, any other control), when the desk disconnects, or after a Home Assistant restart. State clearly that the setpoint is **not persisted** (the desk firmware does not report a target) and that the **Height sensor is the live position** - use it to watch the desk approach the target.
    - Update "Setting a height" step 3: the desk moves; while it moves the setpoint shows the target and the sensor tracks the live position; on arrival the setpoint returns to `unknown`.
    - Keep: the type-and-confirm box rationale, the allowed-range/units section (limits + 500-1300 fallback + locale units), and the "If a set is rejected" section (in-flight, locked).
    - Add a short known-limitation note: if the desk clamps a command to a position different from the commanded one (for example a value set while the 500-1300 fallback range is still in effect, before the desk reports its real limits), the setpoint can remain visible until the next set.
  - Add a new bullet at the top of the bullet list in `app_docs/whats-changed.md`, e.g.: "**Height Setpoint is now a true target display.** The Height Setpoint number shows the height you commanded while the desk is moving toward it, and returns to *unknown* when the desk arrives, the move is interrupted (keypad or preset), or the desk disconnects. Previously it mirrored the desk's live height, so the target you typed was immediately replaced by the moving desk's position. The Height sensor remains the live position."
  - Optional touch-ups (apply if time permits; both are one-liners):
    - `app_docs/disconnect-reconnect-behavior.md` step 2: include the Height Setpoint in the list of entities that flip to unavailable.
    - `README.md`: the "currently provides 5 entities" list omits the Height Setpoint number; add it as entity 6 (a number to move the desk to a specific height).
- **Acceptance Criteria**:
  - `setting-a-custom-height.md` no longer contains the old "shows the desk's current height / not a target display" wording and accurately describes: unknown at rest, target while moving, sensor = live position, clears on arrival/interruption/disconnect/restart, not persisted, known limitation.
  - `whats-changed.md` has the new bullet and still reads coherently with the existing bullets.
  - After the doc changes, `python -m pytest tests/ -v` still passes (docs don't affect tests; confirms the tree is green end-to-end).

## Acceptance Criteria

1. **Setpoint state lives in the coordinator**: `UpliftDeskBluetoothCoordinator` exposes read-only `height_setpoint_mm` (`int | None`); a fresh coordinator reports `None`.
2. **Optimistic set**: a successful `async_move_to_height(800)` leaves `height_setpoint_mm == 800` and the number entity state `800` (via a coordinator listener push), even before any height notification arrives.
3. **Target display while moving**: successive `0x01` height notifications below the target do not change the number state; the sensor follows the live height.
4. **Arrival clears**: a reported height within 4 mm of the target (or a crossing of the target between two notifications) clears the setpoint; the number state becomes `unknown`.
5. **Interruption clears**: a height notification moving away from the target by more than 1 mm clears the setpoint; a 1 mm backward blip does not.
6. **Command failure leaves no stale setpoint**: a failed write restores the pre-call setpoint (`None` for a fresh set, the earlier target for a failed retarget); `UpliftDeskMoveInFlightError` and `UpliftDeskLockedError` preserve the active setpoint.
7. **Setpoint equal to current height**: the command is still sent, but the setpoint is cleared immediately (synchronously after the write, or on the first reconciling notification when the current height was unknown).
8. **Lifecycle**: the setpoint is cleared on unexpected disconnect and on intentional disconnect; after a reconnect the entity is `unknown` (no stale setpoint); after HA restart/setup the entity is `unknown`.
9. **Entity surface unchanged**: unique_id, translation key, name, icon, BOX mode, DISTANCE device class, mm unit, 1 mm step, `available` = connected, and dynamic min/max (desk limits with 500-1300 fallback) all unchanged; `sensor.py` unchanged.
10. **Tests**: `python -m pytest tests/ -v` passes for the entire suite (8 test modules, including reworked `test_number.py` and new `test_setpoint.py`).
11. **Docs**: `app_docs/setting-a-custom-height.md` describes the new target-display semantics; `app_docs/whats-changed.md` has the upgrade bullet; the stale spec directory `.agent/specs/2-number-height-setpoint/` is untouched.

## Validation Commands

- `python -c "import pytest_homeassistant_custom_component"` - check test deps are installed for the local interpreter (CI uses Python 3.13; install with `python -m pip install -r requirements_test.txt` if missing).
- `python -m pytest tests/ -v` - full test suite (must pass entirely).
- `python -m pytest tests/test_number.py tests/test_setpoint.py -v` - focused run on the reworked/new tests.
- No linter is configured (CI runs `hassfest` + `pytest` only); match existing code style manually (type hints, `_LOGGER` logging, docstrings).

## Notes

- **Stale spec warning**: `.agent/specs/2-number-height-setpoint/unimplemented/01-number-height-setpoint/plan.md` (Sep 7) describes the *original addition* of this entity, which is already implemented. It must not be read, followed, extended, or written to; this spec supersedes it for the entity's semantics.
- **Branch context**: the working branch is `v2.0.0`; this change is user-visible behavior change for an existing entity, so the `whats-changed.md` bullet matters for the release notes.
- **Why the coordinator, not the entity, owns the setpoint**: the height callback, the disconnect handlers, and the move command all live in the coordinator; keeping the setpoint there makes it the single source of truth and lets the entity stay a dumb view (mirrors the existing `height_mm` pattern).
- **Why optimistic-before-write (not set-after-success)**: `uplift_ble`'s `command_writer` waits 1.0 s after the write; a short move can fully arrive inside that window, and a setpoint stored after the call would then never be reconciled (no further notifications at rest) - a permanent stale target. See Solution Approach.
- **Tolerance deviation (2 mm -> 4 mm)**: inch-mode desks report height in 2.54 mm steps; with the desk stopping up to 2 mm short, the reported arrival position can be ~3.27 mm from the commanded value. 4 mm covers this; the constants are in `const.py` for easy tuning.
- **Retarget transient**: a retarget while moving can be cleared early by the interruption check during the firmware's reversal transient (entity briefly `unknown`). Accepted and documented; the desk still arrives and the final state is consistent.
- **Firmware-clamp residual**: a command the firmware clamps to a different position (possible only via the 500-1300 fallback window before real limits are reported, or after a limits change) can leave the setpoint visible until the next set. Deliberately not handled with timers in this change; documented as a known limitation in `setting-a-custom-height.md`. A stale-setpoint timeout is a reasonable follow-up if field reports make it a problem.
- **HA service behavior (verified in installed HA 2026.2 `number` component)**: `number.set_value` validates min/max (raises `ServiceValidationError` out of range), clamps to the native range, and does **not** swallow exceptions from `async_set_native_value` - so a failed set surfaces to the service caller (and to `hass.services.async_call(..., blocking=True)` in tests).
- **Test harness details**: the `coordinator` fixture's teardown calls `async_disconnect()` (idempotent - see `test_unload.py`); `instant_sleep` collapses all `asyncio.sleep` so the `command_writer` wake preamble + 1.0 s post-write wait add no wall-clock time; the controller emits `HEIGHT` only after the display unit is known, so tests must push a `0x0E` units packet before height packets; `wait_until` is required for state assertions because coordinator listener callbacks run as scheduled tasks.
- **README staleness (pre-existing)**: `README.md` lists 5 entities and omits the Height Setpoint number entirely (stale since the original addition). The optional one-liner fix is included in the doc task.
