# Troubleshooting

This guide covers the two connection bugs that were fixed in this work, how to
recognize them in the logs, and how to read the new coordinator log lines. It
also explains what to do when the desk still will not come back.

> Tip: none of the log lines below are errors you caused — most of them are
> the integration diagnosing and *fixing* itself. A healthy reconnect will
> still produce a warning or two; what matters is whether the desk ends up
> available again.

## The two bugs that were fixed

### Issue 1 — notifications never resumed after a reconnect

**What was wrong.** When the BLE link dropped and the integration reconnected,
it built a new connection but never re-registered the GATT *notifications*
that deliver live height updates. The result was a "dead but connected" desk:
the connection looked healthy, but nothing ever changed again.

**Pre-fix symptom (what you may have seen before):**

- After a drop, the height sensor was **frozen on its last value** while still
  showing as *available*.
- No height updates ever arrived again, even though the desk was technically
  "connected."
- The desk was unusable until you restarted Home Assistant or reloaded the
  integration.
- In the logs you might see the coordinator fetching a new controller and then
  going silent — no notification activity, no height callbacks.

**Post-fix expected behavior:**

- When the link drops, the sensor and buttons immediately show *unavailable*.
- The integration reconnects automatically (see
  [Disconnect & Reconnect Behavior](disconnect-reconnect-behavior.md)).
- On reconnect, notifications are re-registered, the height and units are
  re-read, and the sensor starts tracking the desk again — no restart needed.
- You will see `Started notifications for …` (debug) and
  `Reconnected to desk …` (info) in the logs, and the sensor value resumes
  updating.

### Issue 2 — empty or partial GATT service set on a connected client

**What was wrong.** The integration could *connect* to the desk, but the
connected client's GATT service set was **empty or partial**, so any command
failed deep inside with a `BleakCharacteristicNotFoundError`.

**The race, in plain terms.** The Linux Bluetooth stack (BlueZ) keeps a
**cache** of each device's GATT service list. When a device connects, the
stack waits for a "services are resolved" flag and then assembles the service
list from data that arrives on a separate D-Bus channel. Two things can go
wrong:

- If the "resolved" flag flips **before** all of the service data has been
  processed, the stack assembles an **empty or partial** service list and
  **caches it**.
- That cached empty list is only thrown away when the device is fully
  *removed* — **not** when it disconnects. So a later reconnect that reuses the
  cache gets the empty service set, even though the desk is connected and
  perfectly healthy.

The old code made this worse by opening extra connect/disconnect "validation"
connections, which churned the shared stack state and made the race more
likely.

**What the fix does.** Before a connection is used, the integration verifies
that the connected client actually exposes the desk's required services. If
the set is empty or partial, it clears the stale stack cache, discards that
client, and **reconnects once**. That converts the old failure — an
inexplicable error deep inside a command — into an early, detected, and
recoverable condition.

**Exact log signatures that confirm this race:**

- From the BlueZ manager logger (enable it, see below):
  `Using cached services for /org/bluez/hci0/dev_...` appearing on a connect
  whose service set then turns out to be empty or partial.
- From the coordinator (this work's new line):
  `Incomplete GATT services (attempt 1/2); clearing cache and retrying`.
- **Pre-fix only** (you should no longer see this in normal operation):
  `BleakCharacteristicNotFoundError: Characteristic 0000fe61-... was not
  found!` raised from the desk controller while handling a command.
- A related variant to watch for: `BleakError: Service Discovery has not been
  performed yet` — the sibling case where the service set is missing entirely.
  The validation path treats this the same way (clear cache + retry).

**Expected (fixed) outcome:** at most one cache clear per failed cycle, a
successful second connect, and **no** `BleakCharacteristicNotFoundError` in
normal operation.

## How to read the new coordinator log lines

These are the log lines the coordinator now emits. Match them against what you
see in your logs to understand what the integration is doing.

| Log line | Level | What it means |
|---|---|---|
| `Desk connection lost; will attempt to reconnect` | WARNING | The BLE link dropped unexpectedly (not a deliberate unload). The coordinator is starting its automatic recovery. |
| `Connected client for <addr> exposes N service(s): [...]` | DEBUG | A client connected. Lists how many GATT services it advertises and their UUIDs. `N` of 0, or a list missing the desk's service, points at the empty/partial-service race (Issue 2). |
| `Incomplete GATT services (attempt 1/2); clearing cache and retrying` | WARNING | The connected client's service set was empty or partial (the BlueZ cache race). The integration is clearing the stack cache and will reconnect once. |
| `Cleared bleak_retry_connector service cache for <addr> (cleared=...)` | WARNING | The stale service-cache entry was purged (`cleared=True`) or could not be (`cleared=False`); the bad client was discarded before the retry. |
| `Started notifications for <desk>` | DEBUG | GATT height notifications were (re)registered on the new controller. This confirms Issue 1 is resolved — live height updates will flow again. |
| `Reconnected to desk <desk>` | INFO | The full reconnect cycle (connect → validate → start → refresh) succeeded. The desk is usable again. |
| `Reconnect attempt for <desk> failed; will retry with backoff` | WARNING | One reconnect attempt failed (desk out of range, still broken, etc.). The loop will wait (5 s → 10 s → 20 s → 30 s, capped) and try again. |

Other useful lines you may see:

| Log line | Level | What it means |
|---|---|---|
| `Starting (re)connect cycle for <desk> (attempt N/2)` | DEBUG | A (re)connect cycle is beginning; `N` is 1 or 2 (2 only after a cache clear). |
| `No known desk service found on connected client for <addr>; services: [...]` | WARNING | The client connected but does not advertise any recognized desk service. |
| `Desk service <uuid> found for <addr> but missing required characteristic(s): [...]` | WARNING | The desk service is present but one or more required characteristics are missing. |
| `Could not read services from connected client for <addr>: ...` | WARNING | The service set could not be read at all (e.g., "Service Discovery has not been performed yet"). Treated as invalid → cache clear + retry. |
| `Height notify callback received height: <N> mm` | DEBUG | A live height notification was received and applied — the sensor is tracking the desk. |
| `Failed to refresh desk height after (re)connect` | WARNING | The connection succeeded but the post-connect height read failed; the connect is kept (best-effort refresh). |
| `Could not retrieve units from desk, defaulting to centimeters` | WARNING | The units read failed; the integration assumes centimeters. |

## How to enable debug logging

To see the debug-level lines above, enable debug logging for these loggers:

- `uplift_desk`
- `bleak.backends.bluezdbus.manager`
- `bleak.backends.bluezdbus.client`
- `bleak_retry_connector`

**Via the Home Assistant UI:**
1. Go to **Settings → Logs → Loggers** (or **Settings → System → Logs**).
2. Add each logger above and set its level to **Debug**.

**Via `configuration.yaml`** (then reload logging or restart):

```yaml
logger:
  default: info
  logs:
    uplift_desk: debug
    bleak.backends.bluezdbus.manager: debug
    bleak.backends.bluezdbus.client: debug
    bleak_retry_connector: debug
```

The `bleak.backends.bluezdbus.manager` logger is the one that emits
`Using cached services for ...`, which is the key signature for confirming the
Issue 2 race.

## Still not working?

**A stuck "retrying" entry.** If the desk is unreachable when Home Assistant
starts (or when you reload the integration), the config entry shows a
**"retrying"** state instead of a hard failure. This means Home Assistant is
automatically retrying setup on its own schedule. It will become active as
soon as the desk is reachable — make sure the desk is powered on, in range,
and not connected to the Uplift app or another device.

**A stuck "unavailable" desk at runtime.** If the sensor and buttons stay
*unavailable* for a long time, the background reconnect loop is still trying
on the 5 s → 10 s → 20 s → 30 s backoff. Check the logs for
`Reconnect attempt for … failed; will retry with backoff`. Common causes:

- The desk is out of range or the adapter is sleeping.
- Another device (the Uplift app, a second Home Assistant, a phone) has the
  BLE connection.
- The Bluetooth adapter needs to be re-enabled.

**The BlueZ race may need the adapter to re-advertise.** The empty/partial
service-set race (Issue 2) lives in the Bluetooth *stack's* cache. The
integration already clears that cache and reconnects once — that is the
correct, bounded recovery. But in some environments the adapter needs to
**re-advertise** the device before a fresh discovery succeeds. If you see the
cache-clear + retry happen and it still fails, try nudging the adapter:

- Toggle the adapter off and on (for example, via `bluetoothctl` → `power off`
  then `power on`), or
- Reload the config entry (Settings → Devices & Services → Uplift Desk →
  Reload).

After the adapter re-advertises, the next (re)connect cycle performs a fresh
service discovery and should succeed.

**When to collect logs.** If the desk still will not recover, capture the logs
for the four loggers above (with debug enabled) covering a drop and a few
reconnect attempts, and include them in any bug report. The most useful lines
are the ones listed in the tables above, plus `Using cached services for ...`
from the manager logger.
