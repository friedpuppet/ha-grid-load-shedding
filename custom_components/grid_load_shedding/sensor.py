"""Sensors: the loads turned off because of a grid outage, and the battery's time left."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GridLoadSheddingConfigEntry
from .battery import BatteryMonitor
from .entity import GridEntity
from .shedder import Shedder


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GridLoadSheddingConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    entities: list[SensorEntity] = [ShedLoadsSensor(entry, entry.runtime_data.shedder)]
    if entry.runtime_data.battery:
        entities.append(BatteryRuntimeSensor(entry, entry.runtime_data.battery))
    async_add_entities(entities)


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


class BatteryRuntimeSensor(GridEntity, SensorEntity):
    """Time left at the median discharge rate; unknown until there is a rate."""

    _attr_icon = "mdi:battery-clock-outline"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(self, entry: GridLoadSheddingConfigEntry, battery: BatteryMonitor) -> None:
        super().__init__(entry, "battery_runtime")
        self._battery = battery

    @property
    def native_value(self) -> int | None:
        runtime = self._battery.runtime_minutes
        return None if runtime is None else round(runtime)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rate = self._battery.median_rate
        return {"median_rate": None if rate is None else round(rate, 1), "passed": sorted(self._battery.passed)}

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._battery.async_add_listener(self._on_change))

    @callback
    def _on_change(self) -> None:
        self.async_write_ha_state()
