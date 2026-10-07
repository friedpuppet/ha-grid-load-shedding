"""Grid presence detection."""

from datetime import timedelta

from pytest_homeassistant_custom_component.common import async_fire_time_changed, mock_restore_cache

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from custom_components.grid_load_shedding.const import CONF_FALLBACK_ENTITY

from .conftest import FALLBACK, GRID, VOLTAGE, make_entry, setup_entry


def later(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))


async def test_voltage_threshold(hass: HomeAssistant) -> None:
    hass.states.async_set(VOLTAGE, "228.4")
    await setup_entry(hass, make_entry(loads=()))
    assert hass.states.get(GRID).state == STATE_ON
    assert hass.states.get(GRID).attributes["source"] == "voltage"

    hass.states.async_set(VOLTAGE, "0.0")
    await hass.async_block_till_done()
    assert hass.states.get(GRID).state == STATE_OFF

    hass.states.async_set(VOLTAGE, "171")
    await hass.async_block_till_done()
    assert hass.states.get(GRID).state == STATE_ON


async def test_short_dropout_holds_state(hass: HomeAssistant) -> None:
    hass.states.async_set(VOLTAGE, "228")
    hass.states.async_set(FALLBACK, STATE_OFF)  # would say "no grid" if consulted
    await setup_entry(hass, make_entry(loads=()))

    hass.states.async_set(VOLTAGE, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    later(hass, 20)
    await hass.async_block_till_done()
    assert hass.states.get(GRID).state == STATE_ON
    assert hass.states.get(GRID).attributes["source"] == "hold"

    hass.states.async_set(VOLTAGE, "229")
    await hass.async_block_till_done()
    later(hass, 60)  # the cancelled hold timer must not fire
    await hass.async_block_till_done()
    assert hass.states.get(GRID).state == STATE_ON
    assert hass.states.get(GRID).attributes["source"] == "voltage"


async def test_long_dropout_uses_fallback(hass: HomeAssistant) -> None:
    hass.states.async_set(VOLTAGE, "228")
    hass.states.async_set(FALLBACK, STATE_ON)
    await setup_entry(hass, make_entry(loads=()))

    hass.states.async_set(VOLTAGE, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    later(hass, 31)
    await hass.async_block_till_done()
    assert hass.states.get(GRID).state == STATE_ON
    assert hass.states.get(GRID).attributes["source"] == "fallback"

    # Real outage while the voltage sensor is down: the fallback decides at once.
    hass.states.async_set(FALLBACK, STATE_OFF)
    await hass.async_block_till_done()
    assert hass.states.get(GRID).state == STATE_OFF

    hass.states.async_set(VOLTAGE, "230")
    await hass.async_block_till_done()
    assert hass.states.get(GRID).state == STATE_ON
    assert hass.states.get(GRID).attributes["source"] == "voltage"


async def test_no_fallback_becomes_unavailable(hass: HomeAssistant) -> None:
    hass.states.async_set(VOLTAGE, "228")
    await setup_entry(hass, make_entry({CONF_FALLBACK_ENTITY: None}, loads=()))

    hass.states.async_set(VOLTAGE, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    later(hass, 31)
    await hass.async_block_till_done()
    assert hass.states.get(GRID).state == STATE_UNAVAILABLE


async def test_restart_with_voltage_unavailable_holds_last_state(hass: HomeAssistant) -> None:
    mock_restore_cache(hass, [State(GRID, STATE_ON)])
    hass.states.async_set(VOLTAGE, STATE_UNAVAILABLE)
    hass.states.async_set(FALLBACK, STATE_UNAVAILABLE)
    await setup_entry(hass, make_entry(loads=()))
    assert hass.states.get(GRID).state == STATE_ON
    assert hass.states.get(GRID).attributes["source"] == "hold"

    later(hass, 31)
    await hass.async_block_till_done()
    assert hass.states.get(GRID).state == STATE_UNAVAILABLE
