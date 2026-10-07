"""Delay before shed loads are turned back on."""

from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GridLoadSheddingConfigEntry
from .const import DEFAULT_RESTORE_DELAY
from .entity import GridEntity
from .shedder import Shedder


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GridLoadSheddingConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([RestoreDelayNumber(entry, entry.runtime_data.shedder)])


class RestoreDelayNumber(GridEntity, RestoreNumber):
    """Grid must stay on this long before shed loads are restored."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = 3600
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_icon = "mdi:timer-outline"

    def __init__(self, entry: GridLoadSheddingConfigEntry, shedder: Shedder) -> None:
        super().__init__(entry, "restore_delay")
        self._shedder = shedder

    @property
    def native_value(self) -> float:
        return self._shedder.restore_delay

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._shedder.set_restore_delay(last.native_value)
        else:
            self._shedder.set_restore_delay(DEFAULT_RESTORE_DELAY)

    async def async_set_native_value(self, value: float) -> None:
        self._shedder.set_restore_delay(value)
        self.async_write_ha_state()
