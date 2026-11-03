"""Constants for Grid Load Shedding."""

from typing import Final

DOMAIN: Final = "grid_load_shedding"

# Config entry options
CONF_VOLTAGE_ENTITY: Final = "voltage_entity"
CONF_THRESHOLD: Final = "threshold"
CONF_HOLD_SECONDS: Final = "hold_seconds"
CONF_FALLBACK_ENTITY: Final = "fallback_entity"

CONF_BATTERY_ENTITY: Final = "battery_entity"  # any sensor with a charge in percent, optional
CONF_BATTERY_RESERVE: Final = "battery_reserve"  # percent the battery can't actually give

DEFAULT_THRESHOLD: Final = 170.0
DEFAULT_HOLD_SECONDS: Final = 30
DEFAULT_RESTORE_DELAY: Final = 60
DEFAULT_BATTERY_RESERVE: Final = 0

# Load subentries
SUBENTRY_LOAD: Final = "load"
CONF_SWITCH_ENTITY: Final = "switch_entity"
CONF_WINDOW_START: Final = "window_start"  # "HH:MM:SS", optional
CONF_WINDOW_END: Final = "window_end"
CONF_SHED_ON_GRID_LOSS: Final = "shed_on_grid_loss"  # bool, default True
CONF_RUN_ON_SCHEDULE: Final = "run_on_schedule"  # bool, default True; only used with a window

# Sound notification subentries
SUBENTRY_ANNOUNCEMENT: Final = "announcement"
CONF_MEDIA_PLAYER: Final = "media_player"  # entity registry id (or entity_id if unregistered)
CONF_SOUND_LOST: Final = "sound_lost"  # {"media_content_id", "media_content_type"}, optional
CONF_SOUND_RESTORED: Final = "sound_restored"
CONF_VOLUME: Final = "volume"  # percent
CONF_BATTERY_VOLUME: Final = "battery_volume"  # percent, for the battery sounds
CONF_ENABLED: Final = "enabled"  # bool, default True
DEFAULT_VOLUME: Final = 50
DEFAULT_ANNOUNCE_WINDOW: Final = ("11:00:00", "22:00:00")

# Battery levels; each has an optional sound under the key f"sound_{level}"
SOC_LEVELS: Final = {"soc_25": 25, "soc_50": 50}  # below this percent
RUNTIME_LEVELS: Final = {"runtime_10": 10, "runtime_30": 30, "runtime_60": 60, "runtime_120": 120}  # minutes left
BATTERY_LEVELS: Final = (*RUNTIME_LEVELS, *SOC_LEVELS)  # most urgent first

# Bus events fired for other automations (Telegram etc.)
EVENT_SHED: Final = f"{DOMAIN}_shed"
EVENT_RESTORED: Final = f"{DOMAIN}_restored"

SERVICE_FORGET: Final = "forget"
SERVICE_RESTORE_NOW: Final = "restore_now"

STORAGE_VERSION: Final = 1
