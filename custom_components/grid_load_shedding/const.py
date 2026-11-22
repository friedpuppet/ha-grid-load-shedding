"""Constants for Grid Load Shedding."""

from typing import Final

DOMAIN: Final = "grid_load_shedding"

# Config entry options
CONF_VOLTAGE_ENTITY: Final = "voltage_entity"
CONF_THRESHOLD: Final = "threshold"
CONF_HOLD_SECONDS: Final = "hold_seconds"
CONF_FALLBACK_ENTITY: Final = "fallback_entity"
CONF_BYPASS_ENTITY: Final = "bypass_entity"  # binary_sensor, on while the inverter is on grid (e.g. its grid relay)

# "City grid" sensor: voltage at the supply, whether or not the inverter accepts it (informational)
CONF_METER_VOLTAGE_ENTITY: Final = "meter_voltage_entity"  # e.g. a Zigbee meter before the inverter, optional
CONF_METER_FRESH_SECONDS: Final = "meter_fresh_seconds"
CONF_CITY_THRESHOLD: Final = "city_threshold"

DEFAULT_THRESHOLD: Final = 170.0
DEFAULT_HOLD_SECONDS: Final = 30
DEFAULT_RESTORE_DELAY: Final = 60
DEFAULT_METER_FRESH_SECONDS: Final = 60
DEFAULT_CITY_THRESHOLD: Final = 100.0

# Load subentries
SUBENTRY_LOAD: Final = "load"
CONF_SWITCH_ENTITY: Final = "switch_entity"
CONF_WINDOW_START: Final = "window_start"  # "HH:MM:SS", optional
CONF_WINDOW_END: Final = "window_end"
CONF_SHED_ON_GRID_LOSS: Final = "shed_on_grid_loss"  # bool, default True
CONF_RUN_ON_SCHEDULE: Final = "run_on_schedule"  # bool, default True; only used with a window

# Removed in 1.2 (moved to battery_monitor / power_sounds); the migration drops them
OLD_BATTERY_OPTIONS: Final = (
    "battery_entity",
    "battery_reserve",
    "battery_reserve_entity",
    "battery_floor_entity",
    "battery_power_entity",
    "battery_capacity",
)
OLD_SUBENTRY_ANNOUNCEMENT: Final = "announcement"
OLD_BATTERY_SENSORS: Final = ("battery_runtime", "battery_session", "battery_state", "battery_time_to_full")

# Bus events fired for other automations (Telegram etc.)
EVENT_SHED: Final = f"{DOMAIN}_shed"
EVENT_RESTORED: Final = f"{DOMAIN}_restored"

SERVICE_FORGET: Final = "forget"
SERVICE_RESTORE_NOW: Final = "restore_now"

STORAGE_VERSION: Final = 1
