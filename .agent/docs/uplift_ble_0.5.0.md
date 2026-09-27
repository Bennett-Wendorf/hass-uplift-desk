# uplift_ble==0.5.0 Package Documentation

> **Package:** `uplift-ble==0.5.0`
> **Installed:** `/home/bennett/.local/lib/python3.14/site-packages/uplift_ble/`
> **Generated:** 2026-05-08

## Overview

`uplift_ble` is a BLE (Bluetooth Low Energy) library for controlling Uplift brand standing desks. It communicates with desk hardware via vendor-specific GATT services and characteristics, using a custom packet protocol. The library supports four desk variants (all JIECANG hardware), identified by their BLE service UUID prefixes.

## Package Structure

| File | Purpose |
|------|---------|
| `__init__.py` | Public exports |
| `ble_protos.py` | Protocol definitions (interfaces) |
| `ble_helpers.py` | Helper utilities for GATT characteristics |
| `models.py` | Data classes (`DiscoveredDesk`) |
| `desk_validator.py` | Device validation logic |
| `desk_controller.py` | Full BLE desk control |
| `desk_enums.py` | Enumerations |
| `desk_configs.py` | Desk variant configuration mapping |
| `desk_finder.py` | Convenience scanner+validator |
| `desk_scanner.py` | BLE device scanner |
| `packet.py` | Packet encoding/decoding |
| `utils.py` | Conversion utilities |
| `byte_maps.py` | Byte-to-enum mappings |

---

## 1. `DeskValidator.validate_device()`

**File:** `desk_validator.py`

### Full Signature

```python
async def validate_device(
    self,
    device: BLEDeviceProtocol,
    timeout: float = 5.0,
) -> DiscoveredDesk | None:
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `self` | `DeskValidator` | — | Instance of `DeskValidator` |
| `device` | `BLEDeviceProtocol` | — | The BLE device to validate. Must have `address` (str) and `name` (str | None) properties. |
| `timeout` | `float` | `5.0` | Maximum time in **seconds** for the connection attempt and validation. Passed to the BleakClient constructor. |

### Return Type

| Type | Description |
|------|-------------|
| `DiscoveredDesk | None` | Returns a fully-populated `DiscoveredDesk` on successful validation, or `None` if the device is not a supported desk or validation fails. |

### Timeout Behavior

1. **Connection timeout:** The `timeout` parameter is passed to `BleakClient(address_or_ble_device=device, timeout=timeout)`. If the BLE connection cannot be established within this window, a `TimeoutError` is raised and caught internally, returning `None`.

2. **Per-device processing:** The timeout governs the entire connection lifecycle (connect -> enumerate services -> check characteristics -> disconnect via context manager).

3. **Default value:** The class method `validate_devices()` defaults to `timeout=10.0`, while `validate_device()` defaults to `timeout=5.0`.

### Exceptions Handled Internally

| Exception | Behavior |
|-----------|----------|
| `EOFError` | If `discovered_desk` was already set (validation succeeded but cleanup failed), returns `discovered_desk`. Otherwise returns `None`. |
| `TimeoutError` | Logs a warning about connection timeout, returns `None`. |
| `Exception` (any other) | Logs the error, returns `None`. |

### Validation Logic

1. Opens an async connection to the device using `BleakClient`.
2. Checks `client.is_connected` -- if not connected, returns `None`.
3. Iterates over `client.services`, looking for a service UUID that matches a key in `DESK_CONFIGS_BY_SERVICE`.
4. For each matching service, calls `_service_has_required_characteristics()`, which verifies the service contains **all three** required GATT characteristics:
   - `input_char_uuid` (command input)
   - `output_char_uuid` (notification output)
   - `name_char_uuid` (device name)
5. If a match is found, constructs and returns a `DiscoveredDesk` with the device's address, name, and the matching `DeskConfig`.
6. If no services match, returns `None`.

---

## 2. `BLEDeviceProtocol`

**File:** `ble_protos.py`

### Full Definition

```python
class BLEDeviceProtocol(Protocol):
    @property
    def name(self) -> str | None: ...

    @property
    def address(self) -> str: ...
```

This is a **typing `Protocol`** (structural subtyping interface) that defines the minimal contract for any BLE device object. It is used by `DeskValidator` and `DeskScanner` to accept both real `bleak.BLEDevice` objects and mock/fake devices for testing.

### Properties

| Property | Type | Description |
|----------|------|-------------|
| `name` | `str | None` | Human-readable device name. May be `None` if not available. |
| `address` | `str` | BLE MAC address of the device (e.g., `"AA:BB:CC:DD:EE:FF"`). |

### Related Protocols (same file)

```python
class GATTCharacteristicProtocol(Protocol):
    @property
    def uuid(self) -> str: ...

class GATTServiceProtocol(Protocol):
    @property
    def uuid(self) -> str: ...
    @property
    def characteristics(self) -> list[GATTCharacteristicProtocol]: ...

class GATTServiceCollectionProtocol(Protocol):
    def __iter__(self) -> Iterator[GATTServiceProtocol]: ...

class BLEClientProtocol(Protocol):
    @property
    def is_connected(self) -> bool: ...
    @property
    def services(self) -> GATTServiceCollectionProtocol: ...
    async def __aenter__(self) -> "BLEClientProtocol": ...
    async def __aexit__(self, *args) -> None: ...
```

---

## 3. `DiscoveredDesk`

**File:** `models.py`

### Fields

```python
@dataclass
class DiscoveredDesk:
    address: str
    name: str | None
    desk_config: DeskConfig
```

| Field | Type | Description |
|-------|------|-------------|
| `address` | `str` | BLE MAC address of the desk. |
| `name` | `str | None` | Human-readable device name. |
| `desk_config` | `DeskConfig` | Configuration object describing the desk variant, including all GATT service/characteristic UUIDs. |

### Methods

#### `create_controller(client: BleakClient, notification_timeout: float = 1.0) -> DeskController`

Creates a `DeskController` instance configured for this specific desk.

```python
def create_controller(
    self,
    client: BleakClient,
    notification_timeout: float = 1.0,
) -> DeskController:
    """Create a controller for this desk."""
    return DeskController(
        client=client,
        input_char_uuid=self.desk_config.input_char_uuid,
        output_char_uuid=self.desk_config.output_char_uuid,
        requires_wake=self.desk_config.requires_wake,
        notification_timeout=notification_timeout,
    )
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `client` | `BleakClient` | — | An already-connected BleakClient instance for the desk. |
| `notification_timeout` | `float` | `1.0` | Max seconds to wait for notifications after sending a command. |

**Returns:** A fully-configured `DeskController` instance.

---

## 4. `DeskController` -- Key Methods

**File:** `desk_controller.py`

### Constructor

```python
def __init__(
    self,
    client: BleakClient,
    input_char_uuid: str,
    output_char_uuid: str,
    requires_wake: bool,
    notification_timeout: float = 1.0,
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `client` | `BleakClient` | — | Bleak client connected to the desk. |
| `input_char_uuid` | `str` | — | GATT characteristic UUID for writing commands. |
| `output_char_uuid` | `str` | — | GATT characteristic UUID for reading notifications. |
| `requires_wake` | `bool` | — | Whether wake commands must be sent before operations. |
| `notification_timeout` | `float` | `1.0` | Seconds to wait for notifications after each command. |

### Async Context Manager

| Method | Signature | Description |
|--------|-----------|-------------|
| `start()` | `async def start()` | Starts BLE notification subscription and spawns the notification processor task. |
| `stop()` | `async def stop()` | Cancels the processor task and stops BLE notifications. |

### Read-Only Properties (populated by notifications)

| Property | Type | Description |
|----------|------|-------------|
| `height_mm` | `float | None` | Current desk height in millimeters. |
| `unit` | `DeskUnit | None` | Display unit preference (centimeters or inches). |
| `touch_mode` | `DeskTouchMode | None` | Button press behavior mode. |
| `lock_status` | `DeskLockStatus | None` | Current lock state. |
| `height_limit_config_max_mm` | `int | None` | Max height limit from initial config (mm). |
| `height_limit_config_min_mm` | `int | None` | Min height limit from initial config (mm). |
| `height_limit_max_mm` | `int | None` | Current max height limit (mm). |
| `height_limit_min_mm` | `int | None` | Current min height limit (mm). |
| `height_preset_1` | `int | None` | Preset 1 height (units vary by hardware). |
| `height_preset_2` | `int | None` | Preset 2 height (units vary by hardware). |
| `height_preset_3` | `int | None` | Preset 3 height (units vary by hardware). |
| `height_preset_4` | `int | None` | Preset 4 height (units vary by hardware). |

### Event System

| Method | Signature | Description |
|--------|-----------|-------------|
| `on()` | `def on(event: DeskEventType, handler: Callable)` | Register an event handler callback for a specific event type. |
| `_emit()` | `def _emit(event_type: DeskEventType, *args)` | Internal: dispatches event to all registered handlers. |

### Command Methods (all return `DeskCommand`)

All command methods are decorated with `@command_writer()` which:
1. Sends 3 wake commands (if `requires_wake=True`) with 0.1s pauses.
2. Converts the command to a BLE packet with checksum.
3. Writes to the input characteristic (`response=False`).
4. Waits `notification_timeout` seconds for responses.

| Method | Opcode | Payload | Description |
|--------|--------|---------|-------------|
| `wake()` | `0x00` | `b""` | Wake the desk (skips extra wake preamble). |
| `move_up()` | `0x01` | `b""` | Move desk upward. |
| `move_down()` | `0x02` | `b""` | Move desk downward. |
| `move_to_height_preset_1()` | `0x05` | `b""` | Move to preset position 1. |
| `move_to_height_preset_2()` | `0x06` | `b""` | Move to preset position 2. |
| `request_height_limits()` | `0x07` | `b""` | Request height limit configuration. |
| `set_calibration_offset(calibration_offset: int)` | `0x10` | 2 bytes BE | Set calibration offset (0-65535). |
| `set_height_limit_max(max_height: int)` | `0x11` | 2 bytes BE | Set max height limit (0-65535 mm). |
| `set_touch_mode(touch_mode: DeskTouchMode)` | `0x19` | 1 byte | Set touch mode (ONE_TOUCH/CONSTANT_TOUCH). |
| `move_to_specified_height(height: int)` | `0x1B` | 2 bytes BE | Move to a specific height (0-65535). |
| `set_current_height_as_height_limit_max()` | `0x21` | `b""` | Set max limit to current height. |
| `set_current_height_as_height_limit_min()` | `0x22` | `b""` | Set min limit to current height. |
| `clear_height_limit(limit: DeskClearHeightLimit)` | `0x23` | 1 byte | Clear a height limit (MAX/MIN). |
| `stop_movement()` | `0x2B` | `b""` | Stop all desk movement. |
| `set_units(unit: DeskUnit)` | `0x0E` | 1 byte | Set display units (CENTIMETERS/INCHES). |
| `reset()` | `0xFE` | `b""` | Initiate desk reset. |

### Notification Handlers (internal, triggered by BLE notifications)

| Handler | Opcode | Payload | Description |
|---------|--------|---------|-------------|
| `_process_notification_0x01` | `0x01` | 3 bytes | Current height (tenths of mm). Emits `HEIGHT`. |
| `_process_notification_0x02` | `0x02` | 1 byte | Error code. Emits `ERROR_CODE`. |
| `_process_notification_0x04` | `0x04` | 0 bytes | Reset required. Emits `RESET`. |
| `_process_notification_0x07` | `0x07` | 4 bytes | Height limit config (max+min in mm). Emits `HEIGHT_LIMITS_CONFIGURATION`. |
| `_process_notification_0x0E` | `0x0E` | 1 byte | Display unit. Emits `UNIT`. |
| `_process_notification_0x19` | `0x19` | 1 byte | Touch mode. Emits `TOUCH_MODE`. |
| `_process_notification_0x1F` | `0x1F` | 1 byte | Lock status. Emits `LOCK_STATUS`. |
| `_process_notification_0x21` | `0x21` | 2 bytes | Max height limit (mm). Emits `HEIGHT_LIMIT_MAX`. |
| `_process_notification_0x22` | `0x22` | 2 bytes | Min height limit (mm). Emits `HEIGHT_LIMIT_MIN`. |
| `_process_notification_0x25` | `0x25` | 2 bytes | Preset 1 height. Emits `HEIGHT_PRESET_1`. |
| `_process_notification_0x26` | `0x26` | 2 bytes | Preset 2 height. Emits `HEIGHT_PRESET_2`. |
| `_process_notification_0x27` | `0x27` | 2 bytes | Preset 3 height. Emits `HEIGHT_PRESET_3`. |
| `_process_notification_0x28` | `0x28` | 2 bytes | Preset 4 height. Emits `HEIGHT_PRESET_4`. |

---

## 5. `DeskEventType`

**File:** `desk_enums.py`

```python
class DeskEventType(Enum):
    """Enumeration of desk notification events."""
    HEIGHT = "height"
    ERROR_CODE = "error_code"
    RESET = "reset"
    HEIGHT_LIMITS_CONFIGURATION = "height_limits_configuration"
    UNIT = "unit"
    TOUCH_MODE = "touch_mode"
    LOCK_STATUS = "lock_status"
    HEIGHT_LIMIT_MAX = "height_limit_max"
    HEIGHT_LIMIT_MIN = "height_limit_min"
    HEIGHT_PRESET_1 = "height_preset_1"
    HEIGHT_PRESET_2 = "height_preset_2"
    HEIGHT_PRESET_3 = "height_preset_3"
    HEIGHT_PRESET_4 = "height_preset_4"
```

| Enum Value | String Value | Handler | Payload |
|------------|-------------|---------|---------|
| `HEIGHT` | `"height"` | `0x01` | `float` -- height in mm |
| `ERROR_CODE` | `"error_code"` | `0x02` | `DeskErrorCode` enum |
| `RESET` | `"reset"` | `0x04` | (none) |
| `HEIGHT_LIMITS_CONFIGURATION` | `"height_limits_configuration"` | `0x07` | `(int, int)` -- (max_mm, min_mm) |
| `UNIT` | `"unit"` | `0x0E` | `DeskUnit` enum |
| `TOUCH_MODE` | `"touch_mode"` | `0x19` | `DeskTouchMode` enum |
| `LOCK_STATUS` | `"lock_status"` | `0x1F` | `DeskLockStatus` enum |
| `HEIGHT_LIMIT_MAX` | `"height_limit_max"` | `0x21` | `int` -- max height in mm |
| `HEIGHT_LIMIT_MIN` | `"height_limit_min"` | `0x22` | `int` -- min height in mm |
| `HEIGHT_PRESET_1` | `"height_preset_1"` | `0x25` | `int` -- raw value |
| `HEIGHT_PRESET_2` | `"height_preset_2"` | `0x26` | `int` -- raw value |
| `HEIGHT_PRESET_3` | `"height_preset_3"` | `0x27` | `int` -- raw value |
| `HEIGHT_PRESET_4` | `"height_preset_4"` | `0x28` | `int` -- raw value |

### Related Enums (same file)

```python
class DeskErrorCode(Enum):
    E01 = "E01"  # ... through E13 = "E13"
    H01 = "H01"
    H02 = "H02"
    LOCK = "LOCK"

class DeskClearHeightLimit(Enum):
    MAX = "max"
    MIN = "min"

class DeskUnit(Enum):
    CENTIMETERS = "centimeters"
    INCHES = "inches"

class DeskTouchMode(Enum):
    ONE_TOUCH = "one_touch"
    CONSTANT_TOUCH = "constant_touch"

class DeskLockStatus(Enum):
    UNLOCKED = "unlocked"
    LOCKED = "locked"
```

---

## 6. `DeskConfig` -- Desk Variant Configuration

**File:** `desk_configs.py`

```python
@dataclass
class DeskConfig:
    desk_variant: DeskVariant
    service_uuid: str
    input_char_uuid: str
    output_char_uuid: str
    name_char_uuid: str
    requires_wake: bool = True
```

### Supported Desk Variants

| Variant | Service UUID | Input Char | Output Char | Name Char |
|---------|-------------|------------|-------------|-----------|
| `JIECANG_0x00FF` | `000000ff-...` | `000001ff-...` | `000002ff-...` | `000036ef-...` |
| `JIECANG_0xFE60` | `0000fe60-...` | `0000fe61-...` | `0000fe62-...` | `0000fe63-...` |
| `JIECANG_0xFF00` | `0000ff00-...` | `0000ff01-...` | `0000ff02-...` | `0000fe63-...` |
| `JIECANG_0xFF12` | `0000ff12-...` | `0000ff01-...` | `0000ff02-...` | `0000ff06-...` |

All UUIDs are 128-bit BLE UUIDs normalized via `bleak.uuids.normalize_uuid_16()`.

---

## 7. Packet Protocol

**File:** `packet.py`

### Command Packet Format (sent to desk)

```
[0xF1, 0xF1] [len] [payload...] [checksum] [0x7E]
```

- **Header:** `0xF1F1` (2 bytes)
- **Opcode:** 1 byte (0x00-0xFF)
- **Payload length:** 1 byte (0x00-0xFF)
- **Payload:** variable
- **Checksum:** 1 byte = `(opcode + len(payload) + sum(payload)) mod 256`
- **Trailer:** `0x7E` (1 byte)

### Notification Packet Format (received from desk)

```
[0xF2, 0xF2] [len] [payload...] [checksum] [0x7E]
```

Same format as command packets, but uses header `0xF2F2` instead of `0xF1F1`. Multiple packets may be concatenated in a single BLE notification.

---

## 8. Usage Patterns in This Project

### In `config_flow.py` (discovery)

```python
validator = DeskValidator()
discovered_device = await validator.validate_device(discovery_info)
# discovery_info is a BluetoothServiceInfoBleak which satisfies BLEDeviceProtocol
```

### In `coordinator.py` (runtime control)

```python
validator = DeskValidator()
validated_desk = await validator.validate_device(self._discovered_desk, timeout=15)

client = BleakClient(validated_desk.address, timeout=15)
await client.connect()
controller = validated_desk.create_controller(client)

controller.on(DeskEventType.HEIGHT, self._async_height_notify_callback)
await controller.start()

# Later...
await controller.request_height_limits()
await controller.move_to_height_preset_1()
await controller.stop()
await client.disconnect()
```

---

## 9. Key Observations for the Hass Integration

1. **`validate_device()` default timeout is 5.0s** -- the integration overrides this to `BLEAK_TIMEOUT_SECONDS=15` in both `config_flow.py` and `coordinator.py`.

2. **`DiscoveredDesk` in the library requires `desk_config`** -- but the local `models.py` in the integration defines a simpler version without `desk_config`. This is a **compatibility mismatch** that will need to be resolved when reverting to the installed package.

3. **`DeskController` must be started** -- calling `start()` is required to begin receiving notifications. The controller supports async context manager protocol (`async with`).

4. **Commands are fire-and-forget** -- `@command_writer` writes with `response=False` and then waits for notifications via `asyncio.sleep()`. The return value is the raw packet bytes, not a parsed response.

5. **The `on()` event system uses synchronous callbacks** -- handlers are called directly (not via `asyncio.create_task`). For async handlers, the integration should use `asyncio.create_task()` or `async_dispatcher_send` in Home Assistant.

6. **`DeskFinder` is a convenience class** -- the package warns that Home Assistant integrations should not use it; instead, use `DeskValidator` with HA's `bluetooth.async_discovered_service_info()` API.
