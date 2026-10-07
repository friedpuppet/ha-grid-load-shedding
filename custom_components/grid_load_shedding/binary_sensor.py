"""Grid presence binary sensor."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import GridLoadSheddingConfigEntry
from .entity import GridEntity
from .grid import GridMonitor


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GridLoadSheddingConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([GridBinarySensor(entry, entry.runtime_data.monitor)])


class GridBinarySensor(GridEntity, BinarySensorEntity, RestoreEntity):
    """On while utility-grid power is present."""

    _attr_device_class = BinarySensorDeviceClass.POWER

    def __init__(self, entry: GridLoadSheddingConfigEntry, monitor: GridMonitor) -> None:
        super().__init__(entry, "grid")
        self._monitor = monitor

    @property
    def is_on(self) -> bool | None:
        return self._monitor.is_on

    @property
    def available(self) -> bool:
        return self._monitor.is_on is not None

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        return {"source": self._monitor.source}

    async def async_added_to_hass(self) -> None:
        if (last := await self.async_get_last_state()) is not None:
            self._monitor.async_restore(
                {STATE_ON: True, STATE_OFF: False}.get(last.state)
            )
        self.async_on_remove(self._monitor.async_add_listener(self._on_change))

    @callback
    def _on_change(self, old: bool | None, new: bool | None) -> None:
        self.async_write_ha_state()
