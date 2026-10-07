"""Per-load switches: "Shed on grid loss" for every load, "Run on schedule" for loads with a window,
and "Sound notifications" for every sound notification.

The source of truth is the load's subentry data (also editable in the load's
Reconfigure dialog); these switches show and change it. A flag-only change is
applied live by the entry's update listener, without a reload.
"""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_OFF, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device import async_entity_id_to_device
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import GridLoadSheddingConfigEntry
from .announcer import Announcement, Announcer
from .const import CONF_ENABLED, CONF_RUN_ON_SCHEDULE, CONF_SHED_ON_GRID_LOSS
from .shedder import Load, Shedder


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GridLoadSheddingConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    shedder = entry.runtime_data.shedder
    for load in shedder.loads.values():
        entities: list[LoadSwitch] = [ShedFlagSwitch(hass, entry, shedder, load)]
        if load.window is not None:
            entities.append(ScheduleSwitch(hass, entry, shedder, load))
        async_add_entities(entities, config_subentry_id=load.id)
    announcer = entry.runtime_data.announcer
    for announcement in announcer.announcements.values():
        async_add_entities(
            [AnnouncementSwitch(hass, entry, shedder, announcer, announcement)],
            config_subentry_id=announcement.id,
        )


class LoadSwitch(SwitchEntity, RestoreEntity):
    """A per-load setting switch (default on), shown on the load's own device.

    Linked like core helpers linked to a source device; if the load has no
    device, the name includes the load title instead.
    """

    _attr_has_entity_name = True
    _key: str

    def __init__(
        self, hass: HomeAssistant, entry: GridLoadSheddingConfigEntry, shedder: Shedder, load: Load
    ) -> None:
        self._entry = entry
        self._shedder = shedder
        self._load = load
        self._attr_unique_id = f"{load.id}_{self._key}"
        self._attr_translation_key = self._key
        self.device_entry = async_entity_id_to_device(hass, load.entity_id)
        if self.device_entry is None:
            self._attr_translation_key = f"{self._key}_named"
            self._attr_translation_placeholders = {"load": entry.subentries[load.id].title}

    @property
    def _flags(self) -> dict[str, bool]:
        raise NotImplementedError

    @property
    def is_on(self) -> bool:
        return self._flags.get(self._load.id, True)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._shedder.async_add_listener(self._on_change))
        subentry = self._entry.subentries[self._load.id]
        if self._key not in subentry.data:
            # Migrate from v0.2.x, where the state lived only in this entity.
            last = await self.async_get_last_state()
            value = last is None or last.state != STATE_OFF
            self._flags[self._load.id] = value  # the update listener isn't registered yet
            self._write(value)

    async def async_turn_on(self, **kwargs) -> None:
        self._write(True)

    async def async_turn_off(self, **kwargs) -> None:
        self._write(False)

    def _write(self, value: bool) -> None:
        subentry = self._entry.subentries[self._load.id]
        self.hass.config_entries.async_update_subentry(
            self._entry, subentry, data={**subentry.data, self._key: value}
        )

    @callback
    def _on_change(self) -> None:
        self.async_write_ha_state()


class ShedFlagSwitch(LoadSwitch):
    """Whether this load is turned off when the grid is lost."""

    _key = CONF_SHED_ON_GRID_LOSS
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:power-plug-off-outline"

    @property
    def _flags(self) -> dict[str, bool]:
        return self._shedder.enabled


class ScheduleSwitch(LoadSwitch):
    """Whether the load's schedule window is active (e.g. "boiler in use")."""

    _key = CONF_RUN_ON_SCHEDULE
    _attr_icon = "mdi:calendar-clock"

    @property
    def _flags(self) -> dict[str, bool]:
        return self._shedder.scheduled

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        start, end = self._load.window
        return {"window_start": start.isoformat(), "window_end": end.isoformat()}


class AnnouncementSwitch(SwitchEntity):
    """Whether this media player announces grid loss and return (mirrors the subentry flag)."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:bullhorn-outline"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: GridLoadSheddingConfigEntry,
        shedder: Shedder,
        announcer: Announcer,
        announcement: Announcement,
    ) -> None:
        self._entry = entry
        self._shedder = shedder  # its listeners hear every live flag change
        self._announcer = announcer
        self._announcement = announcement
        self._attr_unique_id = f"{announcement.id}_{CONF_ENABLED}"
        self._attr_translation_key = "announce"
        self.device_entry = async_entity_id_to_device(hass, announcement.entity_id)
        if self.device_entry is None:
            self._attr_translation_key = "announce_named"
            self._attr_translation_placeholders = {"player": entry.subentries[announcement.id].title}

    @property
    def is_on(self) -> bool:
        return self._announcer.enabled.get(self._announcement.id, True)

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        if self._announcement.window is None:
            return None
        start, end = self._announcement.window
        return {"window_start": start.isoformat(), "window_end": end.isoformat()}

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._shedder.async_add_listener(self._on_change))

    async def async_turn_on(self, **kwargs) -> None:
        self._write(True)

    async def async_turn_off(self, **kwargs) -> None:
        self._write(False)

    def _write(self, value: bool) -> None:
        subentry = self._entry.subentries[self._announcement.id]
        self.hass.config_entries.async_update_subentry(
            self._entry, subentry, data={**subentry.data, CONF_ENABLED: value}
        )

    @callback
    def _on_change(self) -> None:
        self.async_write_ha_state()
