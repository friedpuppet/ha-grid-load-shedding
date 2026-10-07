"""Shed loads when the grid is lost, restore them once it is back, run schedule windows."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, time
import logging

from homeassistant.const import ATTR_ENTITY_ID, STATE_ON
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DEFAULT_RESTORE_DELAY, DOMAIN, EVENT_RESTORED, EVENT_SHED, STORAGE_VERSION
from .grid import GridMonitor

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Load:
    """A switch managed by the integration (one per load subentry)."""

    id: str
    entity_id: str
    window: tuple[time, time] | None = None  # local start, end; may cross midnight


def in_window(now: time, window: tuple[time, time]) -> bool:
    start, end = window
    if start < end:
        return start <= now < end
    return now >= start or now < end  # crosses midnight


class Shedder:
    """Turn off flagged loads on grid loss; turn the same ones back on later.

    A load is shed only if its "shed on grid loss" flag is on and the switch is
    on at the moment the grid goes from on to off; only shed loads are restored.

    A load may have a schedule window, active while its "run on schedule" switch
    is on: turned on at the window start (if the grid is present, otherwise once
    the grid returns within the window), turned off at the window end, and never
    restored outside the window. Manual changes inside the window are respected.

    The shed list and missed window starts are persisted, so a Core restart
    during an outage still restores the loads.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str, monitor: GridMonitor) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.monitor = monitor
        self.loads: dict[str, Load] = {}
        self.enabled: dict[str, bool] = {}  # load id -> "shed on grid loss"
        self.scheduled: dict[str, bool] = {}  # load id -> "run on schedule"
        self.restore_delay: float = DEFAULT_RESTORE_DELAY
        self.shed: list[str] = []
        self.missed: list[str] = []  # entity_ids whose window started without grid
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")
        self._cancel_restore: CALLBACK_TYPE | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._listeners: list[Callable[[], None]] = []

    async def async_load(self) -> None:
        """Load the persisted state."""
        data = await self._store.async_load() or {}
        self.shed = list(data.get("shed", []))
        self.missed = list(data.get("missed", []))

    @callback
    def async_start(self) -> None:
        """Follow the grid and the windows; resume a restore pending from before a restart."""
        self._unsubs.append(self.monitor.async_add_listener(self._async_grid_changed))
        for load in self.loads.values():
            if load.window is None:
                continue
            start, end = load.window
            self._unsubs.append(
                async_track_time_change(
                    self.hass, self._window_job(load, True), start.hour, start.minute, start.second
                )
            )
            self._unsubs.append(
                async_track_time_change(
                    self.hass, self._window_job(load, False), end.hour, end.minute, end.second
                )
            )
        if (self.shed or self.missed) and self.monitor.is_on:
            self._schedule_restore()

    @callback
    def async_stop(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()
        self._cancel_restore_timer()

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Called whenever the shed list or the per-load flags change."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def set_restore_delay(self, seconds: float) -> None:
        self.restore_delay = seconds

    # --- grid -----------------------------------------------------------

    @callback
    def _async_grid_changed(self, old: bool | None, new: bool | None) -> None:
        if old == new:
            return  # only the deciding source changed
        if new is True:
            if self.shed or self.missed:
                self._schedule_restore()
            return
        self._cancel_restore_timer()
        if old is True and new is False:
            self.hass.async_create_task(self.async_shed())

    async def async_shed(self) -> None:
        """Turn off every flagged load that is currently on."""
        targets = [
            load.entity_id
            for load in self.loads.values()
            if self.enabled.get(load.id, True) and self._is_on(load.entity_id)
        ]
        if not targets:
            return
        _LOGGER.info("Grid lost: shedding %s", targets)
        self._set_state(shed=self.shed + [e for e in targets if e not in self.shed])
        self.hass.bus.async_fire(EVENT_SHED, {ATTR_ENTITY_ID: targets})
        await self._switch("turn_off", targets)

    async def async_restore(self) -> None:
        """Turn shed loads (and loads whose window start was missed) back on."""
        self._cancel_restore_timer()
        now = dt_util.now().time()
        targets: list[str] = []
        for entity_id in self.shed:
            load = self._load_for(entity_id)
            if load is not None and self._windowed(load) and not in_window(now, load.window):
                continue  # its window ended during the outage
            if self.hass.states.get(entity_id) is not None:
                targets.append(entity_id)
        for entity_id in self.missed:
            load = self._load_for(entity_id)
            if load is not None and self._windowed(load) and in_window(now, load.window) and entity_id not in targets:
                targets.append(entity_id)
        self._set_state(shed=[], missed=[])
        if not targets:
            return
        _LOGGER.info("Grid back: restoring %s", targets)
        self.hass.bus.async_fire(EVENT_RESTORED, {ATTR_ENTITY_ID: targets})
        await self._switch("turn_on", targets)

    @callback
    def async_forget(self, entity_ids: Iterable[str]) -> None:
        """Drop loads from the shed list so they are not turned back on."""
        drop = set(entity_ids)
        if any(e in drop for e in self.shed + self.missed):
            self._set_state(
                shed=[e for e in self.shed if e not in drop],
                missed=[e for e in self.missed if e not in drop],
            )

    # --- schedule windows ----------------------------------------------

    def _window_job(self, load: Load, start: bool) -> Callable[[datetime], None]:
        @callback
        def _job(_now: datetime) -> None:
            if not self.scheduled.get(load.id, True):
                return
            if start:
                self._async_window_start(load)
            else:
                self._async_window_end(load)

        return _job

    @callback
    def _async_window_start(self, load: Load) -> None:
        if self.monitor.is_on:
            _LOGGER.info("Window start: turning on %s", load.entity_id)
            self.hass.async_create_task(self._switch("turn_on", [load.entity_id]))
        elif load.entity_id not in self.missed:
            _LOGGER.info("Window start without grid: %s will start when the grid returns", load.entity_id)
            self._set_state(missed=self.missed + [load.entity_id])

    @callback
    def _async_window_end(self, load: Load) -> None:
        _LOGGER.info("Window end: turning off %s", load.entity_id)
        self.async_forget([load.entity_id])
        self.hass.async_create_task(self._switch("turn_off", [load.entity_id]))

    @callback
    def async_set_scheduled(self, load_id: str, value: bool) -> None:
        """Toggle "run on schedule"; switching it on inside the window starts the load."""
        was = self.scheduled.get(load_id, True)
        self.scheduled[load_id] = value
        load = self.loads.get(load_id)
        if value and not was and load is not None and load.window and self.monitor.is_on:
            if in_window(dt_util.now().time(), load.window):
                self.hass.async_create_task(self._switch("turn_on", [load.entity_id]))

    def _windowed(self, load: Load) -> bool:
        return load.window is not None and self.scheduled.get(load.id, True)

    def _load_for(self, entity_id: str) -> Load | None:
        return next((load for load in self.loads.values() if load.entity_id == entity_id), None)

    # --- helpers ----------------------------------------------------------

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

    async def _switch(self, service: str, entity_ids: list[str]) -> None:
        await self.hass.services.async_call(
            "switch", service, {ATTR_ENTITY_ID: entity_ids}, blocking=True
        )

    async def async_flush(self) -> None:
        """Write the state now (before a reload/unload reads it back)."""
        await self._store.async_save(self._data())

    def _data(self) -> dict:
        return {"shed": self.shed, "missed": self.missed}

    @callback
    def _set_state(self, *, shed: list[str] | None = None, missed: list[str] | None = None) -> None:
        if shed is not None:
            self.shed = shed
        if missed is not None:
            self.missed = missed
        # Delayed write also gets flushed by Store on Home Assistant shutdown.
        self._store.async_delay_save(self._data, 1)
        self.async_notify()

    @callback
    def async_notify(self) -> None:
        for listener in list(self._listeners):
            listener()
