"""Per-load switches: "Shed on grid loss" for every load, "Run on schedule" for loads with a window."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_OFF, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device import async_entity_id_to_device
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import GridLoadSheddingConfigEntry
from .shedder import Load, Shedder


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GridLoadSheddingConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    shedder = entry.runtime_data.shedder
    for load in shedder.loads.values():
        title = entry.subentries[load.id].title
        entities: list[LoadSwitch] = [ShedFlagSwitch(hass, shedder, load, title)]
        if load.window is not None:
            entities.append(ScheduleSwitch(hass, shedder, load, title))
        async_add_entities(entities, config_subentry_id=load.id)


class LoadSwitch(SwitchEntity, RestoreEntity):
    """A per-load setting switch (default on), shown on the load's own device.

    Linked like core helpers linked to a source device; if the load has no
    device, the name includes the load title instead.
    """

    _attr_has_entity_name = True
    _key: str

    def __init__(self, hass: HomeAssistant, shedder: Shedder, load: Load, title: str) -> None:
        self._shedder = shedder
        self._load = load
        self._attr_unique_id = f"{load.id}_{self._key}"
        self._attr_translation_key = self._key
        self.device_entry = async_entity_id_to_device(hass, load.entity_id)
        if self.device_entry is None:
            self._attr_translation_key = f"{self._key}_named"
            self._attr_translation_placeholders = {"load": title}

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        self._apply(last is None or last.state != STATE_OFF)

    async def async_turn_on(self, **kwargs) -> None:
        self._apply(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._apply(False)
        self.async_write_ha_state()

    def _apply(self, value: bool) -> None:
        raise NotImplementedError


class ShedFlagSwitch(LoadSwitch):
    """Whether this load is turned off when the grid is lost."""

    _key = "shed_on_grid_loss"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:power-plug-off-outline"

    @property
    def is_on(self) -> bool:
        return self._shedder.enabled.get(self._load.id, True)

    def _apply(self, value: bool) -> None:
        self._shedder.enabled[self._load.id] = value


class ScheduleSwitch(LoadSwitch):
    """Whether the load's schedule window is active (e.g. "boiler in use")."""

    _key = "run_on_schedule"
    _attr_icon = "mdi:calendar-clock"

    @property
    def is_on(self) -> bool:
        return self._shedder.scheduled.get(self._load.id, True)

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        # Restoring must not start the load, so set the flag directly.
        self._shedder.scheduled[self._load.id] = last is None or last.state != STATE_OFF

    def _apply(self, value: bool) -> None:
        self._shedder.async_set_scheduled(self._load.id, value)

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        start, end = self._load.window
        return {"window_start": start.isoformat(), "window_end": end.isoformat()}
