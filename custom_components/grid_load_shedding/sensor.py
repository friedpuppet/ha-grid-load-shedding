"""Sensor listing the loads currently turned off because of a grid outage."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GridLoadSheddingConfigEntry
from .entity import GridEntity
from .shedder import Shedder


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GridLoadSheddingConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ShedLoadsSensor(entry, entry.runtime_data.shedder)])


class ShedLoadsSensor(GridEntity, SensorEntity):
    """Number of shed loads; their entity_ids are in the ``entities`` attribute."""

    _attr_icon = "mdi:power-plug-off"

    def __init__(self, entry: GridLoadSheddingConfigEntry, shedder: Shedder) -> None:
        super().__init__(entry, "shed_loads")
        self._shedder = shedder

    @property
    def native_value(self) -> int:
        return len(self._shedder.shed)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"entities": list(self._shedder.shed)}

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._shedder.async_add_listener(self._on_change))

    @callback
    def _on_change(self) -> None:
        self.async_write_ha_state()
