"""Constants for Grid Load Shedding."""

from typing import Final

DOMAIN: Final = "grid_load_shedding"

# Config entry options
CONF_VOLTAGE_ENTITY: Final = "voltage_entity"
CONF_THRESHOLD: Final = "threshold"
CONF_HOLD_SECONDS: Final = "hold_seconds"
CONF_FALLBACK_ENTITY: Final = "fallback_entity"

DEFAULT_THRESHOLD: Final = 170.0
DEFAULT_HOLD_SECONDS: Final = 30
DEFAULT_RESTORE_DELAY: Final = 60

# Load subentries
SUBENTRY_LOAD: Final = "load"
CONF_SWITCH_ENTITY: Final = "switch_entity"
CONF_WINDOW_START: Final = "window_start"  # "HH:MM:SS", optional
CONF_WINDOW_END: Final = "window_end"

# Bus events fired for other automations (Telegram etc.)
EVENT_SHED: Final = f"{DOMAIN}_shed"
EVENT_RESTORED: Final = f"{DOMAIN}_restored"

SERVICE_FORGET: Final = "forget"
SERVICE_RESTORE_NOW: Final = "restore_now"

STORAGE_VERSION: Final = 1
