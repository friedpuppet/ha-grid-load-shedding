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

from .announcer import EVENT_LOST, EVENT_RESTORED, Announcement, Announcer
from .const import (
    CONF_ENABLED,
    CONF_FALLBACK_ENTITY,
    CONF_MEDIA_PLAYER,
    CONF_SOUND_LOST,
    CONF_SOUND_RESTORED,
    CONF_VOLUME,
    DEFAULT_VOLUME,
    SUBENTRY_ANNOUNCEMENT,
    CONF_HOLD_SECONDS,
    CONF_RUN_ON_SCHEDULE,
    CONF_SHED_ON_GRID_LOSS,
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
    announcer: Announcer
    signature: tuple  # config that needs a reload when it changes; see _signature()


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

    announcer = Announcer(hass, monitor)

    # Loads and players are stored by entity registry id so they survive entity_id renames.
    ent_reg = er.async_get(hass)
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type == SUBENTRY_LOAD:
            entity_id = er.async_resolve_entity_id(ent_reg, subentry.data[CONF_SWITCH_ENTITY])
            if entity_id is None:
                continue  # the switch was deleted; its subentry stays until the user removes it
            shedder.loads[subentry_id] = Load(subentry_id, entity_id, _window(subentry.data))
        elif subentry.subentry_type == SUBENTRY_ANNOUNCEMENT:
            entity_id = er.async_resolve_entity_id(ent_reg, subentry.data[CONF_MEDIA_PLAYER])
            if entity_id is None:
                continue
            sounds = {
                event: subentry.data[key]
                for event, key in ((EVENT_LOST, CONF_SOUND_LOST), (EVENT_RESTORED, CONF_SOUND_RESTORED))
                if subentry.data.get(key)
            }
            volume = subentry.data.get(CONF_VOLUME, DEFAULT_VOLUME) / 100
            announcer.announcements[subentry_id] = Announcement(
                subentry_id, entity_id, sounds, volume, _window(subentry.data)
            )
    _apply_flags(entry, shedder, announcer, startup=True)

    entry.runtime_data = RuntimeData(monitor, shedder, announcer, _signature(entry))

    # Entities restore their state (grid, flags, delay) before the logic starts acting.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    monitor.async_start()
    shedder.async_start()
    announcer.async_start()
    entry.async_on_unload(monitor.async_stop)
    entry.async_on_unload(shedder.async_stop)
    entry.async_on_unload(announcer.async_stop)
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GridLoadSheddingConfigEntry) -> bool:
    await entry.runtime_data.shedder.async_flush()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


def _window(data) -> tuple | None:
    start, end = data.get(CONF_WINDOW_START), data.get(CONF_WINDOW_END)
    return (dt_util.parse_time(start), dt_util.parse_time(end)) if start and end else None


_FLAGS = (CONF_SHED_ON_GRID_LOSS, CONF_RUN_ON_SCHEDULE, CONF_ENABLED)


def _signature(entry: GridLoadSheddingConfigEntry) -> tuple:
    """Everything except the on/off flags; a change here needs a reload."""
    return (
        tuple(sorted(entry.options.items())),
        tuple(
            sorted(
                (sid, repr(sorted((k, v) for k, v in sub.data.items() if k not in _FLAGS)))
                for sid, sub in entry.subentries.items()
            )
        ),
    )


def _apply_flags(
    entry: GridLoadSheddingConfigEntry, shedder: Shedder, announcer: Announcer, startup: bool = False
) -> None:
    """Copy the on/off flags from subentry data (the source of truth) into the logic."""
    for announcement_id in announcer.announcements:
        announcer.enabled[announcement_id] = entry.subentries[announcement_id].data.get(CONF_ENABLED, True)
    for load_id in shedder.loads:
        data = entry.subentries[load_id].data
        shedder.enabled[load_id] = data.get(CONF_SHED_ON_GRID_LOSS, True)
        scheduled = data.get(CONF_RUN_ON_SCHEDULE, True)
        if startup:
            shedder.scheduled[load_id] = scheduled
        else:
            shedder.async_set_scheduled(load_id, scheduled)  # may start the load inside its window


async def _async_entry_updated(hass: HomeAssistant, entry: GridLoadSheddingConfigEntry) -> None:
    """Options or loads changed: apply flag-only changes live, reload for anything else."""
    data = entry.runtime_data
    if _signature(entry) == data.signature:
        _apply_flags(entry, data.shedder, data.announcer)
        data.shedder.async_notify()
        return
    await hass.config_entries.async_reload(entry.entry_id)
