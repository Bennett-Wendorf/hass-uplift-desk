"""Constants for the Uplift Desk integration."""

DOMAIN = "uplift_desk"
BLEAK_TIMEOUT_SECONDS = 15

CONF_FALLBACK_UNIT = "fallback_unit"
FALLBACK_UNIT_NONE = "none"
CONF_QUERY_ON_CONNECT = "query_on_connect"

DEFAULT_HEIGHT_LIMIT_MIN_MM: int = 500
DEFAULT_HEIGHT_LIMIT_MAX_MM: int = 1300

# Slots 3 and 4 are only offered on desks that support extended presets, and
# their entities start disabled, like the Move to Preset 3/4 buttons.
BASE_PRESET_SLOTS: tuple[int, ...] = (1, 2)
EXTENDED_PRESET_SLOTS: tuple[int, ...] = (3, 4)
# Saving stores wherever the desk is right now, so refuse to save until the
# desk has reported no height change (and received no recall) for this long.
PRESET_SAVE_SETTLE_SECONDS: float = 2.0
# Slack around the desk's height limits when decoding a reported preset.
PRESET_HEIGHT_LIMIT_TOLERANCE_MM: float = 5.0
# Pause after a save before asking the desk to re-report its presets.
PRESET_REPORT_DELAY_SECONDS: float = 1.0

# The desk reports height in tenths of its display unit: 1 mm steps in cm
# mode, 2.54 mm steps in inch mode. The desk may also stop 1-2 mm short of
# the commanded height, so in the worst case (inch mode) the reported arrival
# position is ~3.27 mm from the commanded value (2 mm short + half of a 2.54
# mm quantum). A 4 mm tolerance covers that case; the cost is the setpoint
# may clear up to 4 mm before the physical stop, which is cosmetically fine.
HEIGHT_SETPOINT_ARRIVAL_TOLERANCE_MM: float = 4.0
# 1 mm is the smallest cm-mode quantum: a single-quantum backward blip is
# indistinguishable from encoder noise, while a real manual interruption
# produces multi-quantum movement (inch-mode quanta of 2.54 mm always exceed
# this epsilon). Note the mode asymmetry: in inch mode a single-quantum
# (2.54 mm) backward blip exceeds this epsilon and IS treated as an
# interruption; raise this value if field data shows inch-mode desks emit
# single-quantum encoder blips.
HEIGHT_SETPOINT_INTERRUPTION_EPSILON_MM: float = 1.0
