"""Shed loads when the grid is lost and restore them once it is back."""

from __future__ import annotations

from collections.abc import Callable, Iterable
import logging

from homeassistant.const import ATTR_ENTITY_ID, STATE_ON
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from .const import DEFAULT_RESTORE_DELAY, DOMAIN, EVENT_RESTORED, EVENT_SHED, STORAGE_VERSION
from .grid import GridMonitor

_LOGGER = logging.getLogger(__name__)


class Shedder:
    """Turn off flagged loads on grid loss; turn the same ones back on later.

    ``loads`` maps a load id (the config subentry id) to the switch entity it
    controls. A load is shed only if its "shed on grid loss" flag is on and the
    switch is on at the moment the grid goes from on to off. The shed list is
    persisted so a Core restart during an outage still restores the loads.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str, monitor: GridMonitor) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.monitor = monitor
        self.loads: dict[str, str] = {}
        self.enabled: dict[str, bool] = {}
        self.restore_delay: float = DEFAULT_RESTORE_DELAY
        self.shed: list[str] = []
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")
        self._cancel_restore: CALLBACK_TYPE | None = None
        self._unsub_monitor: CALLBACK_TYPE | None = None
        self._listeners: list[Callable[[], None]] = []

    async def async_load(self) -> None:
        """Load the persisted shed list."""
        data = await self._store.async_load() or {}
        self.shed = list(data.get("shed", []))

    @callback
    def async_start(self) -> None:
        """Follow the grid monitor; resume a restore pending from before a restart."""
        self._unsub_monitor = self.monitor.async_add_listener(self._async_grid_changed)
        if self.shed and self.monitor.is_on:
            self._schedule_restore()

    @callback
    def async_stop(self) -> None:
        if self._unsub_monitor:
            self._unsub_monitor()
            self._unsub_monitor = None
        self._cancel_restore_timer()

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Called whenever the shed list changes."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def set_restore_delay(self, seconds: float) -> None:
        self.restore_delay = seconds

    @callback
    def _async_grid_changed(self, old: bool | None, new: bool | None) -> None:
        if old == new:
            return  # only the deciding source changed
        if new is True:
            if self.shed:
                self._schedule_restore()
            return
        self._cancel_restore_timer()
        if old is True and new is False:
            self.hass.async_create_task(self.async_shed())

    async def async_shed(self) -> None:
        """Turn off every flagged load that is currently on."""
        targets = [
            entity_id
            for load_id, entity_id in self.loads.items()
            if self.enabled.get(load_id, True) and self._is_on(entity_id)
        ]
        if not targets:
            return
        _LOGGER.info("Grid lost: shedding %s", targets)
        self._set_shed(self.shed + [e for e in targets if e not in self.shed])
        self.hass.bus.async_fire(EVENT_SHED, {ATTR_ENTITY_ID: targets})
        await self.hass.services.async_call(
            "switch", "turn_off", {ATTR_ENTITY_ID: targets}, blocking=True
        )

    async def async_restore(self) -> None:
        """Turn the shed loads back on and clear the list."""
        self._cancel_restore_timer()
        targets = [e for e in self.shed if self.hass.states.get(e) is not None]
        self._set_shed([])
        if not targets:
            return
        _LOGGER.info("Grid back: restoring %s", targets)
        self.hass.bus.async_fire(EVENT_RESTORED, {ATTR_ENTITY_ID: targets})
        await self.hass.services.async_call(
            "switch", "turn_on", {ATTR_ENTITY_ID: targets}, blocking=True
        )

    @callback
    def async_forget(self, entity_ids: Iterable[str]) -> None:
        """Drop loads from the shed list so they are not turned back on."""
        drop = set(entity_ids)
        if any(e in drop for e in self.shed):
            self._set_shed([e for e in self.shed if e not in drop])

    @callback
    def _schedule_restore(self) -> None:
        self._cancel_restore_timer()
        self._cancel_restore = async_call_later(
            self.hass, self.restore_delay, self._async_restore_timer
        )

    async def _async_restore_timer(self, _now) -> None:
        self._cancel_restore = None
        if self.monitor.is_on:
            await self.async_restore()

    @callback
    def _cancel_restore_timer(self) -> None:
        if self._cancel_restore is not None:
            self._cancel_restore()
            self._cancel_restore = None

    def _is_on(self, entity_id: str) -> bool:
        state = self.hass.states.get(entity_id)
        return state is not None and state.state == STATE_ON

    async def async_flush(self) -> None:
        """Write the shed list now (before a reload/unload reads it back)."""
        await self._store.async_save(self._data())

    def _data(self) -> dict:
        return {"shed": self.shed}

    @callback
    def _set_shed(self, shed: list[str]) -> None:
        self.shed = shed
        # Delayed write also gets flushed by Store on Home Assistant shutdown.
        self._store.async_delay_save(self._data, 1)
        for listener in list(self._listeners):
            listener()
