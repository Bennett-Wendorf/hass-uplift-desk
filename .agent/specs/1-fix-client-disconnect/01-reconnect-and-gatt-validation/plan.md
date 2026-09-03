# Plan: 01 - Resume notifications on reconnect and validate GATT services on the operational client

**Project:** `fix-client-disconnect` (project number 1 — first numbered project in this repo; legacy unnumbered specs under `.agent/specs/manual-config-flow/` predate this convention)
**Task type:** fix | **Complexity:** complex (multi-file, async BLE lifecycle, hardware-in-the-loop verification)

## Task Description

This plan fixes two production bugs in the `uplift_desk` Home Assistant integration, both of which make the desk stop responding after a BLE connection drop, plus a small set of directly-adjacent disconnect-handling defects found in the same audit.

**Issue 1 (primary):** After the first BLE disconnect, the coordinator rebuilds a `DeskController` on a fresh client but never calls `start()` on it. `start()` is what registers GATT notifications (`client.start_notify`) and spawns the `_notification_processor` task. The rebuilt controller reports `is_connected == True` (its client is connected), so every subsequent `_get_desk_controller()` call short-circuits and returns this "dead but connected" controller. No notifications are ever received again: the height sensor freezes, and the desk is unusable until HA restart / config-entry reload. The replaced controller is also never `stop()`ped, leaking its `_notification_processor` task.

**Issue 2:** `BleakCharacteristicNotFoundError: Characteristic 0000fe61-... was not found!` raised from a *connected* client whose in-memory service set is empty/partial. Verified: `BleakGATTServiceCollection` defines no `__len__`/`__bool__`, so an empty collection is truthy and `BleakClient.services` returns it instead of raising "Service Discovery has not been performed yet". Leading root cause (strongly supported, not 100% proven): a race in bleak's shared BlueZ manager — `get_services()` waits on the `ServicesResolved` property but assembles the collection from `_service_map`, which is filled from `ObjectsAdded`/`InterfacesAdded` D-Bus signals that can lag; the result (possibly empty) is then **cached** in the manager's `_services_cache` and is only evicted on device *removal* (or manager re-init), never on disconnect. A later connect that uses `dangerous_use_bleak_cache` reuses the empty collection. The current code aggravates this by doing a double (effectively triple) connect per cycle — the validation connect/disconnect churn perturbs the shared manager state — and by never checking that the *operational* client actually contains the required characteristics.

**Adjacent defects (scope decided below):** no `disconnected_callback` (no proactive reconnect; availability goes stale), stale `BLEDevice` object reused forever, `async_disconnect()` reconnects a dropped desk just to disconnect it, an import-time `NameError` in `coordinator.py` (missing `ConfigEntry` import; type alias before class definition), dead `process_service_info()` code referencing undefined names, and a duplicated `"exceptions"` key in `strings.json` that shadows the `no_device_found` translation.

**Goals**
1. Every (re)connect — initial setup, on-demand (button press), or proactive — ends with a controller whose notifications are started and whose client is verified to contain the required GATT characteristics.
2. One BLE connection per (re)connect cycle (no validation connect/disconnect churn on the shared BlueZ manager).
3. On a connected client with missing/partial characteristics: clear the stack cache, reconnect once (bounded), then fail gracefully (`ConfigEntryNotReady` at setup / unavailable + scheduled retry at runtime).
4. Proactive, bounded, cancellable automatic reconnection when the link drops; clean unload with no reconnect churn and no leaked tasks.
5. Sensor/button `available` and state correctly reflect the disconnect → reconnect → resumed-notifications lifecycle.
6. CI-testable core logic (mocked BLE) plus a concrete on-device verification checklist with the exact log signatures that confirm the Issue 2 race.

**Non-goals**
- No changes to the pinned `uplift-ble==0.5.2` library (read-only reference at `/home/bennett/files/programming/uplift-ble/`). Upstream library improvements are listed as follow-ups.
- No re-architecture of the `DataUpdateCoordinator` usage (it is used as a push-only pub/sub hub; that pattern stays).
- No new user-facing options, no new platforms, no config-flow changes (the config flow's own validation path is left as-is).
- No attempt to fix the underlying BlueZ/bleak race upstream; we defend against it.
- No assumption that the desk's GATT table can change: characteristics are fixed by the hardware spec, so a missing characteristic on a freshly connected client is by definition an environmental/stack fault, and cache-clear + retry is the correct recovery (not device re-identification).

## Objective

Make the `uplift_desk` integration survive BLE disconnects without intervention: after any link drop, the desk automatically reconnects, GATT notifications are re-registered on the new controller, the height sensor and preset buttons correctly report availability, and a connected client whose GATT service set is empty/partial (the BlueZ manager cache race) is detected early, recovered via a bounded cache-clear + reconnect, and — if recovery fails — degrades to a clean "unavailable / retrying" state instead of a dead-but-connected controller or a `BleakCharacteristicNotFoundError` deep in a command write.

## Problem Statement

1. **Issue 1 — notifications never resumed after reconnect.**
   - `DeskController.start()` (`uplift-ble/src/uplift_ble/desk_controller.py:210-217`) is the only place `client.start_notify(output_char_uuid, ...)` and the `_notification_processor` task are created.
   - The integration only calls `start()` from `UpliftDeskBluetoothCoordinator.async_connect()` (`coordinator.py:142-143`), which is only ever called from `async_setup_entry` (`__init__.py:41`).
   - `_get_desk_controller()` (`coordinator.py:101-124`) runs on every disconnect/reconnect (gated by `if self._desk is None or not self.is_connected`, line 103). It rebuilds the controller (`coordinator.py:121`) and re-registers the HEIGHT listener (line 122) but **never calls `start()`**, so the new controller receives no notifications ever.
   - Because `is_connected` (`coordinator.py:139-140`) is derived from `client.is_connected`, the dead-but-connected controller makes all future `_get_desk_controller()` calls return the same broken controller indefinitely.
   - The outgoing controller is replaced without `stop()` (which would cancel its `_notification_processor` task — `desk_controller.py:219-230`) → a permanently-leaked, event-waiting asyncio task per dropped connection.
2. **Issue 2 — empty/partial GATT service set on a connected client.**
   - Symptom path: `command_writer` in `desk_controller.py:41-72` issues `self.client.write_gatt_char(self.input_char_uuid, packet, response=False)` (lines 62-64); `BleakClient.write_gatt_char` → `_resolve_characteristic` (`bleak/__init__.py:421-434`) → `services.get_characteristic(uuid)` returns `None` for an empty collection → `BleakCharacteristicNotFoundError`.
   - `BleakClient.services` (`bleak/__init__.py:711-724`) only raises when the backend collection is *falsy*; `BleakGATTServiceCollection` (`bleak/backends/service.py:85-122`) has no `__len__`/`__bool__`, so an **empty** collection is returned silently.
   - Manager race (bleak BlueZ backend, `backends/bluezdbus/manager.py`): `get_services()` (lines 687-780) with `use_cached=True` returns the cached collection immediately (line 712-716, debug log `"Using cached services for %s"`); otherwise it waits for `ServicesResolved` (line 855) then builds the collection from `self._service_map` (line 722) — which is populated only by `InterfacesAdded` signal handling (lines 1025-1046) and initial `GetManagedObjects` (lines 319-333). If `ServicesResolved` flips before the service `InterfacesAdded` signals are processed, an **empty** collection is built and **cached** at line 778. `_services_cache` is only popped on device interface removal (line 1075) or manager re-init (line 252) — never on disconnect.
   - Aggravation in this codebase: `_get_desk_controller()` (`coordinator.py:104-120`) does `establish_connection` (client A) → `DeskValidator(...).validate_device(...)` whose `async with` connects/disconnects → `establish_connection` again (client B) → controller on client B. Each extra connect/disconnect round perturbs the shared manager state (and, with the current "existing client factory", the validator's `async with` on an already-connected client hits `BleakError("Client is already connected")` in the BlueZ backend — see Notes — making the current path broken in more than one way). The operational client (B) is never checked for the required characteristics.
3. **Adjacent defects** (exact locations):
   - No `disconnected_callback` passed to `establish_connection` (`coordinator.py:104-109`, `115-120`) → the coordinator is never told the link dropped; nothing ever triggers a reconnect; `sensor.available` (`sensor.py:62-65`) goes stale (stuck "available" with a frozen value, or stuck "unavailable" with no recovery).
   - `self._desk_ble_device` captured once in `__init__` (`coordinator.py:98`) and reused forever → stale BlueZ D-Bus object path after device removal/re-add breaks reconnects.
   - `async_disconnect()` (`coordinator.py:145-151`) calls `_get_desk_controller()`, which **reconnects** a disconnected desk just to disconnect it again; it also sets `self._desk.client = None` (line 151) instead of dropping the controller.
   - `coordinator.py:43`: `type Uplift_Desk_DeskConfigEntry = ConfigEntry[UpliftDeskBluetoothCoordinator]` — `ConfigEntry` is **not imported** and the referenced class is defined only later (line 84) → import-time `NameError` (module cannot load as written).
   - `coordinator.py:47-74`: `process_service_info()` / `format_event_dispatcher_name()` are dead code referencing undefined names (`SensorUpdate`, `CONF_DEVICE_TYPE`, `coordinator.device_data`, `coordinator.model_info`, `coordinator.set_model_info`) — a landmine if ever called.
   - `coordinator.py:153-166`: `async_read_desk_height()` / `async_read_desk_units()` call `_get_desk_controller()` 2-3 times per invocation, and `async_read_desk_units()` mutates the private `controller._unit` (line 164).
   - `strings.json:41-50`: two `"exceptions"` keys in one JSON object — the second shadows the first, so the `no_device_found` translation key used by `__init__.py:32-36` does not exist in the effective strings.
   - `height_mm` / `keypad_display_units` are set ad hoc (`coordinator.py:155`, `:165`, `:180`) but never initialized in `__init__` → `AttributeError` on `sensor.native_value` (`sensor.py:67-70`) if read before the first successful read.

## Solution Approach

### A. One (re)connect cycle, one connection, notifications always started (fixes Issue 1 + Issue 2)

Replace `_get_desk_controller()` / `async_connect()` with a single (re)connect routine — behaviorally:

```
async _establish_and_start(self) -> DeskController   # raises on final failure
    self._intentional_disconnect = False
    for attempt in 1..2:                             # bounded: one cache-clear retry
        device = await self._resolve_ble_device()    # fresh from HA bluetooth registry,
                                                     # fallback to last known device
        client = await establish_connection(
            BleakClientWithServiceCache, device, name,
            disconnected_callback=self._on_ble_disconnected,   # sync callable
            max_attempts=3)
        desk_config = self._validate_client_services(client)  # see B
        if desk_config is None:
            _LOGGER.warning("incomplete GATT services (attempt %d/2); clearing cache", attempt)
            await self._best_effort(self.clear_cache_and_discard(client))   # see C
            if attempt == 1:
                continue
            raise UpliftDeskServicesError(...)       # new, small, internal exception
        await self._stop_current_controller()        # see D — BEFORE replacement
        controller = DiscoveredDesk(address=..., name=..., desk_config=desk_config) \
                         .create_controller(client)  # library dataclass + factory
        controller.on(DeskEventType.HEIGHT, self._async_height_notify_callback)
        await controller.start()                     # EXACTLY ONCE, on the fresh controller
        self._desk = controller
        await self._refresh_state()                  # units + height, best-effort (see E)
        return controller
```

Rules that make this safe:

- **`start()` contract (Issue 1 core):** `start()` is invoked exactly once, immediately after construction + listener registration, on a *freshly created* controller, inside the (re)connect cycle. It is never called from `async_connect()` alone, and never called twice on the same controller instance. Rationale: the library's idempotency guard (`_notify_started`, `desk_controller.py:212-216`) only covers `start_notify`; each `start()` call unconditionally spawns a new `_notification_processor` task (`desk_controller.py:217`), so double-starting a controller leaks a second processor. Since we always build a new controller per connection, one `start()` per controller is both sufficient and safe. If `start()` raises (e.g. the Issue 2 race surfacing at `start_notify`), the whole cycle is treated as failed: the client is discarded (best-effort disconnect), and — because validation already passed — we do **not** loop again; the failure propagates to the caller (setup → `ConfigEntryNotReady`; runtime → reconnect loop retries the whole cycle later).
- **Validation before use (Issue 2 core):** the operational client's `client.services` is verified to contain the required characteristics *before* a controller is built on it. This converts the original failure mode (a `BleakCharacteristicNotFoundError` deep inside a command write, after the controller is "up") into an early, detected, recoverable condition at connect time.
- **Single connection per cycle:** `DeskValidator` (and the `_generate_existing_client_factory` hack at `coordinator.py:76-82`) are **removed from the coordinator path**. The validator's `async with factory(...)` contract is fundamentally incompatible with an already-connected client (`BleakClient.__aenter__` → `connect()` → BlueZ backend raises `BleakError("Client is already connected")`, `backends/bluezdbus/client.py:123-124`), and re-connecting just to validate is exactly the churn that feeds the manager race. Validation becomes a small local helper over the *connected* client (below). The config flow keeps using `DeskValidator` as today (its own connect/disconnect is acceptable there — it runs once, pre-setup).
- **Old controller teardown before replacement (Issue 1 leak):** `_stop_current_controller()` (see D) runs before `self._desk` is swapped, so the outgoing controller's processor task is cancelled and its notifications stopped (when the client is still alive).

### B. Service validation on the connected client (Issue 2 — exact verification)

New coordinator helper, behaviorally:

1. `services = client.services` — if this raises `BleakError` ("Service Discovery has not been performed yet"), treat as *invalid* (goes to the cache-clear path).
2. Iterate `services` (services only, via `for service in services:`); find services whose `uuid` is a key of `uplift_ble.desk_configs.DESK_CONFIGS_BY_SERVICE` (public import, 4 known variants). **Debug-log every service UUID found and the service count** (this log is a verification signature; an empty/partial set will show 0 or wrong services).
3. If no matching desk service is present → *invalid*, warn with the found service list. (This is an environmental surprise — the device passed GATT validation during config entry; a real hardware table would have its variant's service.)
4. For the matched `DeskConfig`, require **all three** characteristics present in `client.services`, looked up via `client.services.get_characteristic(uuid)` using the config's already-normalized full 128-bit UUID strings:
   - `desk_config.input_char_uuid` (e.g. `0000fe61-0000-1000-8000-00805f9b34fb` for the 0xFE60 variant),
   - `desk_config.output_char_uuid` (e.g. `0000fe62-...`),
   - `desk_config.name_char_uuid` (e.g. `0000fe63-...`).
   - **Never hardcode UUIDs** — derive them from the resolved `DeskConfig` so all 4 variants work. Warn listing exactly which UUIDs are missing.
5. Return the resolved `DeskConfig` on success, `None` on failure.

**Retry bound:** exactly **one** recovery attempt per (re)connect cycle — i.e. at most 2 `establish_connection` calls per cycle, with a cache clear (below) in between. If the second attempt also fails validation, the cycle fails.

**Failure behavior:**
- During `async_setup_entry`: the cycle's exception is converted to `ConfigEntryNotReady` (HA retries setup with its own backoff; the entry shows "retrying", not a permanent failure).
- At runtime (proactive reconnect loop / on-demand): log the error, leave `self._desk = None` (sensor unavailable), and let the reconnect loop (C) retry the *whole* cycle later — each later cycle gets its own one-shot cache-clear retry. No unbounded tight retrying: the loop uses backoff (5s → 10s → 20s → 30s cap, reset on success).

**Why this is the right recovery (and not "device changed"):** the desk's GATT table is fixed by the hardware spec. A freshly connected client missing characteristics is, by definition, a stack/manager fault (the cached-empty-collection race or a stale path) — so clearing the cache and reconnecting once is the correct, bounded response.

### C. Cache clearing, device re-resolution, and proactive reconnection

- **Cache clear:** use the **module-level** `bleak_retry_connector.clear_cache(address)` (Linux; pops the BlueZ manager's `_services_cache` entry for the device path and sends `RemoveDevice` to force a fresh discovery). **Do not** use `BleakClientWithServiceCache.clear_cache()` — in current bleak it is a no-op that only logs a warning (`bleak_retry_connector/__init__.py:198-203`). Best-effort: swallow/log failures (e.g. non-Linux, device already gone). After a cache clear, wait ~1s before the retry so BlueZ/HA scanner can re-advertise, and re-resolve the `BLEDevice` (below) because `RemoveDevice` invalidates the old D-Bus path.
- **Device re-resolution (stale `BLEDevice` fix):** before each `establish_connection`, call `async_ble_device_from_address(hass, address)` (HA bluetooth registry — fresh object with current D-Bus path) and fall back to the last known device if `None` (log which was used). This replaces the forever-reused `self._desk_ble_device` (`coordinator.py:98`). (Note: `establish_connection`'s `ble_device_callback` parameter is accepted but unused in bleak_retry_connector 4.6.0, so the coordinator must re-resolve explicitly.)
- **Proactive reconnection (no `disconnected_callback` fix):** pass `disconnected_callback=self._on_ble_disconnected` to `establish_connection`. The callback is synchronous (invoked by the backend's disconnect watcher); it must only do sync-safe work: ignore if `self._intentional_disconnect` is set or if the callback's client is not the current one, then schedule `self._async_handle_unexpected_disconnect()` on the HA loop. That handler: warns "desk connection lost; will attempt to reconnect", runs `_stop_current_controller()`, and starts a tracked `_reconnect_task` running the backoff loop described in B (each iteration calls the full (re)connect cycle). Guards: only one reconnect task at a time (skip if one is already running), task is cancelled and awaited in `async_disconnect()`/unload, loop exits on intentional disconnect or entry unload.
- **`async_disconnect()` rewrite (reconnect-to-disconnect fix):** no `_get_desk_controller()` call. Behaviorally: set `_intentional_disconnect = True`; cancel the pending reconnect task (suppress `CancelledError`); run `_stop_current_controller()`; if the current client is still connected, `await client.disconnect()` (best-effort); set `self._desk = None`. Unload therefore never opens a new connection and never raises `BleakError("Not connected")`.

### D. Controller teardown (old controller `stop()` before replacement)

`_stop_current_controller()`, behaviorally:

1. `old = self._desk; self._desk = None` (clear the reference first so a mid-teardown failure can't leave a half-replaced state).
2. If `old is None`: return.
3. `try: await old.stop()` — this cancels the old `_notification_processor` task first (`desk_controller.py:221-226`) and then calls `client.stop_notify` (only if `_notify_started`). **Catch `BleakError` (and log at debug)** because `stop_notify` raises `BleakError("Not connected")` when the client already dropped (`backends/bluezdbus/client.py:998-999`); the processor task is already cancelled by that point, so swallowing the `stop_notify` error is correct.
4. If `old.client is not None and old.client.is_connected`: `await old.client.disconnect()` (best-effort, same catch) so the BLE link is released before the new `establish_connection` (BlueZ allows one connection per device; the new client must not piggyback on the old client's watcher state).

### E. State and entity behavior across the reconnect lifecycle

- **Coordinator state:** initialize `self.height_mm = None` and `self.keypad_display_units = None` in `__init__` (kills the `AttributeError` path in `sensor.native_value`). After a successful (re)connect, `_refresh_state()` re-runs the units + height reads (same calls `async_setup_entry` makes today) so the sensor has a fresh value without waiting for the desk to move; these are best-effort (warn, don't fail the connect). While disconnected, the last known value is retained but hidden by `available = False`.
- **`is_connected`** (`coordinator.py:139-140`) stays as the single source of truth (controller's client connected). With proactive reconnection it now flips False on drop (immediately — bleak backend updates `is_connected` when the watcher fires) and back to True only after the full (re)connect cycle (including `start()`) succeeds, because `_desk` is nulled during the disconnect handling and only re-set after a successful cycle.
- **Sensor** (`sensor.py`): `available` = `coordinator.is_connected` (unchanged). `native_value` returns `coordinator.height_mm` (now always an attribute; `None` → HA shows "unknown"). The existing `_handle_coordinator_update` keeps working because `_async_height_notify_callback` (`coordinator.py:179-182`) still calls `async_set_updated_data`.
- **Buttons** (`button.py`): add `available` = `coordinator.is_connected` (currently buttons always show available). Pressing a button while disconnected still works as an on-demand reconnect (the (re)connect cycle is invoked from the same entry points as today) — keep that, log at info.
- **`async_read_desk_height` / `async_read_desk_units`** (`coordinator.py:153-166`): fetch the controller **once** per call (not 2-3 times). The private `controller._unit` mutation (line 164) is kept as-is for now (works; upstream follow-up noted).

### F. Setup/unload wiring (`__init__.py`)

- `async_setup_entry`: keep `async_ble_device_from_address` → `ConfigEntryNotReady("no_device_found")` when absent. Wrap `coordinator.async_connect()` in `try/except` for `BleakError` (covers all `establish_connection` failure types: `BleakNotFoundError`, `BleakConnectionError`, `BleakAbortedError`, `BleakOutOfConnectionSlotsError`), the new `UpliftDeskServicesError`, and `asyncio.TimeoutError` → raise `ConfigEntryNotReady` with a human-readable message (no new translation key required; the entry will show "retrying"). Wrap the units/height reads the same way (a desk that connects but never answers queries is not ready).
- `async_unload_entry`: call the rewritten `coordinator.async_disconnect()`, then unload platforms (unchanged shape).
- **`strings.json`:** merge the two duplicated `"exceptions"` objects into one (keep both `no_device_found` and `device_not_found_error`) so the existing `no_device_found` key actually resolves.

### G. Coordinator hygiene (must-ship-with-this-fix, same file)

- Add `from homeassistant.config_entries import ConfigEntry` and move the `type Uplift_Desk_DeskConfigEntry = ...` alias **below** the class definition (or keep it above with a string form) so `coordinator.py` is importable (fixes the `NameError` at line 43).
- Delete dead `process_service_info()` + `format_event_dispatcher_name()` (`coordinator.py:47-74`) and their now-unused imports (`SensorUpdate` usage, `CONF_DEVICE_TYPE`, `CoreState`, `async_dispatcher_send`, `CoordinatorEntity`, `UpdateFailed`, `BluetoothScanningMode`, duplicate `BluetoothServiceInfoBleak` import, `_generate_existing_client_factory` and its `BleakClient`/`BLEDeviceProtocol` imports as they become unused).
- Keep `DeskValidator` import only where still used (config flow); remove from coordinator.

### Related items — scope decisions

| # | Item | Decision | Where |
|---|------|----------|-------|
| 1 | No `disconnected_callback` → no proactive reconnect, stale `available` | **IN SCOPE** — central to "desk stops responding" UX | C (proactive reconnection) |
| 2 | Stale `self._desk_ble_device` reused forever | **IN SCOPE** — directly breaks reconnects after path change; small change | C (device re-resolution) |
| 3 | `async_disconnect()` reconnects just to disconnect | **IN SCOPE** — same code path, trivial once C exists | C/D (`async_disconnect` rewrite) |
| 4 | Import-time `NameError` + dead `process_service_info` | **IN SCOPE** (as hygiene) — module must import for anything to work/test | G |
| 5 | `strings.json` duplicated `"exceptions"` key | **IN SCOPE** (one-line merge) | F |
| 6 | Library: `DeskController.start()` not fully idempotent (processor task spawned per call) | **FOLLOW-UP** — upstream `uplift-ble` PR; integration enforces "one `start()` per controller" meanwhile | Notes |
| 7 | Library: `stop_notify` raises on disconnected client | **FOLLOW-UP** — upstream could no-op; integration catches meanwhile | Notes |
| 8 | `async_read_desk_units` mutating `controller._unit` | **FOLLOW-UP** — ask upstream for a supported setter | Notes |
| 9 | `BleakClientWithServiceCache.clear_cache()` no-op in current bleak | **NOT A BUG TO FIX HERE** — documented; we use module-level `clear_cache` | Notes |
| 10 | `config_flow` validation churn | **FOLLOW-UP** — runs once pre-setup; acceptable; revisit if the race proves to originate there | Notes |

## Relevant Files

- `custom_components/uplift_desk/coordinator.py` — **primary file; both bugs live here.**
  - `:43` broken module-level type alias (missing `ConfigEntry` import; forward reference)
  - `:47-74` dead `process_service_info` / `format_event_dispatcher_name`
  - `:76-82` `_generate_existing_client_factory` (removed)
  - `:87-99` `__init__` — add state init (`height_mm`, `keypad_display_units`, reconnect bookkeeping)
  - `:101-124` `_get_desk_controller` — **replaced** by the single-connection (re)connect cycle
  - `:139-140` `is_connected` — kept as source of truth
  - `:142-143` `async_connect` — becomes a thin wrapper over the (re)connect cycle
  - `:145-151` `async_disconnect` — **rewritten** (no reconnect-to-disconnect)
  - `:153-166` `async_read_desk_height` / `async_read_desk_units` — fetch controller once
  - `:179-182` `_async_height_notify_callback` — kept
- `custom_components/uplift_desk/__init__.py` — `async_setup_entry` (`:25-51`): wrap connect/reads in `ConfigEntryNotReady`; `async_unload_entry` (`:53-59`) uses rewritten `async_disconnect`.
- `custom_components/uplift_desk/sensor.py` — `:62-65` `available` (kept), `:67-70` `native_value` (safe with initialized state).
- `custom_components/uplift_desk/button.py` — add `available` property to both preset buttons.
- `custom_components/uplift_desk/strings.json` — merge duplicated `"exceptions"` keys.
- `custom_components/uplift_desk/manifest.json` — no change expected (`uplift-ble==0.5.2` stays pinned).

### Library references (READ-ONLY — do not modify)

- `uplift-ble/src/uplift_ble/desk_controller.py`
  - `:41-72` `command_writer` — the original `BleakCharacteristicNotFoundError` site (`write_gatt_char` at `:62-64`)
  - `:210-217` `start()` — guard covers only `start_notify`; processor task spawned on every call
  - `:219-230` `stop()` — cancels processor first, then `stop_notify` (raises `BleakError("Not connected")` if client down)
- `uplift-ble/src/uplift_ble/desk_validator.py` — `:74-133` `validate_device` (connect/disconnect via `async with`; swallows all exceptions → `None`); `:142-168` `_service_has_required_characteristics` (reference for the local validation logic)
- `uplift-ble/src/uplift_ble/desk_configs.py` — `:38-67` `DESK_CONFIGS_BY_SERVICE` (public; source of the UUIDs to verify, per variant)
- `uplift-ble/src/uplift_ble/models.py` — `:9-24` `DiscoveredDesk(address, name, desk_config)` + `create_controller(client)`
- `uplift-ble/src/uplift_ble/ble_protos.py` — protocol definitions (context only)

### Stack references (READ-ONLY — version notes in Notes)

- `bleak/__init__.py:711-724` — `BleakClient.services` (empty collection is truthy → returned, not raised)
- `bleak/backends/service.py:85-122` — `BleakGATTServiceCollection` (no `__len__`/`__bool__`)
- `bleak/__init__.py:421-434` — `_resolve_characteristic` → `BleakCharacteristicNotFoundError`
- `bleak/backends/bluezdbus/manager.py:687-780` — `get_services` (cache read at `:712-716` with `"Using cached services for %s"`; build from `_service_map` at `:722`; cache write at `:778`); `:1025-1046` signal-populated maps; `:1075` cache evicted only on device removal
- `bleak/backends/bluezdbus/client.py:123-124` — `BleakError("Client is already connected")`; `:998-999` — `stop_notify` `BleakError("Not connected")`
- `bleak_retry_connector/__init__.py:413-424` — `establish_connection(client_class, device, name, disconnected_callback=None, max_attempts=..., cached_services=None, ble_device_callback=None, use_services_cache=True, ...)`
- `bleak_retry_connector/__init__.py:253-336` — `_has_valid_services_in_cache` (empty/stale cache → fresh discovery)
- `bleak_retry_connector/bluez.py:272-305` — module-level `clear_cache(address)` (pop manager cache + `RemoveDevice`) — **the** cache-clear to use
- `bleak_retry_connector/__init__.py:187-203` — `BleakClientWithServiceCache.clear_cache()` (no-op in current bleak; do not rely on it)

### New Files (if needed)

- `tests/conftest.py`, `tests/test_coordinator_reconnect.py` (and sibling test modules) — mocked-BLE unit tests (see Task 6)
- `requirements_test.txt` — `pytest-homeassistant-custom-component` (+ transitive pytest stack)
- `.github/workflows/test.yml` — CI job running the test suite (repo already has `hassfest.yml` / `hacsaction.yml`)

## Team Orchestration

> **Worktree Isolation**: The team-lead creates an isolated git worktree for this spec using `~/.config/opencode/scripts/worktree-create.sh`. All builders work inside this worktree. After final validation, changes are merged back via `~/.config/opencode/scripts/worktree-merge.sh`.

The team-lead agent will orchestrate execution using these team members:

### Team Members

- **Builder**
  - Name: `reconnect-builder`
  - Role: Implement the coordinator (re)connect cycle, validation, proactive reconnection, setup/unload wiring, entity availability, and the test suite per the step-by-step tasks.
  - Agent: builder

- **Validator**
  - Name: `reconnect-validator`
  - Role: Verify implementation meets criteria — run the test suite and static checks, review the diff against the acceptance criteria, and produce the on-device verification checklist results summary.
  - Agent: validator

- **Documenter**
  - Name: `reconnect-documenter`
  - Role: Generate documentation for completed work (troubleshooting guide for the disconnect/reconnect behavior and the Issue 2 log signatures) in `app_docs/`.
  - Agent: documenter

## Step by Step Tasks

### 1. Coordinator hygiene: make the module importable and remove dead code
- **Task ID**: coordinator-hygiene
- **Depends On**: none
- **Assigned To**: reconnect-builder
- **Agent**: builder
- **Actions**:
  - In `coordinator.py`: add `from homeassistant.config_entries import ConfigEntry`; move the `type Uplift_Desk_DeskConfigEntry = ConfigEntry[UpliftDeskBluetoothCoordinator]` alias to after the class definition (or otherwise make it evaluate safely).
  - Delete `process_service_info()` and `format_event_dispatcher_name()` (`coordinator.py:47-74`) and all imports that exist only for them (`CoreState`, `async_dispatcher_send`, `CoordinatorEntity`, `UpdateFailed`, `BluetoothScanningMode`, the duplicate `BluetoothServiceInfoBleak` import, and any newly-unused names).
  - Initialize state in `__init__`: `self.height_mm = None`, `self.keypad_display_units = None`, `self._reconnect_task = None`, `self._intentional_disconnect = False`.
  - Collapse repeated `_get_desk_controller()` calls in `async_read_desk_height()` / `async_read_desk_units()` to a single fetch per method (keep the `_unit` fallback logic, including the `controller._unit` mutation, unchanged).
  - Do **not** yet change the (re)connect flow itself — that is Task 2.
- **Acceptance Criteria**:
  - `python -m compileall custom_components/uplift_desk -q` passes and importing the coordinator module in the HA Python environment succeeds (no `NameError`).
  - No references to `process_service_info`, `format_event_dispatcher_name`, or `CONF_DEVICE_TYPE` remain in `coordinator.py`.
  - `sensor.native_value` cannot raise `AttributeError` before the first successful height read (attribute initialized).

### 2. Rewrite the (re)connect cycle: one connection, validate, `start()` (Issues 1 & 2)
- **Task ID**: reconnect-cycle
- **Depends On**: coordinator-hygiene
- **Assigned To**: reconnect-builder
- **Agent**: builder
- **Actions**:
  - Remove `_generate_existing_client_factory` and the `DeskValidator` usage from `coordinator.py`.
  - Implement the (re)connect cycle exactly as specified in Solution Approach A: fresh device resolution → `establish_connection(BleakClientWithServiceCache, device, name, disconnected_callback=..., max_attempts=3)` → `_validate_client_services(client)` → (on failure: module-level `clear_cache(address)`, best-effort client disconnect, ~1s grace, one retry) → `_stop_current_controller()` → build controller via the library `DiscoveredDesk(...).create_controller(client)` → register `DeskEventType.HEIGHT` listener → `await controller.start()` (exactly once) → `self._desk = controller` → best-effort `_refresh_state()`.
  - Implement `_validate_client_services(client)` per Solution Approach B (iterate services; match against `DESK_CONFIGS_BY_SERVICE`; require `input_char_uuid`, `output_char_uuid`, `name_char_uuid` via `client.services.get_characteristic(uuid)`; debug-log service count + UUIDs; warn with specifics on failure; return `DeskConfig | None`).
  - Implement `_stop_current_controller()` per Solution Approach D (null the ref first; `try/except BleakError` around `stop()`; best-effort `client.disconnect()`).
  - Keep `async_connect()` as the setup-time entry point that runs the cycle and raises on failure; make all other consumer paths (preset reads) route through the same cycle when `_desk` is missing.
  - Define `UpliftDeskServicesError(BleakError)` (or similar small internal exception) for the "still invalid after retry" case.
  - Add clear log lines (debug/warning) for: cycle start, services found (count + UUIDs), cache clear, retry, controller started, cycle failure. These log lines are part of the verification contract (see Verification Plan).
- **Acceptance Criteria**:
  - A successful (re)connect cycle performs exactly **one** `establish_connection` call.
  - `controller.start()` is awaited exactly once, on the freshly built controller, before `self._desk` is assigned.
  - The previous controller (if any) has `stop()` awaited (with `BleakError` tolerated) and its client disconnected (best-effort) **before** replacement.
  - On a client whose `services` lack any required characteristic: `clear_cache(address)` is awaited once, the client is discarded, and the cycle retries once; if the second client is also invalid, the cycle raises (no controller is installed, `self._desk` stays `None`).
  - No code path calls `start()` on a controller that was already started.
  - `is_connected` is `False` from the moment the old controller is torn down until the new cycle completes.

### 3. Proactive reconnection and clean disconnect (related items 1, 2, 3)
- **Task ID**: proactive-reconnect
- **Depends On**: reconnect-cycle
- **Assigned To**: reconnect-builder
- **Agent**: builder
- **Actions**:
  - Implement `_on_ble_disconnected(client)` (sync) + `_async_handle_unexpected_disconnect()` + `_async_reconnect_loop()` per Solution Approach C: warn on unexpected drop; tear down the old controller; run a tracked, cancellable reconnect task with backoff 5s → 10s → 20s → 30s cap (reset on success); skip if an intentional disconnect is in progress or a reconnect task is already running; exit on success/unload.
  - Rewrite `async_disconnect()` per Solution Approach C/D: set the intentional flag, cancel + await the reconnect task (suppress `CancelledError`), tear down the controller, best-effort `client.disconnect()`, `self._desk = None`. **No** `_get_desk_controller()`/cycle invocation.
  - Wire `_intentional_disconnect = False` at the start of each (re)connect cycle so subsequent unexpected drops are handled again.
  - Ensure `hass.async_create_task` (not raw `asyncio.create_task`) is used for HA-loop tasks, and the reconnect task is stored for cancellation.
- **Acceptance Criteria**:
  - Simulating a link drop (invoking the client's disconnected callback) results in: a warning log, controller teardown, and a scheduled reconnect that runs the full cycle; on success the sensor becomes available again with no user action.
  - `async_unload_entry` never calls `establish_connection` and never raises `BleakError("Not connected")`, even when the desk is already dropped.
  - At most one reconnect task exists at any time; it is cancelled on unload; backoff resets after a successful connect.
  - A drop that occurs *during* an intentional disconnect (unload) does not schedule a reconnect.

### 4. Setup/unload wiring and strings fix
- **Task ID**: setup-unload-wiring
- **Depends On**: proactive-reconnect
- **Assigned To**: reconnect-builder
- **Agent**: builder
- **Actions**:
  - In `__init__.py`: wrap `coordinator.async_connect()` and the units/height reads in `try/except (BleakError, asyncio.TimeoutError)` (including the new services error) → `raise ConfigEntryNotReady(...)` with a clear message.
  - Keep the existing `no_device_found` `ConfigEntryNotReady` path.
  - Fix `strings.json`: merge the two `"exceptions"` objects into one, preserving both `no_device_found` and `device_not_found_error`.
- **Acceptance Criteria**:
  - With the desk unreachable at setup time, the config entry ends in the "retrying" state (`ConfigEntryNotReady`), not a hard setup failure; raw `BleakError`/`TimeoutError` do not escape `async_setup_entry`.
  - `strings.json` parses as valid JSON with a single `"exceptions"` key containing both entries.
  - Unload still unloads platforms after `async_disconnect()`.

### 5. Entity availability (sensor + buttons)
- **Task ID**: entity-availability
- **Depends On**: reconnect-cycle
- **Assigned To**: reconnect-builder
- **Agent**: builder
- **Actions**:
  - `sensor.py`: keep `available` = `coordinator.is_connected`; ensure `native_value` returns `coordinator.height_mm` (now always an attribute; `None` allowed).
  - `button.py`: add an `available` property returning `coordinator.is_connected` to both preset buttons.
  - No changes to `device_info` / unique IDs.
- **Acceptance Criteria**:
  - During the disconnect → reconnect window, the sensor and both buttons report `available == False`; after the cycle completes they report `True`.
  - After a reconnect, the sensor's value is refreshed by `_refresh_state()` (units + height reads) and then by live notifications.

### 6. Test suite + CI (mocked BLE)
- **Task ID**: tests-ci
- **Depends On**: setup-unload-wiring, entity-availability
- **Assigned To**: reconnect-builder
- **Agent**: builder
- **Actions**:
  - Add `requirements_test.txt` with `pytest-homeassistant-custom-component` (pulls the pinned HA core test harness) and `pytest`.
  - Add `tests/conftest.py` enabling custom integrations (`pytest_plugins = "pytest_homeassistant_custom_component"`; `enable_custom_integrations` fixture usage per the plugin's docs).
  - Build a fake BLE client (plain class, not `BleakClient`): `is_connected` flag, `services` object exposing `__iter__` + `get_characteristic(uuid)` (populate from a real `BleakGATTServiceCollection` or a minimal stand-in), async `connect`/`disconnect`/`start_notify`/`stop_notify`/`write_gatt_char` that record calls, and a hook to simulate a drop (flip `is_connected`, invoke the registered `disconnected_callback`).
  - Use the **real** `DeskController` (from `uplift-ble`) against the fake client so `start()`/`stop()`/processor-task behavior under test is the production code.
  - Implement tests (at minimum):
    1. Reconnect after simulated drop creates a new controller and awaits `start_notify` on the **new** client's output UUID exactly once; the old client receives `stop_notify`/disconnect.
    2. One successful cycle → `establish_connection` (mocked) called exactly once.
    3. First client with empty/partial services → `clear_cache(address)` called once, one retry, controller started on the second (valid) client.
    4. Both clients invalid → cycle raises the services error; `establish_connection` called exactly twice; no controller installed.
    5. `async_disconnect()` with a dropped desk → `establish_connection` not called; no `BleakError` propagates.
    6. Disconnected callback → reconnect task scheduled; after (mocked) backoff the cycle runs; sensor `available` flips False → True.
    7. Setup with unreachable desk → `ConfigEntryNotReady` raised from `async_setup_entry`.
    8. Height notification pushed through the fake client's notify handler updates `coordinator.height_mm` and the sensor state.
  - Add `.github/workflows/test.yml`: `ubuntu-latest`, Python 3.13, `pip install -r requirements_test.txt`, `python -m pytest tests/ -v`.
- **Acceptance Criteria**:
  - `python -m pytest tests/ -v` passes locally and in CI.
  - Tests 1-4 fail if `start()` is removed from the (re)connect cycle (i.e., they genuinely cover Issue 1) and if validation is removed (cover Issue 2).
  - Existing hassfest validation is unaffected (workflow untouched).

### 7. Final Validation
- **Task ID**: validate-all
- **Depends On**: coordinator-hygiene, reconnect-cycle, proactive-reconnect, setup-unload-wiring, entity-availability, tests-ci
- **Assigned To**: reconnect-validator
- **Agent**: validator
- **Checks**:
  - Run all validation commands (pytest, compileall, hassfest via workflow or local `hassfest` if available).
  - Review the full diff against every Acceptance Criterion in Tasks 1-6; verify no changes leaked into `uplift-ble` (read-only) or `config_flow.py` (out of scope).
  - Verify the "one `start()` per controller" invariant by inspection (no second `start()` call site; no `start()` on a pre-existing controller).
  - Verify log lines required by the Verification Plan exist with the expected levels.
  - Produce the on-device verification checklist (below) filled in for anything checkable from CI artifacts, and hand the remaining steps to the human for the physical desk.
- **Acceptance Criteria**:
  - All validation commands pass; all acceptance criteria from Tasks 1-6 met; issues filed as blocking or documented non-blocking.

### 8. Documentation
- **Task ID**: generate-docs
- **Depends On**: validate-all
- **Assigned To**: reconnect-documenter
- **Agent**: documenter
- **Actions**:
  - Read the plan file and implementation files.
  - Generate documentation in `app_docs/`: (a) "Disconnect & Reconnect Behavior" — what happens on a BLE drop (timeline: drop → unavailable → backoff reconnects → available again), (b) "Troubleshooting" — the Issue 2 race, the exact log signatures to look for (see Verification Plan), how to read the new coordinator log lines, and the recovery the integration performs automatically (cache clear + retry), (c) a short "What changed" note for users upgrading.
- **Acceptance Criteria**:
  - `app_docs/` contains the troubleshooting guide with the concrete log signatures and the reconnect timeline; no implementation code is included.

## Acceptance Criteria

1. **Issue 1 fixed:** after any BLE drop, the next (re)connect cycle calls `start()` on the new controller (verified by unit test 1/2); the height sensor resumes updating without HA restart (on-device checklist).
2. **Issue 2 defended:** a connected client missing required characteristics triggers exactly one `clear_cache(address)` + reconnect retry per cycle, then explicit failure (unit tests 3/4); the original `BleakCharacteristicNotFoundError` from `command_writer` no longer occurs in normal operation (on-device soak).
3. **One connection per cycle:** `establish_connection` is called exactly once per successful (re)connect cycle (unit test 2); the `DeskValidator`/existing-client-factory double-connection is gone from the coordinator.
4. **Proactive recovery:** an unexpected drop schedules automatic reconnection with bounded backoff; unload never reconnects and never raises `BleakError("Not connected")` (unit tests 5/6).
5. **Graceful setup failure:** unreachable desk at setup → `ConfigEntryNotReady` (retrying), never a raw exception (unit test 7).
6. **Entities:** sensor + both buttons report `available` in lockstep with `is_connected`; sensor value refreshed after each reconnect (unit test 8 + inspection).
7. **Hygiene:** `coordinator.py` imports cleanly; dead `process_service_info` removed; `strings.json` has a single merged `"exceptions"` key.
8. **No regressions:** hassfest + HACS validation still pass; `uplift-ble` untouched.

## Validation Commands

- `python -m pytest tests/ -v` — run the mocked-BLE test suite (CI: `.github/workflows/test.yml`).
- `python -m compileall custom_components/uplift_desk -q` — byte-compile check (fast import-time regression guard).
- `python -c "import custom_components.uplift_desk.coordinator"` (in the HA Python env) — module import smoke test.
- `hassfest --action validate` (or the existing `Validate with hassfest` GitHub workflow) — manifest/strings validation.
- On-device (manual, requires physical desk + Linux/BlueZ host) — see Verification Plan checklist below.

## Verification Plan

This is a BLE hardware integration: the (re)connect *logic* is fully unit-testable with mocks (Task 6, CI), but the *stack race* (Issue 2) and real drop/reconnect behavior require on-device verification.

### CI (automated, no hardware)

- All Task 6 tests: reconnect calls `start()` once per cycle; single `establish_connection` per cycle; cache-clear + one retry on empty/partial services; no reconnect on unload; `ConfigEntryNotReady` at setup; availability + state updates.
- Static: compileall + import smoke + hassfest.

### On-device / manual (Linux + BlueZ, physical desk)

Enable debug logging for `uplift_desk`, `bleak.backends.bluezdbus.manager`, `bleak.backends.bluezdbus.client`, and `bleak_retry_connector` (HA: Settings → Logs → Loggers, or `logbook` config).

1. **Issue 1 — resume after drop.**
   - Drop the link: `sudo hciconfig hci0 down && sleep 20 && sudo hciconfig hci0 up` (or toggle the adapter in `bluetoothctl`).
   - Expected (fixed): `Desk connection lost; will attempt to reconnect` (warning) → `Connected after N attempts` → `Started notifications for ...` (debug) → `Height notify callback received height: ...` (debug) and the sensor value tracks the desk again. Sensor shows *unavailable* during the window, *available* after.
   - Regression signature (pre-fix, for comparison): `Getting desk controller ...` followed by silence — no `start_notify`, no height callbacks, sensor frozen while still "available".
2. **Issue 2 — empty/partial service set race.**
   - Reproduce by stressing connect/disconnect churn (e.g., repeated entry reloads, or `down/up` cycles in quick succession) with the manager logger at DEBUG.
   - Signatures that confirm the race: `Using cached services for /org/bluez/hciX/dev_...` (manager) appearing on a connect whose service set is then empty/partial; the new coordinator warning `Incomplete GATT services ... (N service(s)) ... clearing cache and reconnecting`; and (pre-fix only) `BleakCharacteristicNotFoundError: Characteristic 0000fe61-0000-1000-8000-00805f9b34fb was not found!` from `uplift_ble.desk_controller`.
   - Expected (fixed): at most one `clear_cache` per failed cycle, a successful second connect, and **no** `BleakCharacteristicNotFoundError` in normal operation.
   - Secondary signature to watch: `BleakError: Service Discovery has not been performed yet` (the sibling failure where the backend collection is `None`, e.g. after `_cleanup_all`) — must also be handled by the validation path (treated as invalid → cache-clear path).
3. **Clean unload/reload.**
   - Reload the config entry (Settings → Devices & Services → Uplift Desk → Reload).
   - Expected: no `Getting desk controller`/connect activity between unload and the next setup; no `BleakError: Not connected` tracebacks; no "reconnect to disconnect" connect.
4. **Soak (24 h).**
   - Let HA run with periodic drops (or overnight, letting the desk/adapter sleep-wake).
   - Expected: every drop self-recovers within a few backoff intervals; no `Task was destroyed but it is pending` / `Task exception was never retrieved` warnings (processor-task leak check); memory stable.
5. **Buttons while disconnected.**
   - Press a preset button in the UI while the desk is dropped → on-demand reconnect occurs (info log) and the preset is applied once connected.

## Notes

- **Version caveats:** the local reference stack is bleak 3.0.2 + bleak_retry_connector 4.6.0; HA core pins its own (older) versions. The behaviors this plan relies on — `establish_connection(..., disconnected_callback=..., max_attempts=...)`, module-level `clear_cache(address)`, `BleakClient.services` truthiness, the BlueZ manager `_services_cache` lifecycle, `stop_notify` raising when disconnected, `connect()` raising when already connected — are stable across bleak 0.22.x–3.x and bleak_retry_connector 3.x–4.x. The builder should confirm the exact signatures against the HA version in use before finalizing mocks.
- **Do not modify** `/home/bennett/files/programming/uplift-ble/` (pinned `uplift-ble==0.5.2`, read-only reference).
- **`BleakGATTServiceCollection` has no `__len__`/`__bool__`** — never write `if client.services:`; use `len(client.services.services)` or the validation helper when counting services.
- **`BleakClientWithServiceCache.clear_cache()` is a no-op** in current bleak (warns and returns False) — always use `bleak_retry_connector.clear_cache(address)`.
- **`establish_connection`'s `ble_device_callback` parameter is unused** in bleak_retry_connector 4.6.0 — re-resolve the `BLEDevice` explicitly before each attempt.
- **`DeskController.start()` contract:** one call per controller instance (library spawns a processor task per call; only `start_notify` is guarded). Enforced by construction here; upstream idempotency is a follow-up.
- **`stop_notify` raises `BleakError("Not connected")`** when the client already dropped — all teardown paths must tolerate this (processor task is cancelled before the raising call, inside `stop()`).
- **Never `async with` an already-connected `BleakClient`** — `connect()` raises `BleakError("Client is already connected")` in the BlueZ backend; this is why the old `_generate_existing_client_factory` pattern (feeding a connected client to `DeskValidator`) is broken and removed.
- **GATT table is fixed by hardware spec** — missing characteristics on a fresh connect are a stack fault, not a device change; hence cache-clear + bounded retry rather than re-identification or config re-validation.
- **`config_flow.py` is intentionally untouched** — its validation churn runs once, pre-setup, and is out of scope (follow-up if evidence ever points there).
- **Follow-ups (not in this project):** upstream `uplift-ble` PRs for (a) fully idempotent `start()`, (b) no-op `stop_notify` when disconnected, (c) a supported way to override the unit instead of mutating `controller._unit`; consider `pytest-homeassistant-custom-component` version bumps as HA releases move.
