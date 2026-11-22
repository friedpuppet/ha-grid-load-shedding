"""Battery state and sessions from the power, the time left and the time to full."""

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.grid_load_shedding.const import (
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_ENTITY,
    CONF_BATTERY_FLOOR_ENTITY,
    CONF_BATTERY_POWER_ENTITY,
    CONF_FALLBACK_ENTITY,
)

from .conftest import VOLTAGE, make_entry, setup_entry

BATTERY = "sensor.inverter_battery"
POWER = "sensor.inverter_battery_power"
FLOOR = "number.inverter_shutdown_soc"
RUNTIME = "sensor.grid_battery_runtime"
SESSION = "sensor.grid_battery_session"
STATE = "sensor.grid_battery_state"
TO_FULL = "sensor.grid_battery_time_to_full"


def expected(soc: float, power: float, floor: float = 5, capacity: float = 16) -> float:
    return (soc - floor) / 100 * capacity * 1000 / power * 60


async def setup(hass: HomeAssistant, soc: float = 90, power: bool = True, watts: float = -500, **extra):
    hass.states.async_set(VOLTAGE, "228")  # on the grid all along: the grid doesn't matter here
    hass.states.async_set(BATTERY, str(soc))
    hass.states.async_set(POWER, str(watts))
    hass.states.async_set(FLOOR, "5")
    entry = make_entry(
        options={
            CONF_FALLBACK_ENTITY: None,
            CONF_BATTERY_ENTITY: BATTERY,
            CONF_BATTERY_FLOOR_ENTITY: FLOOR,
            CONF_BATTERY_CAPACITY: 16,
            **({CONF_BATTERY_POWER_ENTITY: POWER} if power else {}),
            **extra,
        },
        loads=(),
    )
    await setup_entry(hass, entry)
    return entry


async def run(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, minutes: float, watts: float, noise: float = 5
) -> None:
    """Hold ``watts`` for ``minutes``, a reading every 10 s with a little noise."""
    for i in range(round(minutes * 6)):
        hass.states.async_set(POWER, str(watts + (noise if i % 2 else -noise)))
        freezer.tick(timedelta(seconds=10))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


def runtime(hass: HomeAssistant) -> float:
    return float(hass.states.get(RUNTIME).state)


def state(hass: HomeAssistant) -> str:
    return hass.states.get(STATE).state


async def test_state_and_sessions_follow_the_power(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await setup(hass)
    assert state(hass) == "charging"
    charging_start = hass.states.get(SESSION).state
    await run(hass, freezer, 30, -500)
    start = dt_util.utcnow()
    await run(hass, freezer, 0.5, 650)  # two readings beyond 300 W switch at once
    assert state(hass) == "discharging"
    session = hass.states.get(SESSION)
    assert abs(dt_util.parse_datetime(session.state) - start) < timedelta(seconds=30)
    assert session.attributes["state"] == "discharging"
    assert session.attributes["previous_state"] == "charging"
    previous = dt_util.parse_datetime(session.attributes["previous_start"])
    assert abs(previous - dt_util.parse_datetime(charging_start)) < timedelta(seconds=1)
    assert session.attributes["previous_energy"] == pytest.approx(0.5 * 30 / 60, rel=0.03)
    since = dt_util.parse_datetime(hass.states.get(STATE).attributes["since"])
    assert abs(since - dt_util.parse_datetime(session.state)) < timedelta(seconds=1)


async def test_hysteresis(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await setup(hass, watts=650)
    await run(hass, freezer, 10, 650)
    await run(hass, freezer, 10, 80)  # between idle and discharging: stays
    assert state(hass) == "discharging"
    await run(hass, freezer, 10, 0, noise=45)  # float charge flickering around zero
    assert state(hass) == "idle"
    await run(hass, freezer, 60, 0, noise=45)
    assert state(hass) == "idle"
    assert hass.states.get(RUNTIME).state == "unknown"
    await run(hass, freezer, 0.5, -1200)  # a reversal is followed at once
    assert state(hass) == "charging"


async def test_short_spike_does_not_switch(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await setup(hass, watts=0)
    await run(hass, freezer, 10, 0, noise=20)
    hass.states.async_set(POWER, "900")  # one reading only
    await hass.async_block_till_done()
    await run(hass, freezer, 5, 0, noise=20)
    assert state(hass) == "idle"


async def test_steady_load(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await setup(hass, watts=650)
    await run(hass, freezer, 5, 650)  # the plain mean while warming up
    assert hass.states.get(RUNTIME).attributes["method"] == "power"
    assert runtime(hass) == pytest.approx(expected(90, 650), rel=0.02)
    await run(hass, freezer, 30, 650)
    assert runtime(hass) == pytest.approx(expected(90, 650), rel=0.01)
    assert hass.states.get(RUNTIME).attributes["smoothed_power"] == pytest.approx(650, abs=5)
    assert hass.states.get(TO_FULL).state == "unknown"


async def test_time_to_full(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await setup(hass, soc=60, watts=-1000)
    await run(hass, freezer, 30, -1000)
    assert float(hass.states.get(TO_FULL).state) == pytest.approx(40 / 100 * 16 * 60, rel=0.01)
    assert hass.states.get(RUNTIME).state == "unknown"


async def test_kettle_and_dip_are_ignored(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await setup(hass, watts=650)
    await run(hass, freezer, 60, 650)
    seen = []
    for minutes, watts in ((3, 2650), (20, 650), (4, 150), (20, 650)):
        for _ in range(minutes):
            await run(hass, freezer, 1, watts)
            seen.append(runtime(hass))
    assert max(seen) / min(seen) < 1.03


async def test_sustained_change_is_followed(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await setup(hass, watts=650)
    await run(hass, freezer, 60, 650)
    await run(hass, freezer, 30, 900)
    assert 700 < hass.states.get(RUNTIME).attributes["smoothed_power"] < 900
    await run(hass, freezer, 90, 900)
    assert hass.states.get(RUNTIME).attributes["smoothed_power"] == pytest.approx(900, rel=0.03)


async def test_measured_capacity(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await setup(hass, watts=600)
    soc = 90
    for _ in range(6):  # 1 % per 15 min at 600 W = 150 Wh per 1 % = 15 kWh
        await run(hass, freezer, 15, 600)
        soc -= 1
        hass.states.async_set(BATTERY, str(soc))
        await hass.async_block_till_done()
    assert hass.states.get(RUNTIME).attributes["measured_capacity"] == pytest.approx(15, abs=0.2)


async def test_session_and_average_survive_a_reload(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    entry = await setup(hass, watts=650)
    start = hass.states.get(SESSION).state
    await run(hass, freezer, 30, 650)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(SESSION).state == start
    assert state(hass) == "discharging"
    assert entry.runtime_data.battery.smoothed_power == pytest.approx(650, abs=5)
    await run(hass, freezer, 2, 650)
    assert runtime(hass) == pytest.approx(expected(90, 650), rel=0.01)


async def test_no_power_falls_back_to_the_percentage(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await setup(hass, watts=650)
    await run(hass, freezer, 10, 650)
    hass.states.async_set(POWER, "unavailable")
    await hass.async_block_till_done()
    rt = hass.states.get(RUNTIME)
    assert rt.attributes["method"] == "soc" and rt.state == "unknown"


async def test_without_a_power_sensor(hass: HomeAssistant) -> None:
    await setup(hass, power=False)
    assert hass.states.get(STATE) is None and hass.states.get(SESSION) is None and hass.states.get(TO_FULL) is None
    assert "method" not in hass.states.get(RUNTIME).attributes


async def test_floor_from_the_inverter(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await setup(hass, watts=650)
    await run(hass, freezer, 30, 650)
    assert runtime(hass) == pytest.approx(expected(90, 650), rel=0.01)
    assert hass.states.get(RUNTIME).attributes["floor"] == 5
    hass.states.async_set(FLOOR, "20")
    await hass.async_block_till_done()
    assert runtime(hass) == pytest.approx(expected(90, 650, floor=20), rel=0.01)  # at once, not on the next tick
    hass.states.async_set(FLOOR, "unavailable")
    await hass.async_block_till_done()
    assert hass.states.get(RUNTIME).attributes["floor"] == 20  # the last known one
