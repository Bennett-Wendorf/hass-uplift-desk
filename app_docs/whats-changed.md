# What changed (for users upgrading)

If you are upgrading the Uplift Desk integration, here is what improves for
you. No configuration changes are required — existing setups keep working as
they are.

- **Automatic reconnection.** The desk now reconnects by itself after a
  Bluetooth (BLE) drop. Previously, once the link fell the desk stayed "dead
  but connected" until you restarted Home Assistant or reloaded the
  integration. Recovery is now automatic, on a short, bounded wait (5 s → 10 s
  → 20 s → 30 s between attempts).

- **Honest availability.** The height sensor and preset buttons now correctly
  show as **unavailable** while the desk is disconnected, instead of freezing
  on a stale value while still claiming to be available. They come back
  automatically the moment the desk reconnects — no action needed.

- **Self-healing "connected but broken" state.** A connected client whose GATT
  service set is empty or partial (a known Bluetooth-stack race) is now
  detected early and recovered from automatically — the integration clears the
  stale stack cache and reconnects once — instead of failing deep inside a
  command with a confusing error.

- **Clean "retrying" state at startup.** If the desk is unreachable when Home
  Assistant starts (or when the integration is loaded/reloaded), the config
  entry now shows a **"retrying"** state rather than a hard, permanent failure.
  It activates automatically as soon as the desk is reachable.

- **Press-to-reconnect still works.** Preset buttons can still be pressed
  while the desk is disconnected; doing so triggers an on-demand reconnect and
  applies the preset once the desk is back.

For details on the reconnect timeline and how to read the new log lines, see
[Disconnect & Reconnect Behavior](disconnect-reconnect-behavior.md) and
[Troubleshooting](troubleshooting.md).
