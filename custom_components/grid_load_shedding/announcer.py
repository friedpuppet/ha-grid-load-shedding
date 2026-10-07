"""Play a sound on a media player when the grid is lost or comes back."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
import logging

from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_VOLUME_LEVEL,
    DOMAIN as MEDIA_PLAYER_DOMAIN,
    SERVICE_PLAY_MEDIA,
)
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_VOLUME_SET
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .grid import GridMonitor
from .shedder import in_window

_LOGGER = logging.getLogger(__name__)

EVENT_LOST = "lost"
EVENT_RESTORED = "restored"


@dataclass(frozen=True)
class Announcement:
    """A media player that announces grid changes (one per announcement subentry)."""

    id: str
    entity_id: str
    sounds: dict[str, dict[str, str]]  # EVENT_LOST/EVENT_RESTORED -> media content id/type
    volume: float  # 0..1
    window: tuple[time, time] | None = None  # local start, end; may cross midnight


class Announcer:
    """Announce grid loss (on -> off) and return (off -> on), only inside each window."""

    def __init__(self, hass: HomeAssistant, monitor: GridMonitor) -> None:
        self.hass = hass
        self.monitor = monitor
        self.announcements: dict[str, Announcement] = {}
        self.enabled: dict[str, bool] = {}  # announcement id -> "Sound notifications"
        self._unsub: CALLBACK_TYPE | None = None

    @callback
    def async_start(self) -> None:
        self._unsub = self.monitor.async_add_listener(self._async_grid_changed)

    @callback
    def async_stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    @callback
    def _async_grid_changed(self, old: bool | None, new: bool | None) -> None:
        if old is True and new is False:
            event = EVENT_LOST
        elif old is False and new is True:
            event = EVENT_RESTORED
        else:
            return  # startup, an unknown state or only the deciding source changed
        now = dt_util.now().time()
        for announcement in self.announcements.values():
            if not self.enabled.get(announcement.id, True):
                continue
            if announcement.window is not None and not in_window(now, announcement.window):
                _LOGGER.debug("Grid %s: %s is outside its window", event, announcement.entity_id)
                continue
            self.hass.async_create_task(self._async_play(announcement, event))

    async def async_test(self, announcement_id: str, event: str) -> None:
        """Play the sound for ``event`` now, ignoring the window and the switch."""
        await self._async_play(self.announcements[announcement_id], event)

    async def _async_play(self, announcement: Announcement, event: str) -> None:
        sound = announcement.sounds.get(event)
        if sound is None:
            return
        _LOGGER.info("Grid %s: playing %s on %s", event, sound[ATTR_MEDIA_CONTENT_ID], announcement.entity_id)
        target = {ATTR_ENTITY_ID: announcement.entity_id}
        try:
            await self.hass.services.async_call(
                MEDIA_PLAYER_DOMAIN,
                SERVICE_VOLUME_SET,
                {**target, ATTR_MEDIA_VOLUME_LEVEL: announcement.volume},
                blocking=True,
            )
            await self.hass.services.async_call(
                MEDIA_PLAYER_DOMAIN,
                SERVICE_PLAY_MEDIA,
                {
                    **target,
                    ATTR_MEDIA_CONTENT_ID: sound[ATTR_MEDIA_CONTENT_ID],
                    ATTR_MEDIA_CONTENT_TYPE: sound[ATTR_MEDIA_CONTENT_TYPE],
                },
                blocking=True,
            )
        except HomeAssistantError as err:
            _LOGGER.warning("Grid %s: can't play on %s: %s", event, announcement.entity_id, err)
