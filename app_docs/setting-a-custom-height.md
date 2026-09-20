# Setting a Custom Height

You can now move your desk to any height you want — not just the four
presets. The **Height Setpoint** number entity is the way to do it: open it,
type a height, and confirm. The desk moves to that height.

## What the Height Setpoint shows

The **Height Setpoint** entity is a **target display**: it shows the height
you commanded, not the desk's live position.

- While the desk is **at rest** (no move in progress), the entity shows
  **unknown**.
- While the desk is **moving toward the target**, the entity keeps showing
  the height you typed.
- The entity returns to **unknown** when:
  - the desk **arrives** at the target (within a few millimeters),
  - the move is **interrupted** — a keypad press, a preset button, or any
    other control takes over,
  - the desk **disconnects**, or
  - Home Assistant **restarts**.

The desk's firmware does not report a target height, so the setpoint is
tracked by the integration and is **not persisted**: after a restart or a
reconnect, the entity is **unknown** again. The desk's **live position** is
reported by the separate **Height** sensor — use it to watch the desk
approach the target while the setpoint holds the height you typed.

## Setting a height

1. Open the **Height Setpoint** entity for your desk.
2. Type the height you want and confirm.
3. The desk moves to that height. While it moves, the setpoint shows the
   target you typed and the **Height** sensor tracks the desk's live
   position. When the desk arrives, the setpoint returns to **unknown**.

The value box is a **type-and-confirm box, not a slider, on purpose.** A
slider could be bumped accidentally and physically move the desk; a box
requires you to type a value and confirm it, so an accidental touch does
nothing.

While the desk is disconnected, the entity shows as **unavailable**, just
like the height sensor and preset buttons — and it comes back on its own once
the desk reconnects.

## Allowed range and units

- The allowed range (minimum and maximum) follows the **desk's configured
  height limits**. The desk reports its limits shortly after it connects, so
  the real range is normally known within moments of a connection.
- Until the desk reports its limits, a fallback range of **500–1300 mm** is
  shown.
- Values are shown and entered in **your locale's length unit** (for example
  centimeters or inches) and converted automatically — the desk works in
  millimeters.

## If a set is rejected

Two situations produce a clear error instead of a move:

- **A move command is already in flight.** While a previous move command is
  still being sent to the desk — roughly the first 1–2 seconds after you
  confirm, or longer if the desk is in the middle of reconnecting — a second
  set is **rejected** with a clear error saying a move command is in flight
  (or the desk is reconnecting). The desk keeps moving to the first target.
  This is deliberate: move commands are not queued. If you want a different
  height, set it a few seconds later; the desk retargets while it is still
  moving, which works as intended.
- **The desk is locked.** If the desk last reported itself as locked, setting
  a value reports a clear "desk is locked" error instead of moving. Unlock
  the desk (for example from its keypad) and try again.

## Known limitation

If the desk clamps a command to a position different from the one you
commanded — for example, a value set while the 500–1300 mm fallback range is
still in effect, before the desk reports its real limits — the setpoint can
remain visible until the next set.

See [Disconnect & Reconnect Behavior](disconnect-reconnect-behavior.md) for
what happens when the Bluetooth link drops, and
[Troubleshooting](troubleshooting.md) if the desk does not respond as
expected.
