"""Inverter-only decisions: shed on battery, restore only in bypass, nothing while blind.

The live setup since v0.8.0: a voltage sensor and a bypass (grid relay) sensor of the same
inverter, no fallback. If Home Assistant can't see the inverter (its network is down), the
grid state becomes unknown and no load is touched.
"""

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.common import async_fire_time_changed, mock_restore_cache

from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from custom_components.grid_load_shedding.const import (
    CONF_BYPASS_ENTITY,
    CONF_FALLBACK_ENTITY,
    CONF_METER_VOLTAGE_ENTITY,
    DOMAIN,
)

from .conftest import FALLBACK, GRID, SHED, VOLTAGE, make_entry, setup_entry, setup_loads

BYPASS = "binary_sensor.inverter_grid_relay"
METER = "sensor.meter_voltage"
CITY = "binary_sensor.grid_city_grid"
OPTIONS = {CONF_BYPASS_ENTITY: BYPASS, CONF_FALLBACK_ENTITY: None, CONF_METER_VOLTAGE_ENTITY: METER}


def later(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))


async def _state(hass: HomeAssistant, entity_id: str) -> str:
    await hass.async_block_till_done()
    return hass.states.get(entity_id).state


async def _inverter(hass: HomeAssistant, volts: str, relay: str) -> None:
    hass.states.async_set(VOLTAGE, volts)
    hass.states.async_set(BYPASS, relay)
    await hass.async_block_till_done()


async def _setup(hass: HomeAssistant, volts: str = "228", relay: str = STATE_ON):
    """Inverter in bypass, kettle on."""
    await setup_loads(hass)
    hass.states.async_set(VOLTAGE, volts)
    hass.states.async_set(BYPASS, relay)
    hass.states.async_set(METER, "228")
    entry = make_entry(OPTIONS, loads=("switch.kettle",))
    await setup_entry(hass, entry)
    await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: "switch.kettle"}, blocking=True)
    return entry


async def test_on_needs_voltage_and_relay(hass: HomeAssistant) -> None:
    await _setup(hass, relay=STATE_OFF)
    assert await _state(hass, GRID) == STATE_OFF
    assert hass.states.get(GRID).attributes["source"] == "relay"

    await _inverter(hass, "228", STATE_ON)
    assert await _state(hass, GRID) == STATE_ON
    assert hass.states.get(GRID).attributes["source"] == "relay"


async def test_voltage_drop_sheds_at_once(hass: HomeAssistant) -> None:
    await _setup(hass)
    await _inverter(hass, "0.0", STATE_ON)  # the relay is polled slowly; the voltage decides
    assert await _state(hass, GRID) == STATE_OFF
    assert hass.states.get(GRID).attributes["source"] == "voltage"
    assert await _state(hass, "switch.kettle") == STATE_OFF


async def test_relay_off_with_good_voltage_sheds(hass: HomeAssistant) -> None:
    await _setup(hass)
    await _inverter(hass, "228", STATE_OFF)
    assert await _state(hass, GRID) == STATE_OFF
    assert await _state(hass, "switch.kettle") == STATE_OFF


async def test_restore_waits_for_bypass(hass: HomeAssistant) -> None:
    await _setup(hass)
    await _inverter(hass, "0.0", STATE_OFF)
    assert await _state(hass, "switch.kettle") == STATE_OFF

    # Voltage is back, but the inverter hasn't switched over yet.
    await _inverter(hass, "229", STATE_OFF)
    later(hass, 120)
    assert await _state(hass, GRID) == STATE_OFF
    assert await _state(hass, "switch.kettle") == STATE_OFF

    await _inverter(hass, "229", STATE_ON)
    assert await _state(hass, GRID) == STATE_ON
    later(hass, 61)
    assert await _state(hass, "switch.kettle") == STATE_ON


async def test_blind_does_nothing(hass: HomeAssistant) -> None:
    """2026-11-16: HA lost its LAN; inverter and ping gone, the Zigbee meter still fine."""
    await _setup(hass)
    hass.states.async_set(FALLBACK, STATE_OFF)  # not configured: must not matter
    await _inverter(hass, STATE_UNAVAILABLE, STATE_UNAVAILABLE)
    later(hass, 31)
    assert await _state(hass, GRID) == STATE_UNAVAILABLE
    later(hass, 600)
    assert await _state(hass, "switch.kettle") == STATE_ON
    assert await _state(hass, SHED) == "0"

    await _inverter(hass, "228", STATE_ON)
    assert await _state(hass, GRID) == STATE_ON
    later(hass, 61)
    assert await _state(hass, "switch.kettle") == STATE_ON


async def test_restart_while_blind_does_nothing(hass: HomeAssistant) -> None:
    mock_restore_cache(hass, [State(GRID, STATE_ON)])
    await setup_loads(hass)
    await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: "switch.kettle"}, blocking=True)
    hass.states.async_set(VOLTAGE, STATE_UNAVAILABLE)
    hass.states.async_set(BYPASS, STATE_UNAVAILABLE)
    hass.states.async_set(FALLBACK, STATE_OFF)
    await setup_entry(hass, make_entry(OPTIONS, loads=("switch.kettle",)))
    assert hass.states.get(GRID).attributes["source"] == "hold"
    later(hass, 31)
    assert await _state(hass, GRID) == STATE_UNAVAILABLE
    later(hass, 600)
    assert await _state(hass, "switch.kettle") == STATE_ON


async def test_loss_seen_after_blind_spell_sheds(hass: HomeAssistant) -> None:
    """on -> unknown -> off: the outage began while HA couldn't see the inverter."""
    await _setup(hass)
    await _inverter(hass, STATE_UNAVAILABLE, STATE_UNAVAILABLE)
    later(hass, 31)
    assert await _state(hass, GRID) == STATE_UNAVAILABLE
    await _inverter(hass, "0.0", STATE_OFF)
    assert await _state(hass, GRID) == STATE_OFF
    assert await _state(hass, "switch.kettle") == STATE_OFF


async def test_manual_on_survives_blind_spell(hass: HomeAssistant) -> None:
    """off -> unknown -> off: a load switched on by hand during the outage stays on."""
    await _setup(hass)
    await _inverter(hass, "0.0", STATE_OFF)
    assert await _state(hass, "switch.kettle") == STATE_OFF
    await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: "switch.kettle"}, blocking=True)
    await _inverter(hass, STATE_UNAVAILABLE, STATE_UNAVAILABLE)
    later(hass, 31)
    await _inverter(hass, "0.0", STATE_OFF)
    assert await _state(hass, GRID) == STATE_OFF
    assert await _state(hass, "switch.kettle") == STATE_ON


async def test_restart_restores_only_once_inverter_seen(hass: HomeAssistant, hass_storage) -> None:
    """A shed list from before the restart is restored on live bypass, not on the held state."""
    entry = make_entry(OPTIONS, loads=("switch.kettle",))
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {
        "version": 1,
        "key": f"{DOMAIN}.{entry.entry_id}",
        "data": {"shed": ["switch.kettle"], "missed": []},
    }
    mock_restore_cache(hass, [State(GRID, STATE_ON)])
    await setup_loads(hass)
    await hass.services.async_call("switch", "turn_off", {ATTR_ENTITY_ID: "switch.kettle"}, blocking=True)
    hass.states.async_set(VOLTAGE, STATE_UNAVAILABLE)
    hass.states.async_set(BYPASS, STATE_UNAVAILABLE)
    await setup_entry(hass, entry)
    later(hass, 25)  # still holding
    later(hass, 61)
    assert await _state(hass, "switch.kettle") == STATE_OFF

    await _inverter(hass, "228", STATE_ON)
    later(hass, 61)
    assert await _state(hass, "switch.kettle") == STATE_ON
    assert await _state(hass, SHED) == "0"


async def test_restore_timer_waits_while_held(hass: HomeAssistant) -> None:
    """The inverter vanishes during the restore delay: restore only once it's seen again."""
    await _setup(hass)
    await _inverter(hass, "0.0", STATE_OFF)
    await _inverter(hass, "228", STATE_ON)
    later(hass, 40)
    await _inverter(hass, STATE_UNAVAILABLE, STATE_UNAVAILABLE)
    later(hass, 61)  # the restore timer fires while holding
    assert await _state(hass, "switch.kettle") == STATE_OFF
    await _inverter(hass, "228", STATE_ON)
    later(hass, 61)
    assert await _state(hass, "switch.kettle") == STATE_ON


async def test_city_grid(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await _setup(hass)
    assert await _state(hass, CITY) == STATE_ON

    # Brownout: the relay before the inverter opened; the meter at the supply still sees 178 V.
    hass.states.async_set(METER, "178")
    await _inverter(hass, "0.0", STATE_OFF)
    assert await _state(hass, GRID) == STATE_OFF
    assert await _state(hass, CITY) == STATE_ON
    assert hass.states.get(CITY).attributes["source"] == "meter"

    # Outage: the meter goes silent (keeps its last state), the inverter reads 0 V.
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    assert await _state(hass, CITY) == STATE_OFF
    assert hass.states.get(CITY).attributes["source"] == "inverter"

    # Nothing known at all.
    await _inverter(hass, STATE_UNAVAILABLE, STATE_UNAVAILABLE)
    assert await _state(hass, CITY) == STATE_UNAVAILABLE

    # HA blind to the inverter, meter fine: city on; the flat sensor stays out of it.
    hass.states.async_set(METER, "225")
    assert await _state(hass, CITY) == STATE_ON
