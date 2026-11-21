"""Battery levels from a single percentage sensor.

Any entity reporting a charge in percent will do (an inverter battery, a phone), so nothing
here knows about power, capacity or voltage. The discharge rate comes from the percentage
itself: every fall of ``SEGMENT_PERCENT`` points is one segment with its own %/h, and the rate
in use is the median of the last ``MAX_SEGMENTS`` segments within ``WINDOW``: a slow battery
reporting whole percents still gets an estimate, and a fast one follows a change of load within
minutes. Fixed points rather than state changes, so a sensor reporting 0.1 % steps and a phone
reporting whole percents behave alike.
Rates between single 0.1 % steps only take the few values the sensor's update period allows.

Each level is announced once per discharge. The passed levels are stored, so a restart in the
middle of an outage repeats nothing.

With a battery power sensor and the grid monitor, the time left comes from power instead.
A battery session is the time the grid monitor says the grid is off; "unknown" (the inverter
out of sight) neither starts nor ends one. Within a session the power is averaged per minute
(time-weighted), each minute goes through a median of the last ``MEDIAN_MINUTES`` minutes, so a
kettle or a short dip of a few minutes is dropped, and then through an exponential average with
``POWER_TAU``, bias-corrected so that it is the plain mean during the first minutes of a session.
Time left = usable charge (charge above the reserve, times the capacity) / smoothed power. If the
power is stale or missing, the percentage method above is used.

The reserve is a fixed percentage, or the state of a reserve entity when one is set and has a
number (e.g. the level an inverter actually stops discharging at, which changes with its mode).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
import logging
import math
from statistics import median
from typing import TYPE_CHECKING, Any

from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import BATTERY_LEVELS, DOMAIN, RUNTIME_LEVELS, SOC_LEVELS, STORAGE_VERSION

if TYPE_CHECKING:
    from .grid import GridMonitor

_LOGGER = logging.getLogger(__name__)

WINDOW = timedelta(minutes=90)
SEGMENT_PERCENT = 1.0
MIN_SEGMENTS = 3
MAX_SEGMENTS = 5
REARM_PERCENT = 5.0  # a level comes back only after the battery really charged, not on a grid blink
SAVE_DELAY = 10
PRUNE_INTERVAL = timedelta(minutes=1)  # also the power bucket and the sensor's update period

POWER_TAU = timedelta(minutes=45)
MEDIAN_MINUTES = 15  # a burst or dip shorter than half of this doesn't reach the average
MIN_POWER = 50.0  # W; below this the battery isn't really discharging
POWER_STALE = timedelta(minutes=15)  # no power minute for this long: the average is dropped
MIN_BUCKET_SECONDS = 10
SAVE_EVERY = 5  # minutes, while in a session
MIN_CAPACITY_DROP = 5.0  # percent discharged before the capacity is measured


def _percent(state: str | None) -> float | None:
    if state is None or state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    try:
        return float(state)
    except ValueError:
        return None


def _watts(state) -> float | None:
    if state is None or (value := _percent(state.state)) is None:
        return None
    return value * 1000 if state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) == "kW" else value


def _time(value: str | None) -> datetime | None:
    return dt_util.parse_datetime(value) if value else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class PowerSmoother:
    """Median of recent minutes, then a bias-corrected exponential average."""

    def __init__(self) -> None:
        self.ema = 0.0
        self.weight = 0.0
        self.minutes: deque[float] = deque(maxlen=MEDIAN_MINUTES)
        self.updated: datetime | None = None

    @property
    def value(self) -> float | None:
        return self.ema / self.weight if self.weight > 0 else None

    def add(self, mean: float, seconds: float, now: datetime) -> None:
        if self.updated is not None and now - self.updated > POWER_STALE:
            self.__init__()
        self.minutes.append(mean)
        alpha = 1 - math.exp(-seconds / POWER_TAU.total_seconds())
        self.ema = (1 - alpha) * self.ema + alpha * median(self.minutes)
        self.weight = (1 - alpha) * self.weight + alpha
        self.updated = now

    def as_dict(self) -> dict[str, Any]:
        return {"ema": self.ema, "weight": self.weight, "minutes": list(self.minutes), "updated": _iso(self.updated)}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> PowerSmoother:
        smoother = cls()
        if data:
            smoother.ema = data.get("ema", 0.0)
            smoother.weight = data.get("weight", 0.0)
            smoother.minutes.extend(data.get("minutes", []))
            smoother.updated = _time(data.get("updated"))
        return smoother


class BatteryMonitor:
    """Estimate the time left and report each level crossed while discharging."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        *,
        entity_id: str,
        reserve: float,
        reserve_entity: str | None = None,
        on_level: Callable[[str], None],
        power_entity: str | None = None,
        capacity: float | None = None,
        grid: GridMonitor | None = None,
    ) -> None:
        self.hass = hass
        self.entity_id = entity_id
        self.fixed_reserve = reserve
        self.reserve_entity = reserve_entity
        self.power_entity = power_entity
        self.capacity = capacity  # kWh
        self._grid = grid
        # Current battery session: start, charge at the start, energy given so far (Wh)
        self.session_start: datetime | None = None
        self._session_soc: float | None = None
        self.session_energy = 0.0
        self.previous: dict[str, Any] = {}  # start, end, energy (kWh) of the last session
        self._smoother = PowerSmoother()
        self._power: float | None = None  # W, last reading
        self._power_since: datetime | None = None
        self._bucket = [0.0, 0.0]  # W·s, seconds covered in the current minute
        self._ticks = 0
        self.soc: float | None = None
        self.passed: set[str] = set()
        self._on_level = on_level
        self._segments: deque[tuple[datetime, float]] = deque(maxlen=MAX_SEGMENTS)  # end of segment, %/h
        self._anchor: tuple[datetime, float] | None = None  # start of the current segment
        self._low: float | None = None  # lowest charge since the runtime levels were re-armed
        self._listeners: list[CALLBACK_TYPE] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.battery")

    @property
    def reserve(self) -> float:
        """The reserve entity's number if there is one, the fixed reserve otherwise."""
        if self.reserve_entity:
            state = self.hass.states.get(self.reserve_entity)
            if (value := _percent(state.state if state else None)) is not None:
                return value
        return self.fixed_reserve

    @property
    def median_rate(self) -> float | None:
        """Median discharge rate in %/h, once there are enough segments."""
        if len(self._segments) < MIN_SEGMENTS:
            return None
        return median(rate for _, rate in self._segments)

    @property
    def uses_power(self) -> bool:
        return bool(self.power_entity and self.capacity and self._grid)

    @property
    def smoothed_power(self) -> float | None:
        """Smoothed discharge power in W, while in a session and not stale."""
        if not self.uses_power or self.session_start is None:
            return None
        updated = self._smoother.updated
        if updated is None or dt_util.utcnow() - updated > POWER_STALE:
            return None
        return self._smoother.value

    @property
    def method(self) -> str:
        return "power" if self.smoothed_power is not None else "soc"

    @property
    def measured_capacity(self) -> float | None:
        """kWh: the session's energy over its fall of charge, once that is large enough."""
        if self.session_start is None or self._session_soc is None or self.soc is None:
            return None
        drop = self._session_soc - self.soc
        if drop < MIN_CAPACITY_DROP or self.session_energy <= 0:
            return None
        return self.session_energy / drop * 100 / 1000

    @property
    def runtime_minutes(self) -> float | None:
        """Time left at the smoothed power or the median rate; 0 once the usable charge is gone."""
        if self.soc is None:
            return None
        usable = self.soc - self.reserve
        if usable <= 0:
            return 0.0
        if (power := self.smoothed_power) is not None:
            if power < MIN_POWER:
                return None
            return usable / 100 * self.capacity * 1000 / power * 60
        rate = self.median_rate
        return None if rate is None else usable / rate * 60

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.passed = set(data.get("passed", [])) & set(BATTERY_LEVELS)
        self._low = data.get("low")
        session = data.get("session") or {}
        self.session_start = _time(session.get("start"))
        self._session_soc = session.get("soc")
        self.session_energy = session.get("energy", 0.0)
        self.previous = data.get("previous") or {}
        self._smoother = PowerSmoother.from_dict(data.get("power"))

    @callback
    def async_start(self) -> None:
        now = dt_util.utcnow()
        self._unsubs = [
            async_track_state_change_event(self.hass, self.entity_id, self._async_state_changed),
            async_track_time_interval(self.hass, self._async_tick, PRUNE_INTERVAL),
        ]
        state = self.hass.states.get(self.entity_id)
        if (soc := _percent(state.state if state else None)) is not None:
            self._async_update(soc, now)
        if self._grid:
            self._unsubs.append(self._grid.async_add_listener(self._async_grid_changed))
            self._follow_grid(self._grid.is_on, now)  # the grid may have changed while HA was down
        if self.reserve_entity:
            self._unsubs.append(
                async_track_state_change_event(self.hass, self.reserve_entity, self._async_reserve_changed)
            )
        if self.power_entity:
            self._unsubs.append(
                async_track_state_change_event(self.hass, self.power_entity, self._async_power_changed)
            )
            self._power = _watts(self.hass.states.get(self.power_entity))
            self._power_since = now

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
        return {
            "passed": sorted(self.passed),
            "low": self._low,
            "session": {"start": _iso(self.session_start), "soc": self._session_soc, "energy": self.session_energy},
            "previous": self.previous,
            "power": self._smoother.as_dict(),
        }

    @callback
    def _async_grid_changed(self, old: bool | None, new: bool | None) -> None:
        if self._follow_grid(new, dt_util.utcnow()):
            self._store.async_delay_save(self._data, SAVE_DELAY)
            self._notify()

    def _follow_grid(self, grid: bool | None, now: datetime) -> bool:
        """Start or end the battery session; return whether it changed."""
        if grid is False and self.session_start is None:
            self._accumulate(now)
            self.session_start, self._session_soc, self.session_energy = now, self.soc, 0.0
            self._smoother = PowerSmoother()
            self._bucket = [0.0, 0.0]
            _LOGGER.info("Battery session started at %s %%", self.soc)
            return True
        if grid is True and self.session_start is not None:
            self._accumulate(now)
            self.previous = {
                "start": _iso(self.session_start),
                "end": _iso(now),
                "energy": round(self.session_energy / 1000, 2) if self.power_entity else None,
            }
            _LOGGER.info("Battery session ended: %s", self.previous)
            self.session_start, self._session_soc, self.session_energy = None, None, 0.0
            return True
        return False

    @callback
    def _async_reserve_changed(self, _event: Event[EventStateChangedData]) -> None:
        self._notify()

    @callback
    def _async_power_changed(self, event: Event[EventStateChangedData]) -> None:
        now = dt_util.utcnow()
        self._accumulate(now)
        self._power = _watts(event.data["new_state"])

    def _accumulate(self, now: datetime) -> None:
        """Add the last power reading, held since it came, to the minute and the session."""
        since, self._power_since = self._power_since, now
        if self._power is None or since is None or self.session_start is None:
            return
        seconds = (now - since).total_seconds()
        if seconds <= 0:
            return
        power = max(self._power, 0.0)
        self._bucket[0] += power * seconds
        self._bucket[1] += seconds
        self.session_energy += power * seconds / 3600

    @callback
    def _async_tick(self, now: datetime) -> None:
        if self.uses_power:
            now = dt_util.utcnow()
            self._accumulate(now)
            energy, seconds = self._bucket
            self._bucket = [0.0, 0.0]
            if seconds >= MIN_BUCKET_SECONDS:
                self._smoother.add(energy / seconds, seconds, now)
            if self.session_start is not None:
                self._ticks += 1
                if self._ticks % SAVE_EVERY == 0:
                    self._store.async_delay_save(self._data, SAVE_DELAY)
        self._async_prune(now)

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
