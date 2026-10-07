"""Button to turn shed loads back on immediately; test buttons for sound notifications."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device import async_entity_id_to_device
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GridLoadSheddingConfigEntry
from .announcer import EVENT_LOST, EVENT_RESTORED, Announcement, Announcer
from .entity import GridEntity
from .shedder import Shedder


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GridLoadSheddingConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([RestoreNowButton(entry, entry.runtime_data.shedder)])
    announcer = entry.runtime_data.announcer
    for announcement in announcer.announcements.values():
        async_add_entities(
            [
                TestSoundButton(hass, entry, announcer, announcement, event)
                for event in (EVENT_LOST, EVENT_RESTORED)
            ],
            config_subentry_id=announcement.id,
        )


class RestoreNowButton(GridEntity, ButtonEntity):
    """Turn every shed load back on now and clear the list."""

    _attr_icon = "mdi:power-plug"

    def __init__(self, entry: GridLoadSheddingConfigEntry, shedder: Shedder) -> None:
        super().__init__(entry, "restore_now")
        self._shedder = shedder

    async def async_press(self) -> None:
        await self._shedder.async_restore()


class TestSoundButton(ButtonEntity):
    """Play a notification sound now, ignoring the window and the on/off switch."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:play-circle-outline"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: GridLoadSheddingConfigEntry,
        announcer: Announcer,
        announcement: Announcement,
        event: str,
    ) -> None:
        self._announcer = announcer
        self._announcement_id = announcement.id
        self._event = event
        key = f"test_sound_{event}"
        self._attr_unique_id = f"{announcement.id}_{key}"
        self._attr_translation_key = key
        self.device_entry = async_entity_id_to_device(hass, announcement.entity_id)
        if self.device_entry is None:
            self._attr_translation_key = f"{key}_named"
            self._attr_translation_placeholders = {"player": entry.subentries[announcement.id].title}

    async def async_press(self) -> None:
        await self._announcer.async_test(self._announcement_id, self._event)
