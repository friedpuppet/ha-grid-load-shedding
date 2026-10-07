"""Per-load schedule windows (e.g. a boiler that heats 01:00–07:00)."""

from datetime import datetime, time, timedelta

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.grid_load_shedding.const import (
    CONF_SWITCH_ENTITY,
    CONF_WINDOW_END,
    CONF_WINDOW_START,
    DOMAIN,
    SUBENTRY_LOAD,
)
from custom_components.grid_load_shedding.shedder import in_window

from .conftest import SHED, VOLTAGE, flag_entity, make_entry, setup_entry, setup_loads

BOILER = "switch.boiler"
NIGHT = {BOILER: ("01:00:00", "07:00:00")}


@pytest.fixture
async def clock(hass: HomeAssistant, freezer: FrozenDateTimeFactory):
    """Move local wall-clock time and fire time-change listeners."""

    async def to(hh: int, mm: int = 0, ss: int = 0) -> None:
        now = dt_util.now()
        target = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        freezer.move_to(target)
        async_fire_time_changed(hass, target)
        await hass.async_block_till_done()

    async def wait(seconds: float) -> None:
        target = dt_util.now() + timedelta(seconds=seconds)
        freezer.move_to(target)
        async_fire_time_changed(hass, target)
        await hass.async_block_till_done()

    start = dt_util.now().replace(hour=0, minute=30, second=0, microsecond=0)
    freezer.move_to(start)
    to.wait = wait
    return to


async def _setup(hass: HomeAssistant, windows=NIGHT, volts: str = "228"):
    await setup_loads(hass)
    hass.states.async_set(VOLTAGE, volts)
    entry = make_entry(loads=(BOILER, "switch.kettle"), windows=windows)
    await setup_entry(hass, entry)
    for load in (BOILER, "switch.kettle"):
        await _switch(hass, "turn_off", load)  # optimistic template switches start "unknown"
    return entry


def _state(hass: HomeAssistant, entity_id: str) -> str:
    return hass.states.get(entity_id).state


async def _grid(hass: HomeAssistant, volts: str) -> None:
    hass.states.async_set(VOLTAGE, volts)
    await hass.async_block_till_done()


async def _switch(hass: HomeAssistant, service: str, entity_id: str) -> None:
    await hass.services.async_call("switch", service, {ATTR_ENTITY_ID: entity_id}, blocking=True)


def test_in_window() -> None:
    night = (time(1), time(7))
    assert in_window(time(1), night) and in_window(time(6, 59), night)
    assert not in_window(time(7), night) and not in_window(time(0, 59), night)
    over_midnight = (time(22), time(2))
    assert in_window(time(23), over_midnight) and in_window(time(1), over_midnight)
    assert not in_window(time(2), over_midnight) and not in_window(time(21), over_midnight)


async def test_window_turns_on_and_off(hass: HomeAssistant, clock) -> None:
    entry = await _setup(hass)
    assert _state(hass, flag_entity(hass, entry, BOILER, "run_on_schedule")) == STATE_ON
    assert hass.states.get(flag_entity(hass, entry, BOILER, "run_on_schedule")).attributes["window_start"] == "01:00:00"
    assert _state(hass, BOILER) == STATE_OFF
    await clock(1)
    assert _state(hass, BOILER) == STATE_ON
    await clock(7)
    assert _state(hass, BOILER) == STATE_OFF


async def test_no_schedule_switch_for_loads_without_window(hass: HomeAssistant, clock) -> None:
    entry = await _setup(hass)
    subentry_id = next(s.subentry_id for s in entry.subentries.values() if s.data[CONF_SWITCH_ENTITY] == "switch.kettle")
    assert er.async_get(hass).async_get_entity_id("switch", DOMAIN, f"{subentry_id}_run_on_schedule") is None


async def test_schedule_switch_off_disables_window(hass: HomeAssistant, clock) -> None:
    entry = await _setup(hass)
    await _switch(hass, "turn_off", flag_entity(hass, entry, BOILER, "run_on_schedule"))
    await clock(1)
    assert _state(hass, BOILER) == STATE_OFF
    # Without the schedule the load behaves like any other: manual on stays on at 07:00.
    await _switch(hass, "turn_on", BOILER)
    await clock(7)
    assert _state(hass, BOILER) == STATE_ON


async def test_schedule_switch_on_inside_window_starts_load(hass: HomeAssistant, clock) -> None:
    entry = await _setup(hass)
    schedule = flag_entity(hass, entry, BOILER, "run_on_schedule")
    await _switch(hass, "turn_off", schedule)
    await clock(2)
    assert _state(hass, BOILER) == STATE_OFF
    await _switch(hass, "turn_on", schedule)
    await hass.async_block_till_done()
    assert _state(hass, BOILER) == STATE_ON


async def test_missed_start_runs_when_grid_returns_in_window(hass: HomeAssistant, clock) -> None:
    await _setup(hass)
    await _grid(hass, "0.0")
    await clock(1)  # window starts during the outage
    assert _state(hass, BOILER) == STATE_OFF
    await clock(3)
    await _grid(hass, "225")
    await clock.wait(30)
    assert _state(hass, BOILER) == STATE_OFF  # restore delay (60 s) not over yet
    await clock.wait(31)
    assert _state(hass, BOILER) == STATE_ON


async def test_missed_start_dropped_at_window_end(hass: HomeAssistant, clock) -> None:
    await _setup(hass)
    await _grid(hass, "0.0")
    await clock(1)
    await clock(7)
    await _grid(hass, "225")
    await clock.wait(61)
    assert _state(hass, BOILER) == STATE_OFF


async def test_shed_in_window_not_restored_after_window(hass: HomeAssistant, clock) -> None:
    await _setup(hass)
    await clock(1)
    assert _state(hass, BOILER) == STATE_ON
    await _grid(hass, "0.0")
    assert _state(hass, BOILER) == STATE_OFF
    assert _state(hass, SHED) == "1"
    await clock(7)  # window ends during the outage: forgotten
    assert _state(hass, SHED) == "0"
    await _grid(hass, "225")
    await clock.wait(61)
    assert _state(hass, BOILER) == STATE_OFF


async def test_shed_in_window_restored_in_window(hass: HomeAssistant, clock) -> None:
    await _setup(hass)
    await clock(1)
    await _grid(hass, "0.0")
    await clock(2)
    await _grid(hass, "225")
    await clock.wait(61)
    assert _state(hass, BOILER) == STATE_ON


async def test_manual_off_in_window_is_respected(hass: HomeAssistant, clock) -> None:
    await _setup(hass)
    await clock(1)
    await _switch(hass, "turn_off", BOILER)
    await _grid(hass, "0.0")
    await _grid(hass, "225")
    await clock.wait(61)
    assert _state(hass, BOILER) == STATE_OFF


async def test_window_crossing_midnight(hass: HomeAssistant, clock) -> None:
    await _setup(hass, windows={BOILER: ("23:00:00", "02:00:00")})
    # The clock starts at 00:30, inside this window; pass its end first so the jump to
    # 23:00 doesn't fire a stale 02:00 trigger late (real time never jumps like that).
    await clock(2, 0, 1)
    await clock(23)
    assert _state(hass, BOILER) == STATE_ON
    await clock(1)
    assert _state(hass, BOILER) == STATE_ON
    await clock(2)
    assert _state(hass, BOILER) == STATE_OFF


async def test_missed_start_survives_reload(hass: HomeAssistant, clock) -> None:
    entry = await _setup(hass)
    await _grid(hass, "0.0")
    await clock(1)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await clock(2)
    await _grid(hass, "225")
    await clock.wait(61)
    assert _state(hass, BOILER) == STATE_ON


async def test_reconfigure_sets_and_clears_window(hass: HomeAssistant, clock) -> None:
    await setup_loads(hass)
    hass.states.async_set(VOLTAGE, "228")
    entry = make_entry(loads=(BOILER,))
    await setup_entry(hass, entry)
    subentry_id = next(iter(entry.subentries))
    ent_reg = er.async_get(hass)
    assert ent_reg.async_get_entity_id("switch", DOMAIN, f"{subentry_id}_run_on_schedule") is None

    async def reconfigure(data):
        result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.subentries.async_configure(result["flow_id"], data)
        await hass.async_block_till_done()
        return result

    result = await reconfigure({CONF_WINDOW_START: "01:00:00"})
    assert result["errors"] == {"base": "window_incomplete"}

    result = await reconfigure({CONF_WINDOW_START: "01:00:00", CONF_WINDOW_END: "07:00:00"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.subentries[subentry_id].data[CONF_WINDOW_END] == "07:00:00"
    assert ent_reg.async_get_entity_id("switch", DOMAIN, f"{subentry_id}_run_on_schedule")
    await clock(1)
    assert _state(hass, BOILER) == STATE_ON

    result = await reconfigure({})
    assert result["reason"] == "reconfigure_successful"
    assert CONF_WINDOW_START not in entry.subentries[subentry_id].data
    await clock(7)
    assert _state(hass, BOILER) == STATE_ON  # no window any more


async def test_add_load_with_window(hass: HomeAssistant) -> None:
    await setup_loads(hass)
    hass.states.async_set(VOLTAGE, "228")
    entry = make_entry(loads=())
    await setup_entry(hass, entry)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_LOAD), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_SWITCH_ENTITY: BOILER, CONF_WINDOW_START: "01:00:00", CONF_WINDOW_END: "01:00:00"},
    )
    assert result["errors"] == {"base": "window_empty"}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_SWITCH_ENTITY: BOILER, CONF_WINDOW_START: "01:00:00", CONF_WINDOW_END: "07:00:00"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    load = next(iter(entry.runtime_data.shedder.loads.values()))
    assert load.window == (time(1), time(7))
