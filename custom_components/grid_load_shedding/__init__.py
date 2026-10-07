"""Grid Load Shedding: detect utility-grid loss and shed/restore loads."""

from __future__ import annotations

from dataclasses import dataclass

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import (
    CONF_FALLBACK_ENTITY,
    CONF_HOLD_SECONDS,
    CONF_SWITCH_ENTITY,
    CONF_THRESHOLD,
    CONF_VOLTAGE_ENTITY,
    CONF_WINDOW_END,
    CONF_WINDOW_START,
    DEFAULT_HOLD_SECONDS,
    DEFAULT_THRESHOLD,
    DOMAIN,
    SERVICE_FORGET,
    SERVICE_RESTORE_NOW,
    SUBENTRY_LOAD,
)
from .grid import GridMonitor
from .shedder import Load, Shedder

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.NUMBER, Platform.SENSOR, Platform.SWITCH]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class RuntimeData:
    monitor: GridMonitor
    shedder: Shedder


type GridLoadSheddingConfigEntry = ConfigEntry[RuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register domain services (they act on every loaded entry)."""

    def _shedders() -> list[Shedder]:
        return [
            entry.runtime_data.shedder
            for entry in hass.config_entries.async_loaded_entries(DOMAIN)
        ]

    async def forget(call: ServiceCall) -> None:
        for shedder in _shedders():
            shedder.async_forget(call.data[ATTR_ENTITY_ID])

    async def restore_now(call: ServiceCall) -> None:
        for shedder in _shedders():
            await shedder.async_restore()

    hass.services.async_register(
        DOMAIN, SERVICE_FORGET, forget, schema=vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_ids})
    )
    hass.services.async_register(DOMAIN, SERVICE_RESTORE_NOW, restore_now, schema=vol.Schema({}))
    return True


async def async_setup_entry(hass: HomeAssistant, entry: GridLoadSheddingConfigEntry) -> bool:
    opts = entry.options
    monitor = GridMonitor(
        hass,
        voltage_entity=opts[CONF_VOLTAGE_ENTITY],
        threshold=opts.get(CONF_THRESHOLD, DEFAULT_THRESHOLD),
        hold_seconds=opts.get(CONF_HOLD_SECONDS, DEFAULT_HOLD_SECONDS),
        fallback_entity=opts.get(CONF_FALLBACK_ENTITY) or None,
    )
    shedder = Shedder(hass, entry.entry_id, monitor)
    await shedder.async_load()

    # Loads are stored by entity registry id so they survive entity_id renames.
    ent_reg = er.async_get(hass)
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_LOAD:
            continue
        entity_id = er.async_resolve_entity_id(ent_reg, subentry.data[CONF_SWITCH_ENTITY])
        if entity_id is None:
            continue  # the switch was deleted; its subentry stays until the user removes it
        start, end = subentry.data.get(CONF_WINDOW_START), subentry.data.get(CONF_WINDOW_END)
        window = (dt_util.parse_time(start), dt_util.parse_time(end)) if start and end else None
        shedder.loads[subentry_id] = Load(subentry_id, entity_id, window)

    entry.runtime_data = RuntimeData(monitor, shedder)

    # Entities restore their state (grid, flags, delay) before the logic starts acting.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    monitor.async_start()
    shedder.async_start()
    entry.async_on_unload(monitor.async_stop)
    entry.async_on_unload(shedder.async_stop)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GridLoadSheddingConfigEntry) -> bool:
    await entry.runtime_data.shedder.async_flush()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload(hass: HomeAssistant, entry: GridLoadSheddingConfigEntry) -> None:
    """Options changed or a load was added/removed."""
    await hass.config_entries.async_reload(entry.entry_id)
