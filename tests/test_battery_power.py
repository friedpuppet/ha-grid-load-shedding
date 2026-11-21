"""Battery session and the time left from smoothed power."""

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.grid_load_shedding.const import (
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_ENTITY,
    CONF_BATTERY_POWER_ENTITY,
    CONF_BATTERY_RESERVE,
    CONF_BATTERY_RESERVE_ENTITY,
    CONF_FALLBACK_ENTITY,
)

from .conftest import VOLTAGE, make_entry, setup_entry

BATTERY = "sensor.inverter_battery"
POWER = "sensor.inverter_battery_power"
RUNTIME = "sensor.grid_battery_runtime"
SESSION = "sensor.grid_battery_session"


def expected(soc: float, power: float, reserve: float = 20, capacity: float = 16) -> float:
    return (soc - reserve) / 100 * capacity * 1000 / power * 60


FLOOR = "sensor.inverter_discharge_floor"


async def setup(hass: HomeAssistant, soc: float = 90, power: bool = True, **extra):
    hass.states.async_set(VOLTAGE, "228")
    hass.states.async_set(BATTERY, str(soc))
    hass.states.async_set(POWER, "-500")  # charging on the grid
    entry = make_entry(
        options={
            CONF_FALLBACK_ENTITY: None,
            CONF_BATTERY_ENTITY: BATTERY,
            CONF_BATTERY_RESERVE: 20,
            CONF_BATTERY_CAPACITY: 16,
            **({CONF_BATTERY_POWER_ENTITY: POWER} if power else {}),
            **extra,
        },
        loads=(),
    )
    await setup_entry(hass, entry)
    return entry


async def grid_off(hass: HomeAssistant) -> None:
    hass.states.async_set(VOLTAGE, "0")
    await hass.async_block_till_done()


async def run(hass: HomeAssistant, freezer: FrozenDateTimeFactory, minutes: int, watts: float) -> None:
    """Hold ``watts`` for ``minutes``, a reading every 10 s with a little noise."""
    for i in range(minutes * 6):
        hass.states.async_set(POWER, str(watts + (5 if i % 2 else -5)))
        freezer.tick(timedelta(seconds=10))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


def runtime(hass: HomeAssistant) -> float:
    return float(hass.states.get(RUNTIME).state)


async def test_session_follows_the_grid(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await setup(hass)
    assert hass.states.get(SESSION).state == "unknown"
    start = dt_util.utcnow()
    await grid_off(hass)
    assert abs(dt_util.parse_datetime(hass.states.get(SESSION).state) - start) < timedelta(seconds=1)
    await run(hass, freezer, 60, 650)

    hass.states.async_set(VOLTAGE, "unavailable")  # the inverter out of sight: still the same session
    freezer.tick(timedelta(minutes=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(SESSION).state != "unknown"

    hass.states.async_set(VOLTAGE, "228")
    await hass.async_block_till_done()
    state = hass.states.get(SESSION)
    assert state.state == "unknown"
    assert dt_util.parse_datetime(state.attributes["previous_start"]) - start < timedelta(seconds=1)
    assert state.attributes["previous_energy"] == pytest.approx(0.65 * 65 / 60, rel=0.03)


async def test_steady_load(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await setup(hass)
    await grid_off(hass)
    await run(hass, freezer, 5, 650)  # the plain mean while warming up
    assert hass.states.get(RUNTIME).attributes["method"] == "power"
    assert runtime(hass) == pytest.approx(expected(90, 650), rel=0.02)
    await run(hass, freezer, 30, 650)
    assert runtime(hass) == pytest.approx(expected(90, 650), rel=0.01)
    assert hass.states.get(RUNTIME).attributes["smoothed_power"] == pytest.approx(650, abs=5)


async def test_kettle_and_dip_are_ignored(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await setup(hass)
    await grid_off(hass)
    await run(hass, freezer, 60, 650)
    seen = []
    for minutes, watts in ((3, 2650), (20, 650), (4, 150), (20, 650)):
        for _ in range(minutes):
            await run(hass, freezer, 1, watts)
            seen.append(runtime(hass))
    assert max(seen) / min(seen) < 1.03


async def test_sustained_change_is_followed(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await setup(hass)
    await grid_off(hass)
    await run(hass, freezer, 60, 650)
    await run(hass, freezer, 30, 900)
    assert 700 < hass.states.get(RUNTIME).attributes["smoothed_power"] < 900
    await run(hass, freezer, 90, 900)
    assert hass.states.get(RUNTIME).attributes["smoothed_power"] == pytest.approx(900, rel=0.03)


async def test_measured_capacity(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await setup(hass)
    await grid_off(hass)
    soc = 90
    for _ in range(6):  # 1 % per 15 min at 600 W = 150 Wh per 1 % = 15 kWh
        await run(hass, freezer, 15, 600)
        soc -= 1
        hass.states.async_set(BATTERY, str(soc))
        await hass.async_block_till_done()
    assert hass.states.get(RUNTIME).attributes["measured_capacity"] == pytest.approx(15, abs=0.2)


async def test_session_and_average_survive_a_reload(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    entry = await setup(hass)
    await grid_off(hass)
    start = hass.states.get(SESSION).state
    await run(hass, freezer, 30, 650)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(SESSION).state == start
    assert entry.runtime_data.battery.smoothed_power == pytest.approx(650, abs=5)
    await run(hass, freezer, 2, 650)
    assert runtime(hass) == pytest.approx(expected(90, 650), rel=0.01)


async def test_stale_power_falls_back_to_the_percentage(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await setup(hass)
    await grid_off(hass)
    await run(hass, freezer, 10, 650)
    hass.states.async_set(POWER, "unavailable")
    for _ in range(16):
        freezer.tick(timedelta(minutes=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    state = hass.states.get(RUNTIME)
    assert state.attributes["method"] == "soc" and state.state == "unknown"


async def test_on_grid_there_is_no_power_estimate(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await setup(hass)
    await run(hass, freezer, 10, -500)
    state = hass.states.get(RUNTIME)
    assert state.state == "unknown" and state.attributes["method"] == "soc"


async def test_without_a_power_sensor(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await setup(hass, power=False)
    await grid_off(hass)
    assert hass.states.get(SESSION).state != "unknown"
    assert "method" not in hass.states.get(RUNTIME).attributes
    assert "energy" not in hass.states.get(SESSION).attributes


async def test_reserve_from_an_entity(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """The inverter's actual floor moves with its mode; the fixed reserve is the fallback."""
    hass.states.async_set(FLOOR, "5")
    await setup(hass, **{CONF_BATTERY_RESERVE_ENTITY: FLOOR})
    await grid_off(hass)
    await run(hass, freezer, 30, 650)
    assert runtime(hass) == pytest.approx(expected(90, 650, reserve=5), rel=0.01)
    assert hass.states.get(RUNTIME).attributes["reserve"] == 5
    hass.states.async_set(FLOOR, "35")
    await hass.async_block_till_done()
    assert runtime(hass) == pytest.approx(expected(90, 650, reserve=35), rel=0.01)  # at once, not on the next tick
    hass.states.async_set(FLOOR, "unavailable")
    await hass.async_block_till_done()
    assert hass.states.get(RUNTIME).attributes["reserve"] == 20
