"""Button to turn shed loads back on immediately."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GridLoadSheddingConfigEntry
from .entity import GridEntity
from .shedder import Shedder


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GridLoadSheddingConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([RestoreNowButton(entry, entry.runtime_data.shedder)])


class RestoreNowButton(GridEntity, ButtonEntity):
    """Turn every shed load back on now and clear the list."""

    _attr_icon = "mdi:power-plug"

    def __init__(self, entry: GridLoadSheddingConfigEntry, shedder: Shedder) -> None:
        super().__init__(entry, "restore_now")
        self._shedder = shedder

    async def async_press(self) -> None:
        await self._shedder.async_restore()
