"""Battery levels from a single percentage sensor.

Any entity reporting a charge in percent will do (an inverter battery, a phone), so nothing
here knows about power, capacity or voltage. The discharge rate comes from the percentage
itself: every fall of ``SEGMENT_PERCENT`` points is one segment with its own %/h, and the rate
in use is the median of the segments of the last ``WINDOW``. Fixed points rather than state
changes, so a sensor reporting 0.1 % steps and a phone reporting whole percents behave alike.
Rates between single 0.1 % steps only take the few values the sensor's update period allows.

Each level is announced once per discharge. The passed levels are stored, so a restart in the
middle of an outage repeats nothing.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
import logging
from statistics import median

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import BATTERY_LEVELS, DOMAIN, RUNTIME_LEVELS, SOC_LEVELS, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

WINDOW = timedelta(minutes=30)
SEGMENT_PERCENT = 1.0
MIN_SEGMENTS = 3
REARM_PERCENT = 5.0  # a level comes back only after the battery really charged, not on a grid blink
SAVE_DELAY = 10
PRUNE_INTERVAL = timedelta(minutes=1)


def _percent(state: str | None) -> float | None:
    if state is None or state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    try:
        return float(state)
    except ValueError:
        return None


class BatteryMonitor:
    """Estimate the time left and report each level crossed while discharging."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        *,
        entity_id: str,
        reserve: float,
        on_level: Callable[[str], None],
    ) -> None:
        self.hass = hass
        self.entity_id = entity_id
        self.reserve = reserve
        self.soc: float | None = None
        self.passed: set[str] = set()
        self._on_level = on_level
        self._segments: deque[tuple[datetime, float]] = deque()  # end of segment, %/h
        self._anchor: tuple[datetime, float] | None = None  # start of the current segment
        self._low: float | None = None  # lowest charge since the runtime levels were re-armed
        self._listeners: list[CALLBACK_TYPE] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.battery")

    @property
    def median_rate(self) -> float | None:
        """Median discharge rate in %/h, once there are enough segments."""
        if len(self._segments) < MIN_SEGMENTS:
            return None
        return median(rate for _, rate in self._segments)

    @property
    def runtime_minutes(self) -> float | None:
        """Time left at the median rate; 0 once the usable charge is gone."""
        if self.soc is None:
            return None
        usable = self.soc - self.reserve
        if usable <= 0:
            return 0.0
        rate = self.median_rate
        return None if rate is None else usable / rate * 60

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.passed = set(data.get("passed", [])) & set(BATTERY_LEVELS)
        self._low = data.get("low")

    @callback
    def async_start(self) -> None:
        self._unsubs = [
            async_track_state_change_event(self.hass, self.entity_id, self._async_state_changed),
            async_track_time_interval(self.hass, self._async_prune, PRUNE_INTERVAL),
        ]
        state = self.hass.states.get(self.entity_id)
        if (soc := _percent(state.state if state else None)) is not None:
            self._async_update(soc, dt_util.utcnow())

    @callback
    def async_stop(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()

    async def async_flush(self) -> None:
        await self._store.async_save(self._data())

    @callback
    def async_add_listener(self, listener: CALLBACK_TYPE) -> CALLBACK_TYPE:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def _data(self) -> dict:
        return {"passed": sorted(self.passed), "low": self._low}

    @callback
    def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        if (soc := _percent(new.state if new else None)) is not None:
            self._async_update(soc, dt_util.utcnow())

    @callback
    def _async_prune(self, now: datetime) -> None:
        self._prune(now)
        self._notify()

    def _prune(self, now: datetime) -> None:
        while self._segments and now - self._segments[0][0] > WINDOW:
            self._segments.popleft()

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    @callback
    def _async_update(self, soc: float, now: datetime) -> None:
        previous, self.soc = self.soc, soc
        self._track_rate(soc, now)
        self._prune(now)
        changed = self._rearm(soc)
        if previous is not None and soc < previous:  # levels only matter on the way down
            changed = self._check_levels(soc) or changed
        if changed:
            self._store.async_delay_save(self._data, SAVE_DELAY)
        self._notify()

    def _track_rate(self, soc: float, now: datetime) -> None:
        if self._anchor is None or now - self._anchor[0] > WINDOW:
            self._anchor = (now, soc)  # after an idle stretch, which says nothing about the rate
            return
        started, level = self._anchor
        if soc >= level + SEGMENT_PERCENT:  # charging: the old rate no longer applies
            self._segments.clear()
            self._anchor = (now, soc)
        elif level - soc >= SEGMENT_PERCENT:
            hours = (now - started).total_seconds() / 3600
            if hours > 0:
                self._segments.append((now, (level - soc) / hours))
            self._anchor = (now, soc)

    def _rearm(self, soc: float) -> bool:
        """Bring levels back after a real charge; return whether the stored data changed."""
        before = (set(self.passed), self._low)
        self.passed -= {event for event, level in SOC_LEVELS.items() if soc >= level + REARM_PERCENT}
        if self._low is None or soc < self._low:
            self._low = soc
        elif soc >= self._low + REARM_PERCENT:
            self.passed -= set(RUNTIME_LEVELS)
            self._low = soc
        return (self.passed, self._low) != before

    def _check_levels(self, soc: float) -> bool:
        runtime = self.runtime_minutes
        crossed = [
            event
            for event in BATTERY_LEVELS
            if event not in self.passed
            and (
                soc < SOC_LEVELS[event]
                if event in SOC_LEVELS
                else runtime is not None and runtime <= RUNTIME_LEVELS[event]
            )
        ]
        if not crossed:
            return False
        self.passed.update(crossed)
        _LOGGER.info("Battery at %s %%, runtime %s min: %s", soc, runtime, crossed)
        self._on_level(crossed[0])  # one player can't say several at once: the most urgent only
        return True
