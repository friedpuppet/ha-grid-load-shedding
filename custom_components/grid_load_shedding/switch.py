"""Per-load "Shed on grid loss" switches, one per load subentry."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_OFF, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device import async_entity_id_to_device
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import GridLoadSheddingConfigEntry
from .shedder import Shedder


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GridLoadSheddingConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    shedder = entry.runtime_data.shedder
    for subentry_id, entity_id in shedder.loads.items():
        async_add_entities(
            [ShedFlagSwitch(hass, shedder, subentry_id, entity_id, entry.subentries[subentry_id].title)],
            config_subentry_id=subentry_id,
        )


class ShedFlagSwitch(SwitchEntity, RestoreEntity):
    """Whether this load is turned off when the grid is lost (default on).

    Lives on the load's own device (like core helpers linked to a source device),
    so it shows up next to the plug it controls.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "shed_on_grid_loss"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:power-plug-off-outline"

    def __init__(
        self, hass: HomeAssistant, shedder: Shedder, load_id: str, target: str, title: str
    ) -> None:
        self._shedder = shedder
        self._load_id = load_id
        self._attr_unique_id = f"{load_id}_shed"
        self.device_entry = async_entity_id_to_device(hass, target)
        if self.device_entry is None:
            # Target has no device: name the flag after the load instead.
            self._attr_translation_placeholders = {"load": title}
            self._attr_translation_key = "shed_on_grid_loss_named"

    @property
    def is_on(self) -> bool:
        return self._shedder.enabled.get(self._load_id, True)

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        self._shedder.enabled[self._load_id] = last is None or last.state != STATE_OFF

    async def async_turn_on(self, **kwargs) -> None:
        self._shedder.enabled[self._load_id] = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._shedder.enabled[self._load_id] = False
        self.async_write_ha_state()
