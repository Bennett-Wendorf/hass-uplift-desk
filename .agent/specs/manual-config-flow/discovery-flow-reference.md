# Uplift Desk Discovery Flow — Complete Reference

## 1. `async_step_bluetooth` — Entry Point (Auto-Discovery)

**Triggered by:** Home Assistant's Bluetooth scanner detects a matching BLE advertisement.

**Signature:**
```python
async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> ConfigFlowResult
```

**Step-by-step flow:**

| Step | Code | Purpose |
|------|------|---------|
| 1 | `await self.async_set_unique_id(discovery_info.address)` | Sets the config entry's unique ID to the BLE device's MAC address. This prevents duplicate entries for the same physical desk. |
| 2 | `self._abort_if_unique_id_configured()` | If a config entry with this unique ID already exists, aborts the flow (no re-discovery). |
| 3 | `self._discovery_info = discovery_info` | **Stores** the raw `BluetoothServiceInfoBleak` object on `self._discovery_info` for use in the confirm step. |
| 4 | `self._desk_validator = DeskValidator()` | Creates a `DeskValidator` instance (from the local `uplift_ble` package). |
| 5 | `self._discovered_device = await self._desk_validator.validate_device(discovery_info)` | **Validates** the BLE advertisement. On success, stores a `Desk` object on `self._discovered_device`. |
| 6 | `return await self.async_step_bluetooth_confirm()` | Immediately advances to the confirmation step. |

**Key data stored on `self` during this step:**
- `self._discovery_info` — `BluetoothServiceInfoBleak` (raw BLE advertisement data)
- `self._discovered_device` — `Desk` (validated device object from `DeskValidator`)
- `self._desk_validator` — `DeskValidator` instance (used for re-validation in coordinator)

---

## 2. `async_step_bluetooth_confirm` — User Confirmation

**Triggered by:** Automatically from `async_step_bluetooth` after validation.

**Signature:**
```python
async def async_step_bluetooth_confirm(
    self, user_input: dict[str, Any] | None = None
) -> ConfigFlowResult
```

**Step-by-step flow:**

| Step | Code | Purpose |
|------|------|---------|
| 1 | `assert self._discovered_device is not None` / `assert self._discovery_info is not None` | Defensive assertions — these must have been set in the bluetooth step. |
| 2 | `title = discovery_info.name` | Extracts the device name from the BLE advertisement as the entry title. |
| 3a | `return self.async_create_entry(title=title, data={"address": discovery_info.address, "name": discovery_info.name})` | **Creates the config entry.** See section 3 below. |
| 4a | `self._set_confirm_only()` | Tells HA this is a confirmation-only flow — main UI title is suppressed, replaced by `title_placeholders`. |
| 4b | `placeholders = {"name": title}` | Sets the device name as a placeholder for the UI title. |
| 4c | `self.context["title_placeholders"] = placeholders` | Injects the placeholder into the flow context so HA displays the device name in the UI header. |
| 4d | `return self.async_show_form(step_id="bluetooth_confirm", description_placeholders=placeholders)` | Renders a confirmation form ("Do you want to set up [device name]?"). |

**Data flow:**
```
self._discovery_info.address  →  unique_id (prevents duplicates)
self._discovery_info.name     →  title + UI placeholder
self._discovery_info.address  →  entry.data["address"]
self._discovery_info.name     →  entry.data["name"]
```

---

## 3. `async_create_entry()` — Exact Entry Payload

```python
return self.async_create_entry(
    title=title,                                        # discovery_info.name (e.g., "Uplift Desk")
    data={
        "address": discovery_info.address,              # BLE MAC address string
        "name": discovery_info.name,                    # BLE device name string
    }
)
```

**What HA stores internally for this config entry:**

| Field | Value | Source |
|-------|-------|--------|
| `entry.entry_id` | Auto-generated UUID | Home Assistant |
| `entry.domain` | `"uplift_desk"` | From `ConfigFlow` class `domain=DOMAIN` |
| `entry.title` | `"Uplift Desk"` (or `discovery_info.name`) | User-visible title |
| `entry.unique_id` | `discovery_info.address` (BLE MAC) | Set by `async_set_unique_id()` |
| `entry.data` | `{"address": "<MAC>", "name": "<device_name>"}` | Passed to `async_create_entry()` |
| `entry.version` | `1` | Class attribute `VERSION` |
| `entry.minor_version` | `1` | Class attribute `MINOR_VERSION` |
| `entry.runtime_data` | `UpliftDeskBluetoothCoordinator` | Populated in `__init__.py:async_setup_entry()` |

**Note:** The entry `data` is **minimal** — just `address` and `name`. No BLE scan results, rssi, service UUIDs, or manufacturer data are persisted.

---

## 4. Post-Creation: `__init__.py:async_setup_entry()` — Coordinator Lifetime

After the config entry is created, HA calls `async_setup_entry()`:

```python
async def async_setup_entry(hass: HomeAssistant, entry: Uplift_Desk_DeskConfigEntry) -> bool:
    # 1. Extract address/name from entry.data
    coordinator = UpliftDeskBluetoothCoordinator(
        hass, entry,
        entry.data["address"],   # desk_address
        entry.data["name"]       # desk_name
    )
    # 2. Attach coordinator to entry
    entry.runtime_data = coordinator
    # 3. Connect to the desk
    await coordinator.async_connect()
    # 4. Read initial height
    await coordinator.async_read_desk_height()
    coordinator.async_set_updated_data(coordinator._desk)
    # 5. Forward to sensor/button platforms
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    return True
```

**Data chain:**
```
entry.data["address"]  →  coordinator._discovered_desk.address
entry.data["name"]     →  coordinator._discovered_desk.name
```

The coordinator then uses `DeskValidator.validate_device()` again (with a timeout) to re-validate and create a `BleakClient` + `DeskController` for active BLE communication.

---

## 5. `Uplift_Desk_DeskConfigEntry` Type Alias Pattern

**Definition** (in `coordinator.py`, line 33):
```python
type Uplift_Desk_DeskConfigEntry = ConfigEntry[UpliftDeskBluetoothCoordinator]
```

**What this means:**
- This is a **Python 3.12+ `type` statement** (PEP 695), not a `typing.TypeAlias` or `typing.NewType`.
- It creates a **generic type alias** that binds `ConfigEntry` to a specific `runtime_data` type.
- `ConfigEntry[T]` is Home Assistant's typed config entry where `T` is the type of `entry.runtime_data`.
- This pattern provides **type safety**: anywhere `Uplift_Desk_DeskConfigEntry` is used, `entry.runtime_data` is statically known to be `UpliftDeskBluetoothCoordinator`.

**Usage locations:**

| File | Where |
|------|-------|
| `coordinator.py:46` | Parameter type for `process_service_info()` |
| `coordinator.py:80` | Parameter type for `UpliftDeskBluetoothCoordinator.__init__()` |
| `__init__.py:21,38` | Parameter type for `async_setup_entry()` and `async_unload_entry()` |

**Key convention:** The type alias name uses **snake_case with underscores** (`Uplift_Desk_DeskConfigEntry`) rather than PascalCase, distinguishing it from class names. This is a Home Assistant convention for config entry type aliases.

---

## 6. `_discovered_device` and `_discovery_info` — Storage & Lifecycle

Both are **instance attributes** on the `UpliftDeskConfigFlow` class, stored in `__init__`:

```python
def __init__(self) -> None:
    self._discovery_info: BluetoothServiceInfoBleak | None = None
    self._discovered_device: Desk | None = None
    self._discovered_devices: dict[str, tuple[Desk, BluetoothServiceInfoBleak]] = {}
```

| Attribute | Type | Set In | Used In | Lifespan |
|-----------|------|--------|---------|----------|
| `_discovery_info` | `BluetoothServiceInfoBleak \| None` | `async_step_bluetooth` | `async_step_bluetooth_confirm` | Entire flow lifetime (cleared after entry creation) |
| `_discovered_device` | `Desk \| None` | `async_step_bluetooth` | `async_step_bluetooth_confirm` | Entire flow lifetime (cleared after entry creation) |
| `_discovered_devices` | `dict[str, tuple[Desk, BluetoothServiceInfoBleak]]` | (unused) | (unused) | Entire flow lifetime |

**Important:** `_discovered_devices` is initialized but **never populated or read** in the current codebase — it appears to be scaffolding for a future multi-device selection UI.

**The `Desk` type** is referenced but not imported in `config_flow.py` — it comes from the local `uplift_ble.desk_controller` package (currently not present in the workspace, likely a TODO to revert to the external `uplift_ble` package per the comment on line 3 of `config_flow.py`).

---

## 7. End-to-End Summary Diagram

```
BLE Advertisement Detected
        │
        ▼
async_step_bluetooth(discovery_info)
   ├─ set_unique_id(address)           → prevents duplicates
   ├─ abort_if_unique_id_configured()  → skip if already set up
   ├─ self._discovery_info = discovery_info
   ├─ self._discovered_device = validate_device(discovery_info)
   └─ → async_step_bluetooth_confirm()
              │
              ▼
   async_step_bluetooth_confirm()
      ├─ First render (user_input=None):
      │    ├─ _set_confirm_only()
      │    ├─ title_placeholders = {"name": device_name}
      │    └─ async_show_form("bluetooth_confirm")
      │         → User sees "Set up [device name]?" dialog
      │
      └─ After user submits:
           └─ async_create_entry(
                title=device_name,
                data={"address": mac, "name": device_name}
              )
              → ConfigEntry stored in HA
              │
              ▼
   __init__.py:async_setup_entry(entry)
      ├─ coordinator = UpliftDeskBluetoothCoordinator(hass, entry, address, name)
      ├─ entry.runtime_data = coordinator
      ├─ await coordinator.async_connect()
      ├─ await coordinator.async_read_desk_height()
      └─ async_forward_entry_setups([SENSOR, BUTTON])
```

---

## Key Observations

1. **Minimal persisted data:** Only `address` and `name` are stored in the config entry. All BLE scan metadata is transient.
2. **Two-phase validation:** Device is validated once during discovery (config flow) and again during coordinator initialization (`async_connect` → `validate_device` with timeout).
3. **No user input customization:** The `async_step_user` method is a stub with placeholder fields (`test1`, `test2`). Manual setup is not yet implemented.
4. **`_discovered_devices` is dead code:** The dict is initialized but never used — likely planned for multi-device selection.
5. **`Desk` type import missing:** `config_flow.py` references `Desk` without importing it, suggesting incomplete migration from the `uplift_ble` package.
