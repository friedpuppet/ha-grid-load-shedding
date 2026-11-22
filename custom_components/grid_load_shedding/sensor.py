"""Sensors: the loads turned off because of a grid outage, the battery's time left and session."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GridLoadSheddingConfigEntry
from .battery import BATTERY_STATES, BatteryMonitor
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
        if entry.runtime_data.battery.uses_power:
            entities.append(BatteryStateSensor(entry, entry.runtime_data.battery))
            entities.append(BatterySessionSensor(entry, entry.runtime_data.battery))
            entities.append(BatteryTimeToFullSensor(entry, entry.runtime_data.battery))
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


class _BatteryEntity(GridEntity, SensorEntity):
    def __init__(self, entry: GridLoadSheddingConfigEntry, battery: BatteryMonitor, key: str) -> None:
        super().__init__(entry, key)
        self._battery = battery

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._battery.async_add_listener(self._on_change))

    @callback
    def _on_change(self) -> None:
        self.async_write_ha_state()


class BatteryRuntimeSensor(_BatteryEntity):
    """Time left to the floor while discharging; unknown otherwise or until there is an estimate."""

    _attr_icon = "mdi:battery-clock-outline"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(self, entry: GridLoadSheddingConfigEntry, battery: BatteryMonitor) -> None:
        super().__init__(entry, battery, "battery_runtime")

    @property
    def native_value(self) -> int | None:
        runtime = self._battery.runtime_minutes
        return None if runtime is None else round(runtime)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        battery = self._battery
        rate = battery.median_rate
        attrs: dict[str, Any] = {
            "median_rate": None if rate is None else round(rate, 1),
            "passed": sorted(battery.passed),
            "floor": battery.floor,
        }
        if battery.uses_power:
            power, measured = battery.smoothed_power, battery.measured_capacity
            attrs |= {
                "method": battery.method,
                "smoothed_power": None if power is None else round(power),
                "capacity": battery.capacity,
                "measured_capacity": None if measured is None else round(measured, 1),
            }
        return attrs


class BatteryTimeToFullSensor(_BatteryEntity):
    """Time to 100 % while charging, at the smoothed power; unknown otherwise."""

    _attr_icon = "mdi:battery-charging-high"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(self, entry: GridLoadSheddingConfigEntry, battery: BatteryMonitor) -> None:
        super().__init__(entry, battery, "battery_time_to_full")

    @property
    def native_value(self) -> int | None:
        minutes = self._battery.time_to_full_minutes
        return None if minutes is None else round(minutes)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        power = self._battery.smoothed_power if self._battery.state == "charging" else None
        return {"smoothed_power": None if power is None else round(power)}


class BatteryStateSensor(_BatteryEntity):
    """Discharging, charging or idle, from the battery power with hysteresis."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(BATTERY_STATES)

    def __init__(self, entry: GridLoadSheddingConfigEntry, battery: BatteryMonitor) -> None:
        super().__init__(entry, battery, "battery_state")

    @property
    def native_value(self) -> str | None:
        return self._battery.state

    @property
    def icon(self) -> str:
        return {
            "discharging": "mdi:battery-arrow-down",
            "charging": "mdi:battery-charging",
            "idle": "mdi:battery-outline",
        }.get(self._battery.state or "", "mdi:battery-unknown")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        mean = self._battery.mean_power
        return {
            "since": self._battery.session_start.isoformat() if self._battery.session_start else None,
            "mean_power": None if mean is None else round(mean),
        }


class BatterySessionSensor(_BatteryEntity):
    """Start of the current session: the time the battery has been in its present state."""

    _attr_icon = "mdi:timer-play-outline"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry: GridLoadSheddingConfigEntry, battery: BatteryMonitor) -> None:
        super().__init__(entry, battery, "battery_session")

    @property
    def native_value(self) -> datetime | None:
        return self._battery.session_start

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        battery = self._battery
        return {
            "state": battery.state,
            "start_soc": battery._session_soc,
            "energy": round(battery.session_energy / 1000, 2) if battery.session_start else None,
            "previous_state": battery.previous.get("state"),
            "previous_start": battery.previous.get("start"),
            "previous_end": battery.previous.get("end"),
            "previous_energy": battery.previous.get("energy"),
        }
