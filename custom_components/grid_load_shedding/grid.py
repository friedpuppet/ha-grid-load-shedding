"""Utility-grid presence detection.

The primary signal is a voltage sensor (e.g. a hybrid inverter's grid-input voltage):
grid is present while the voltage is above a threshold. Voltage sources are often
flaky (the inverter's Wi-Fi bridge drops for a few seconds), so while the voltage is
unavailable the last decision is held for ``hold_seconds``; after that an optional
fallback binary_sensor (e.g. ping of a device powered from the grid side) decides.
Without a fallback, or with the fallback unavailable too, the state becomes unknown
(``None``) rather than "off", so a dead sensor never looks like a grid outage.
"""

from __future__ import annotations

from collections.abc import Callable
import logging

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

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
    ) -> None:
        self.hass = hass
        self.voltage_entity = voltage_entity
        self.threshold = threshold
        self.hold_seconds = hold_seconds
        self.fallback_entity = fallback_entity
        self.is_on: bool | None = None
        # Which input currently decides: "voltage", "hold" or "fallback"
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
        if voltage_decision(self._state(self.voltage_entity), self.threshold) is not None:
            return
        self._cancel_hold_timer()
        self._start_hold_timer()
        self._set(last, "hold")

    @callback
    def _async_on_source_change(self, event: Event[EventStateChangedData]) -> None:
        self._async_evaluate()

    @callback
    def _async_evaluate(self) -> None:
        decision = voltage_decision(self._state(self.voltage_entity), self.threshold)
        if decision is not None:
            self._cancel_hold_timer()
            self._set(decision, "voltage")
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
        if voltage_decision(self._state(self.voltage_entity), self.threshold) is None:
            _LOGGER.debug("%s unavailable for %ss, using fallback", self.voltage_entity, self.hold_seconds)
            self._set(self._fallback(), "fallback")

    @callback
    def _cancel_hold_timer(self) -> None:
        if self._cancel_hold is not None:
            self._cancel_hold()
            self._cancel_hold = None

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
        if old != value:
            _LOGGER.debug("Grid %s -> %s (source: %s)", old, value, source)
        for listener in list(self._listeners):
            listener(old, value)
