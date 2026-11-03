"""Shed loads when the grid is lost, restore them once it is back, run schedule windows."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, time
import logging

from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later, async_track_state_change_event, async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DEFAULT_RESTORE_DELAY, DOMAIN, EVENT_RESTORED, EVENT_SHED, STORAGE_VERSION
from .grid import GridMonitor

_LOGGER = logging.getLogger(__name__)

# A command that fails is retried after these pauses (seconds); after that it stays pending
# and is retried every RECHECK_INTERVAL, or as soon as the switch comes back if it is unavailable.
RETRY_DELAYS: tuple[float, ...] = (2.0, 5.0)
RECHECK_INTERVAL: float = 60.0

_TURN_ON = "turn_on"
_TURN_OFF = "turn_off"
_TARGET = {_TURN_ON: STATE_ON, _TURN_OFF: STATE_OFF}
_GONE = (STATE_UNAVAILABLE, STATE_UNKNOWN)


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

    Each switch gets its own command, retried on failure. A command that still fails, or
    that targets an unavailable switch, stays pending until the switch reports the wanted
    state; a shed load stays in the shed list until it is really back on. A flagged load
    that comes back on during an outage (e.g. a plug that lost power and restores "on")
    is shed too.
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
        self._cancel_recheck: CALLBACK_TYPE | None = None
        self._pending: dict[str, str] = {}  # entity_id -> service not yet confirmed
        self._running = False
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
        self._running = True
        self._unsubs.append(self.monitor.async_add_listener(self._async_grid_changed))
        if self.loads:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, [load.entity_id for load in self.loads.values()], self._async_load_changed
                )
            )
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
        self._running = False
        while self._unsubs:
            self._unsubs.pop()()
        self._cancel_restore_timer()
        if self._cancel_recheck is not None:
            self._cancel_recheck()
            self._cancel_recheck = None
        self._pending.clear()

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
            # The restore turns shed loads on; a turn-off still pending for them is moot now.
            for entity_id in self.shed:
                if self._pending.get(entity_id) == _TURN_OFF:
                    del self._pending[entity_id]
            if self.shed or self.missed:
                self._schedule_restore()
            return
        self._cancel_restore_timer()
        for entity_id, service in list(self._pending.items()):
            if service == _TURN_ON:
                del self._pending[entity_id]  # stays in the shed list for the next restore
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
        await self._switch(_TURN_OFF, targets)

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
        # They leave the shed list one by one, once each is confirmed on (see _async_confirmed).
        self._set_state(shed=targets, missed=[])
        if not targets:
            return
        _LOGGER.info("Grid back: restoring %s", targets)
        self.hass.bus.async_fire(EVENT_RESTORED, {ATTR_ENTITY_ID: targets})
        await self._switch(_TURN_ON, targets)

    @callback
    def async_forget(self, entity_ids: Iterable[str]) -> None:
        """Drop loads from the shed list so they are not turned back on."""
        drop = set(entity_ids)
        for entity_id in drop:
            if self._pending.get(entity_id) == _TURN_ON:
                del self._pending[entity_id]
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
            self.hass.async_create_task(self._switch(_TURN_ON, [load.entity_id]))
        elif load.entity_id not in self.missed:
            _LOGGER.info("Window start without grid: %s will start when the grid returns", load.entity_id)
            self._set_state(missed=self.missed + [load.entity_id])

    @callback
    def _async_window_end(self, load: Load) -> None:
        _LOGGER.info("Window end: turning off %s", load.entity_id)
        self.async_forget([load.entity_id])
        self.hass.async_create_task(self._switch(_TURN_OFF, [load.entity_id]))

    @callback
    def async_set_scheduled(self, load_id: str, value: bool) -> None:
        """Toggle "run on schedule"; switching it on inside the window starts the load."""
        was = self.scheduled.get(load_id, True)
        self.scheduled[load_id] = value
        load = self.loads.get(load_id)
        if value and not was and load is not None and load.window and self.monitor.is_on:
            if in_window(dt_util.now().time(), load.window):
                self.hass.async_create_task(self._switch(_TURN_ON, [load.entity_id]))

    # --- load state ----------------------------------------------------

    @callback
    def _async_load_changed(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        new, old = event.data["new_state"], event.data["old_state"]
        if new is None or new.state in _GONE:
            return
        came_back = old is None or old.state in _GONE
        service = self._pending.get(entity_id)
        if service is not None:
            if new.state == _TARGET[service]:
                self._async_confirmed(entity_id, service)
            elif came_back:
                self.hass.async_create_task(self._command(service, entity_id))
            return
        if came_back and new.state == STATE_ON and self.monitor.is_on is False:
            load = self._load_for(entity_id)
            if entity_id in self.shed or (load is not None and self.enabled.get(load.id, True)):
                _LOGGER.info("%s came back on during the outage: shedding it", entity_id)
                if entity_id not in self.shed:
                    self._set_state(shed=self.shed + [entity_id])
                    self.hass.bus.async_fire(EVENT_SHED, {ATTR_ENTITY_ID: [entity_id]})
                self.hass.async_create_task(self._command(_TURN_OFF, entity_id))

    @callback
    def _async_confirmed(self, entity_id: str, service: str) -> None:
        if self._pending.get(entity_id) == service:
            del self._pending[entity_id]
        if service == _TURN_ON and entity_id in self.shed:
            self._set_state(shed=[e for e in self.shed if e != entity_id])

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
        """One command per switch, so one lost Zigbee frame doesn't hide the others."""
        await asyncio.gather(*(self._command(service, entity_id) for entity_id in entity_ids))

    async def _command(self, service: str, entity_id: str) -> None:
        """Send the command with retries; leave it pending if the switch doesn't take it."""
        self._pending[entity_id] = service
        for attempt, delay in enumerate((0.0, *RETRY_DELAYS), 1):
            if delay:
                await asyncio.sleep(delay)
            if not self._running or self._pending.get(entity_id) != service:
                return  # stopped, superseded, confirmed or forgotten meanwhile
            state = self.hass.states.get(entity_id)
            if state is None or state.state == STATE_UNAVAILABLE:
                _LOGGER.warning("%s is unavailable: %s waits until it is back", entity_id, service)
                return  # _async_load_changed resends it
            try:
                await self._call(service, entity_id)
            except HomeAssistantError as err:
                _LOGGER.warning("%s %s failed (attempt %d): %s", service, entity_id, attempt, err)
                continue
            self._async_confirmed(entity_id, service)
            return
        _LOGGER.warning("%s %s still failing: retrying every %d s", service, entity_id, RECHECK_INTERVAL)
        self._schedule_recheck()

    async def _call(self, service: str, entity_id: str) -> None:
        await self.hass.services.async_call("switch", service, {ATTR_ENTITY_ID: entity_id}, blocking=True)

    @callback
    def _schedule_recheck(self) -> None:
        if self._cancel_recheck is None and self._running:
            self._cancel_recheck = async_call_later(self.hass, RECHECK_INTERVAL, self._async_recheck)

    async def _async_recheck(self, _now) -> None:
        self._cancel_recheck = None
        await asyncio.gather(
            *(self._command(service, entity_id) for entity_id, service in list(self._pending.items()))
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
