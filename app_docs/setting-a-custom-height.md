# Setting a Custom Height

You can now move your desk to any height you want — not just the four
presets. The **Height Setpoint** number entity is the way to do it: open it,
type a height, and confirm. The desk moves to that height.

## What the Height Setpoint shows

The **Height Setpoint** entity shows the desk's **current height** — the same
live value the height sensor tracks. It is not a target display: after you
confirm a new value, the number follows the desk as it moves until it arrives
at the target.

## Setting a height

1. Open the **Height Setpoint** entity for your desk.
2. Type the height you want and confirm.
3. The desk moves to that height, and the value tracks the desk as it moves.

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

See [Disconnect & Reconnect Behavior](disconnect-reconnect-behavior.md) for
what happens when the Bluetooth link drops, and
[Troubleshooting](troubleshooting.md) if the desk does not respond as
expected.
