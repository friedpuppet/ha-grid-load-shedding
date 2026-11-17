"""Utility-grid presence detection.

``GridMonitor`` answers "is the flat on grid power right now?" (the inverter in bypass):
- off while the inverter's grid-input voltage is at or below the threshold, or while its
  optional bypass sensor (e.g. the grid relay) is off;
- on only while the voltage is above the threshold and the bypass sensor (if set) is on,
  so a returning grid counts only once the inverter has actually switched over.
Inverter telemetry is often flaky (its Wi-Fi bridge drops for a few seconds), so while it
is unavailable the last decision is held for ``hold_seconds``; after that an optional
fallback binary_sensor decides. Without a fallback, or with the fallback unavailable too,
the state becomes unknown (``None``) rather than "off", so losing sight of the inverter
(e.g. Home Assistant's own network is down) never looks like a grid outage. A fallback
that depends on the same network as the inverter defeats that, so leave it empty then.

``CityGridMonitor`` answers "is there voltage at the supply?", whether or not the
inverter accepts it (a brownout is "city on, flat off"). It is informational only.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
import logging

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

type GridListener = Callable[[bool | None, bool | None], None]


def voltage_decision(state: str | None, threshold: float) -> bool | None:
    """Return grid presence from a voltage state, or None if it isn't a number."""
    if state is None or state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    try:
        return float(state) > threshold
    except ValueError:
        return None


def fallback_decision(state: str | None) -> bool | None:
    """Return grid presence from the fallback binary_sensor state."""
    if state == STATE_ON:
        return True
    if state == STATE_OFF:
        return False
    return None


class GridMonitor:
    """Track the grid state and notify listeners on every change."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        voltage_entity: str,
        threshold: float,
        hold_seconds: float,
        fallback_entity: str | None,
        bypass_entity: str | None = None,
    ) -> None:
        self.hass = hass
        self.voltage_entity = voltage_entity
        self.threshold = threshold
        self.hold_seconds = hold_seconds
        self.fallback_entity = fallback_entity
        self.bypass_entity = bypass_entity
        self.is_on: bool | None = None
        # The last known (non-None) state before the latest change; lets listeners tell
        # "on -> unknown -> off" (a real loss seen late) from "off -> unknown -> off".
        self.known_before: bool | None = None
        self._last_known: bool | None = None
        # Which input currently decides: "voltage", "relay", "hold" or "fallback"
        self.source: str = "voltage"
        self._listeners: list[GridListener] = []
        self._unsub_track: CALLBACK_TYPE | None = None
        self._cancel_hold: CALLBACK_TYPE | None = None

    @callback
    def async_add_listener(self, listener: GridListener) -> CALLBACK_TYPE:
        """Register ``listener(old, new)``; returns a remover."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def async_start(self) -> None:
        """Subscribe to the source sensors and compute the initial state."""
        entities = [self.voltage_entity]
        if self.bypass_entity:
            entities.append(self.bypass_entity)
        if self.fallback_entity:
            entities.append(self.fallback_entity)
        self._unsub_track = async_track_state_change_event(
            self.hass, entities, self._async_on_source_change
        )
        self._async_evaluate()

    @callback
    def async_stop(self) -> None:
        """Unsubscribe and cancel timers."""
        if self._unsub_track:
            self._unsub_track()
            self._unsub_track = None
        self._cancel_hold_timer()

    @callback
    def async_restore(self, last: bool | None) -> None:
        """Seed the state from the entity's last known state after a restart.

        Only used while the voltage is still unavailable at startup, so a restart
        during a voltage-sensor blip keeps the previous decision for ``hold_seconds``.
        """
        if self.is_on is not None or last is None:
            return
        if self._decision()[0] is not None:
            return
        self._cancel_hold_timer()
        self._start_hold_timer()
        self._set(last, "hold")

    @callback
    def _async_on_source_change(self, event: Event[EventStateChangedData]) -> None:
        self._async_evaluate()

    @callback
    def _async_evaluate(self) -> None:
        decision, source = self._decision()
        if decision is not None:
            self._cancel_hold_timer()
            self._set(decision, source)
            return

        if self._cancel_hold is not None:
            return  # still holding; the timer decides
        if self.source == "fallback" or self.is_on is None:
            # Already past the hold, or nothing to hold (startup): follow the fallback.
            self._set(self._fallback(), "fallback")
            return
        self._start_hold_timer()
        self._set(self.is_on, "hold")

    @callback
    def _start_hold_timer(self) -> None:
        self._cancel_hold = async_call_later(self.hass, self.hold_seconds, self._async_hold_expired)

    @callback
    def _async_hold_expired(self, _now) -> None:
        self._cancel_hold = None
        if self._decision()[0] is None:
            _LOGGER.debug("%s unavailable for %ss, using fallback", self.voltage_entity, self.hold_seconds)
            self._set(self._fallback(), "fallback")

    @callback
    def _cancel_hold_timer(self) -> None:
        if self._cancel_hold is not None:
            self._cancel_hold()
            self._cancel_hold = None

    def _decision(self) -> tuple[bool | None, str]:
        """Decide from the inverter alone: (state, deciding input), state None if unknown."""
        voltage = voltage_decision(self._state(self.voltage_entity), self.threshold)
        if not self.bypass_entity:
            return voltage, "voltage"
        bypass = fallback_decision(self._state(self.bypass_entity))
        if voltage is False:
            return False, "voltage"  # fastest sign of running on the battery
        if bypass is False:
            return False, "relay"  # grid lost by the relay, or back but not yet switched over
        if voltage and bypass:
            return True, "relay"
        return None, "voltage"

    def _fallback(self) -> bool | None:
        if not self.fallback_entity:
            return None
        return fallback_decision(self._state(self.fallback_entity))

    def _state(self, entity_id: str) -> str | None:
        state = self.hass.states.get(entity_id)
        return state.state if state else None

    @callback
    def _set(self, value: bool | None, source: str) -> None:
        """Update state; listeners also hear source-only changes (old == new)."""
        if value == self.is_on and source == self.source:
            return
        old, self.is_on, self.source = self.is_on, value, source
        self.known_before = self._last_known
        if value is not None:
            self._last_known = value
        if old != value:
            _LOGGER.debug("Grid %s -> %s (source: %s)", old, value, source)
        for listener in list(self._listeners):
            listener(old, value)


class CityGridMonitor:
    """Voltage at the supply, whether or not the inverter accepts it (informational).

    on:  a fresh meter reading or the inverter's grid voltage is above ``threshold``;
    off: the inverter reads at or below it and the meter is silent, stale or low too
         (or the meter is fresh and low while the inverter is unknown);
    else unknown. "Fresh" = reported within ``fresh_seconds`` (``last_reported``), because a
    mains-powered meter that lost power keeps its last state for minutes.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        inverter_entity: str,
        meter_entity: str | None,
        threshold: float,
        fresh_seconds: float,
    ) -> None:
        self.hass = hass
        self.inverter_entity = inverter_entity
        self.meter_entity = meter_entity
        self.threshold = threshold
        self.fresh_seconds = fresh_seconds
        self.is_on: bool | None = None
        self.source: str | None = None  # "meter" or "inverter"
        self.voltage: float | None = None
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[CALLBACK_TYPE] = []

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def async_start(self) -> None:
        entities = [self.inverter_entity] + ([self.meter_entity] if self.meter_entity else [])
        self._unsubs.append(async_track_state_change_event(self.hass, entities, self._async_on_change))
        if self.meter_entity:
            # Staleness produces no event, so look again now and then.
            self._unsubs.append(async_track_time_interval(self.hass, self._async_on_change, timedelta(seconds=10)))
        self._async_evaluate()

    @callback
    def _async_on_change(self, _event_or_now) -> None:
        self._async_evaluate()

    @callback
    def async_stop(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()

    def _number(self, entity_id: str, fresh: bool) -> float | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        if fresh and (dt_util.utcnow() - state.last_reported).total_seconds() > self.fresh_seconds:
            return None
        try:
            return float(state.state)
        except ValueError:
            return None

    @callback
    def _async_evaluate(self) -> None:
        meter = self._number(self.meter_entity, fresh=True) if self.meter_entity else None
        inverter = self._number(self.inverter_entity, fresh=False)
        if meter is not None and meter > self.threshold:
            value, source, voltage = True, "meter", meter
        elif inverter is not None and inverter > self.threshold:
            value, source, voltage = True, "inverter", inverter
        elif inverter is not None:
            value, source, voltage = False, "inverter", inverter
        elif meter is not None:
            value, source, voltage = False, "meter", meter
        else:
            value, source, voltage = None, None, None
        if (value, source, voltage) == (self.is_on, self.source, self.voltage):
            return
        self.is_on, self.source, self.voltage = value, source, voltage
        for listener in list(self._listeners):
            listener()
