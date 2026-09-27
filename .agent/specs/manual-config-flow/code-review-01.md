# Code Review 01 — Manual Config Flow Implementation

**Date:** 2026-05-08
**Scope:** `config_flow.py` implementation review against `manual-config-flow.md` plan and `discovery-flow-reference.md` spec
**Files reviewed:**
- `custom_components/uplift_desk/config_flow.py` (full file, 345 lines)
- `custom_components/uplift_desk/const.py` (full file)
- `custom_components/uplift_desk/strings.json` (full file)
- `custom_components/uplift_desk/translations/en.json` (full file)
- `custom_components/uplift_desk/models.py` (full file)
- `custom_components/uplift_desk/__init__.py` (full file)
- `custom_components/uplift_desk/coordinator.py` (full file)
- `custom_components/uplift_desk/manifest.json` (full file)
- Installed package: `uplift_ble/desk_validator.py`, `ble_protos.py`, `models.py`

---

## Passing Checks (Summary)

The following checks passed without issue.

| # | Check | Status |
|---|-------|--------|
| 1 | All 3 config flow steps implemented | PASS |
| 2 | MAC address validation (colon + no-separator) | PASS |
| 3 | `_ManualBLEDevice` stub is BLEDeviceProtocol-compatible | PASS |
| 5 | `_abort_if_unique_id_configured()` called before validation | PASS |
| 6 | Discovery + manual flow converge without conflict | PASS |
| 7 | Entry creation matches reference pattern | PASS |
| 8 | Unique ID set to MAC address | PASS |

---

## Partial / Failing Checks — Detailed Analysis

---

### Check 4: `validate_device()` wiring uses `BLEAK_TIMEOUT_SECONDS`, handles `TimeoutError`/`Exception` gracefully

**Verdict: PARTIAL — Timeout wiring is correct; exception handling is inconsistent across flows.**

#### What is correct

All three config flow steps pass `timeout=BLEAK_TIMEOUT_SECONDS` (value `15` from `const.py`) to `DeskValidator.validate_device()`:

| Flow | Location | Code |
|------|----------|------|
| `async_step_bluetooth` | config_flow.py:78 | `await self._desk_validator.validate_device(discovery_info, timeout=BLEAK_TIMEOUT_SECONDS)` |
| `async_step_user` (dropdown path) | config_flow.py:165 | `await self._desk_validator.validate_device(selected_info, timeout=BLEAK_TIMEOUT_SECONDS)` |
| `async_step_user_manual` | config_flow.py:265 | `await self._desk_validator.validate_device(manual_device, timeout=BLEAK_TIMEOUT_SECONDS)` |

This matches the plan's requirement: *"Use `BLEAK_TIMEOUT_SECONDS` (15s) for all connection attempts."*

#### Exception handling divergence

The `uplift_ble` package's `DeskValidator.validate_device()` already catches `TimeoutError`, `EOFError`, and generic `Exception` internally (desk_validator.py:114–133), returning `None` on failure. The config flow then inspects the return value and catches any exceptions that *escape* the validator.

Here is how each flow handles the two exception types:

**`async_step_bluetooth` (lines 80–91):**
```python
except TimeoutError:
    logger.warning(...)
    return None          # Returns None (no explicit abort)
except Exception as e:
    logger.error(...)
    return None          # Returns None (no explicit abort)

if self._discovered_device is None:
    return None          # Returns None (no explicit abort)
```

**`async_step_user` dropdown path (lines 166–207):**
```python
except TimeoutError:
    logger.warning(...)
    return self.async_show_form(
        step_id="user",
        data_schema=...,
        errors={"base": "connection_failed"},
    )
except Exception as e:
    logger.error(...)
    return self.async_show_form(
        step_id="user",
        data_schema=...,
        errors={"base": "connection_failed"},
    )

if validated is None:
    return self.async_show_form(
        step_id="user",
        data_schema=...,
        errors={"base": "invalid_address"},
    )
```

**`async_step_user_manual` (lines 266–298):**
```python
except TimeoutError:
    logger.warning(...)
    return self.async_show_form(
        step_id="user_manual",
        data_schema=...,
        errors={"base": "connection_failed"},
    )
except Exception as e:
    logger.error(...)
    return self.async_show_form(
        step_id="user_manual",
        data_schema=...,
        errors={"base": "connection_failed"},
    )

if validated is None:
    return self.async_show_form(
        step_id="user_manual",
        data_schema=...,
        errors={"base": "invalid_address"},
    )
```

#### The problem

`async_step_bluetooth` returns `None` on every failure path. In Home Assistant's config flow architecture, returning `None` from a step method is interpreted as "no response" — the framework does not render a form, does not show an error, and does not abort. The user sees **nothing** when Bluetooth discovery fails to validate the device.

By contrast, both `async_step_user` and `async_step_user_manual` show explicit error forms with user-facing messages (`connection_failed`, `invalid_address`).

#### Why this matters

1. **Silent failure:** A Bluetooth-discovered device that fails validation (timeout, connection error, not a supported desk) produces zero user feedback. The discovery flow simply vanishes.
2. **Inconsistent UX:** The manual flow gives clear feedback; the bluetooth flow gives none.
3. **Not a new regression:** This was the pre-existing behavior of `async_step_bluetooth` before the manual flow was added. However, the manual flow implementation sets a precedent for explicit error handling, making the bluetooth flow's silence more conspicuous.

#### Recommended fix

Replace the `return None` branches in `async_step_bluetooth` with an explicit abort:

```python
except TimeoutError:
    logger.warning(...)
    return self.async_abort(reason="connection_failed")
except Exception as e:
    logger.error(...)
    return self.async_abort(reason="connection_failed")

if self._discovered_device is None:
    return self.async_abort(reason="invalid_address")
```

This requires adding the corresponding abort keys to `strings.json`:
```json
"abort": {
    "connection_failed": "Failed to connect to the desk...",
    "invalid_address": "The discovered device is not a supported desk."
}
```

---

### Check 9: Title placeholders are set for config entry

**Verdict: MINOR — Placeholders are set correctly, but unused translation keys exist.**

#### What is correct

Both confirmation steps set `self.context["title_placeholders"]`:

**`async_step_bluetooth_confirm` (lines 109–111):**
```python
self._set_confirm_only()
placeholders = {"name": title}
self.context["title_placeholders"] = placeholders
```

**`async_step_user_confirm` (lines 338–340):**
```python
self._set_confirm_only()
placeholders = {"name": name, "address": address}
self.context["title_placeholders"] = placeholders
```

This matches the reference doc's pattern of using `title_placeholders` to display the device name in the UI header when `_set_confirm_only()` suppresses the main form title.

The bluetooth confirm uses `{"name": title}` (single placeholder, matching the reference exactly). The user confirm uses `{"name": name, "address": address}` (two placeholders), which is more informative and aligns with the `user_confirm` description template that references both `{name}` and `{address}`.

#### The problem: unused `confirm` field in translations

The `strings.json` (lines 24–29) and `translations/en.json` (lines 24–29) declare a `data` field for `user_confirm`:

```json
"user_confirm": {
    "title": "Confirm Desk Details",
    "description": "Please confirm the details for your desk:\n\n**Name:** `{name}`\n**Address:** `{address}`",
    "data": {
        "confirm": "Confirm"
    }
}
```

However, the code in `async_step_user_confirm` **never declares this field in the form schema** and **never reads it from `user_input`**:

```python
async def async_step_user_confirm(
    self, user_input: dict[str, Any] | None = None
) -> ConfigFlowResult:
    # ...
    if user_input is not None:
        return self.async_create_entry(...)   # No user_input["confirm"] read

    self._set_confirm_only()
    placeholders = {"name": name, "address": address}
    self.context["title_placeholders"] = placeholders
    return self.async_show_form(
        step_id="user_confirm",
        description_placeholders=placeholders,
        # No data_schema parameter — no fields declared
    )
```

The `async_show_form` call omits `data_schema`, which means **no input fields are rendered** — only the description text. The `confirm` button is the default "Submit" button that Home Assistant adds automatically to confirmation forms.

The `"confirm": "Confirm"` label in `strings.json` is dead translation data. It will never be rendered.

#### Recommended fix

Either:
- **Option A (remove dead data):** Delete the `"data": {"confirm": "Confirm"}` key from both `strings.json` and `translations/en.json`. The default submit button label is fine.
- **Option B (add the field):** Add a `data_schema` with a checkbox or confirm field if explicit user confirmation is desired:
  ```python
  return self.async_show_form(
      step_id="user_confirm",
      data_schema=vol.Schema({
          vol.Required("confirm"): bool,
      }),
      description_placeholders=placeholders,
  )
  ```

---

### Check 10: No regressions in existing bluetooth discovery flow

**Verdict: MINOR — No code-level regression; pre-existing UX gap remains.**

#### What is preserved

The bluetooth discovery flow is functionally unchanged:

| Aspect | Before | After | Status |
|--------|--------|-------|--------|
| `async_step_bluetooth` signature | `discovery_info: BluetoothServiceInfoBleak` | Same | OK |
| `async_step_bluetooth_confirm` signature | `user_input: dict | None` | Same | OK |
| Unique ID = `discovery_info.address` | Yes | Yes | OK |
| `_abort_if_unique_id_configured()` call | Yes | Yes | OK |
| `DeskValidator` instantiation | `DeskValidator()` | Same | OK |
| `timeout=BLEAK_TIMEOUT_SECONDS` | Yes | Yes | OK |
| Entry creation pattern | `title`, `{"address", "name"}` | Same | OK |
| `_set_confirm_only()` | Yes | Yes | OK |
| `title_placeholders` injection | Yes | Yes | OK |

No existing lines were modified in a way that breaks the bluetooth flow's behavior.

#### The pre-existing UX gap (see Check 4 analysis)

The bluetooth flow returns `None` on validation failure, meaning users get no feedback when a discovered device cannot be validated. This was **not introduced by the manual flow implementation** — it was present before. However, the manual flow's explicit error handling (`errors={"base": "connection_failed"}`) makes this silence more noticeable.

The `discovery-flow-reference.md` section 1 describes the bluetooth flow as a linear success path (steps 1–6) with no failure branches documented. This suggests the reference doc itself was written with an optimistic/idealized view of the flow.

#### Conclusion

No code regression. The bluetooth flow works exactly as it did before. The UX gap is a pre-existing issue that the manual flow implementation did not introduce, but which the manual flow's better error handling makes more apparent.

---

## Additional Gaps Found

---

### Gap A: `DiscoveredDesk` type mismatch between local and installed package

**Severity: Low (latent risk)**

The local `models.py` defines a simplified `DiscoveredDesk`:
```python
@dataclass
class DiscoveredDesk():
    name: str
    address: str
```

The installed `uplift_ble==0.5.0` package defines a richer version:
```python
@dataclass
class DiscoveredDesk:
    address: str
    name: str | None
    desk_config: DeskConfig
```

**Implications:**

1. The config flow imports `DiscoveredDesk` from the local `models.py` (config_flow.py:11), but `DeskValidator.validate_device()` returns the **package** version (with `desk_config`). At runtime, the returned object has a `desk_config` attribute that the local type annotation doesn't declare.

2. The local `DiscoveredDesk` is used in `coordinator.py` (line 89) to construct a device object for the coordinator:
   ```python
   self._discovered_desk = DiscoveredDesk(name=desk_name, address=desk_address)
   ```
   This is then passed to `validate_device()` which only reads `.address` and `.name` — so it works structurally.

3. If any future code accesses `self._discovered_device.desk_config` (which the package version has but the local version doesn't), it will raise `AttributeError` at runtime.

4. The TODO comment on config_flow.py:6 says: `"TODO: Revert this back to installed uplift_ble package instead of local"`. Once this TODO is acted on, the local `models.py` will be removed and the package's `DiscoveredDesk` will be used directly, resolving this mismatch.

**Recommendation:** No immediate action needed. This gap resolves itself when the TODO is addressed (reverting to the installed package).

---

### Gap B: Dead code — `_discovered_devices` dict

**Severity: Low (cleanup opportunity)**

The `__init__` method initializes (config_flow.py:63–65):
```python
self._discovered_devices: dict[
    str, tuple[DiscoveredDesk, BluetoothServiceInfoBleak]
] = {}
```

This dict is **never populated or read** anywhere in the codebase. It was likely scaffolding for a future multi-device selection UI mentioned in the plan.

**Recommendation:** Remove it for cleanliness, or add a comment noting it's planned for future multi-device selection.

---

### Gap C: Test infrastructure not yet created

**Severity: Medium (plan compliance)**

Per the tasks doc (Track D), the following files are planned but **do not exist**:
- `tests/__init__.py`
- `tests/conftest.py` (shared fixtures)
- `tests/test_config_flow.py` (unit tests for all scenarios)

The `tests/` directory itself does not exist.

The plan (Phase 3, tasks D1–D2) specifies 8 test scenarios covering:
1. Manual entry with valid, in-range address → success
2. Manual entry with invalid MAC format → form-level error
3. Manual entry with valid-format address, out of range → timeout error
4. Duplicate device (same MAC) → `already_configured` abort
5. Cancel during flow → clean abort
6. Discovery + manual converge on same device → no conflict
7. `async_step_user` with no discovered devices → manual entry
8. `async_step_user` with discovered devices → dropdown

**Recommendation:** This is a Track D task in the tasks doc. It is a planned next step, not a code defect.

---

### Gap D: Redundant `address_data` key in translations

**Severity: Low (dead data)**

The `strings.json` (line 21) declares `address_data` as a translation key under `user_manual`:
```json
"user_manual": {
    "data": {
        "address": "Bluetooth Address",
        "name": "Desk Name",
        "address_data": "Bluetooth Address"
    }
}
```

The form schema (config_flow.py:310–313) only uses `address` and `name`:
```python
vol.Required("address"): str,
vol.Optional("name"): str,
```

`address_data` is never referenced. It is a duplicate of the `address` label.

**Recommendation:** Remove the `address_data` key from both `strings.json` and `translations/en.json`.

---

### Gap E: Unused `confirm` field in `user_confirm` translations

**Severity: Low (dead data)**

Same issue as Check 9. The `user_confirm` step declares a `data` field with a `confirm` key, but the form has no `data_schema` and the code never reads `user_input["confirm"]`.

**Recommendation:** Remove the `"data": {"confirm": "Confirm"}` key from both `strings.json` and `translations/en.json` (see Check 9 for full analysis).

---

## Summary Table

| # | Check | Status | Severity | Action Required |
|---|-------|--------|----------|-----------------|
| 4 | `validate_device()` timeout + error handling | PARTIAL | Medium | Fix `async_step_bluetooth` to return explicit abort instead of `None` |
| 9 | Title placeholders | MINOR | Low | Remove unused `confirm` field from translations |
| 10 | No bluetooth discovery regressions | MINOR | Low | Pre-existing gap acknowledged; no action needed |
| A | `DiscoveredDesk` type mismatch | INFO | Low | Resolves when TODO (revert to package) is addressed |
| B | `_discovered_devices` dead code | INFO | Low | Optional cleanup |
| C | Test infrastructure missing | INFO | Medium | Planned next step (Track D) |
| D | Redundant `address_data` key | INFO | Low | Remove dead translation key |
| E | Unused `confirm` field in translations | MINOR | Low | Remove dead translation key (see Check 9) |

---

## Overall Assessment

The manual config flow implementation is **substantially correct** and aligns well with the plan. The core logic — three-step flow, MAC validation, BLEDeviceProtocol stub, timeout wiring, unique ID handling, flow convergence, and entry creation — is all properly implemented.

The most impactful finding is **Check 4**: the `async_step_bluetooth` flow's `return None` on failure produces silent failures with zero user feedback. This is the only item that should be addressed before merging, as it affects the user experience of the discovery flow.

All other gaps are either pre-existing issues, dead translation data, or planned future work (tests).
