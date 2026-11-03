"""Battery levels and time left, from a single percentage sensor."""

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.grid_load_shedding.battery import BatteryMonitor
from custom_components.grid_load_shedding.const import (
    BATTERY_LEVELS,
    CONF_BATTERY_ENTITY,
    CONF_BATTERY_RESERVE,
    CONF_BATTERY_VOLUME,
    CONF_VOLUME,
    CONF_WINDOW_END,
    CONF_WINDOW_START,
    DOMAIN,
)

from .conftest import VOLTAGE, make_entry, setup_entry
from .test_announcer import ANNOUNCEMENT, PLAYER, Player, at

BATTERY = "sensor.phone_battery"
RUNTIME = "sensor.grid_battery_runtime"
SOUNDS = {
    f"sound_{level}": {"media_content_id": f"media-source://x/{level}.mp3", "media_content_type": "audio/mpeg"}
    for level in BATTERY_LEVELS
}


def monitor(hass: HomeAssistant, reserve: float = 0) -> tuple[BatteryMonitor, list[str]]:
    levels: list[str] = []
    return BatteryMonitor(hass, "e", entity_id=BATTERY, reserve=reserve, on_level=levels.append), levels


async def start(hass: HomeAssistant, soc: float, reserve: float = 0) -> tuple[BatteryMonitor, list[str]]:
    hass.states.async_set(BATTERY, str(soc))
    battery, levels = monitor(hass, reserve)
    await battery.async_load()
    battery.async_start()
    return battery, levels


async def discharge(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, soc: float, to: float, step: float = 1, per_hour: float = 12
) -> float:
    """Walk the sensor from ``soc`` down to ``to``."""
    seconds = step / per_hour * 3600
    while soc > to:
        soc = round(soc - step, 1)
        freezer.tick(timedelta(seconds=seconds))
        hass.states.async_set(BATTERY, str(soc))
        await hass.async_block_till_done()
    return soc


@pytest.mark.parametrize("step", [0.1, 1])
async def test_rate_is_independent_of_the_sensor_step(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, step: float
) -> None:
    battery, _ = await start(hass, 90)
    await discharge(hass, freezer, 90, 80, step=step, per_hour=12)
    assert battery.median_rate == pytest.approx(12, rel=0.02)
    assert battery.runtime_minutes == pytest.approx(400, rel=0.02)
    battery.async_stop()


async def test_needs_three_segments(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    battery, _ = await start(hass, 90)
    await discharge(hass, freezer, 90, 88)
    assert battery.median_rate is None and battery.runtime_minutes is None
    await discharge(hass, freezer, 88, 87)
    assert battery.median_rate == pytest.approx(12)
    battery.async_stop()


async def test_median_ignores_a_burst(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    battery, _ = await start(hass, 90)
    soc = await discharge(hass, freezer, 90, 87)
    soc = await discharge(hass, freezer, soc, soc - 1, per_hour=120)  # a kettle
    await discharge(hass, freezer, soc, soc - 1)
    assert battery.median_rate == pytest.approx(12)
    battery.async_stop()


async def test_old_segments_expire(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    battery, _ = await start(hass, 90)
    await discharge(hass, freezer, 90, 80)
    assert battery.median_rate is not None
    freezer.tick(timedelta(minutes=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert battery.median_rate is None
    battery.async_stop()


async def test_idle_stretch_is_not_a_slow_segment(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    battery, _ = await start(hass, 100)
    freezer.tick(timedelta(hours=10))
    await discharge(hass, freezer, 100, 96)
    assert min(rate for _, rate in battery._segments) == pytest.approx(12)
    battery.async_stop()


async def test_reserve_shortens_the_runtime(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    battery, _ = await start(hass, 60, reserve=15)
    await discharge(hass, freezer, 60, 55)
    assert battery.runtime_minutes == pytest.approx((55 - 15) / 12 * 60)
    hass.states.async_set(BATTERY, "14")
    await hass.async_block_till_done()
    assert battery.runtime_minutes == 0
    battery.async_stop()


async def test_each_level_once_in_order(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    battery, levels = await start(hass, 60)
    await discharge(hass, freezer, 60, 1, per_hour=18)
    assert levels == ["soc_50", "runtime_120", "soc_25", "runtime_60", "runtime_30", "runtime_10"]
    battery.async_stop()


async def test_jump_to_zero_says_only_the_most_urgent(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    battery, levels = await start(hass, 60)
    await discharge(hass, freezer, 60, 51)
    hass.states.async_set(BATTERY, "0")
    await hass.async_block_till_done()
    assert levels == ["runtime_10"]
    assert battery.passed == set(BATTERY_LEVELS)
    battery.async_stop()


async def test_charging_is_silent(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    battery, levels = await start(hass, 10)
    for soc in (12, 20, 30):
        freezer.tick(60)
        hass.states.async_set(BATTERY, str(soc))
        await hass.async_block_till_done()
    assert levels == []
    battery.async_stop()


async def test_unavailable_changes_nothing(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    battery, levels = await start(hass, 51)
    hass.states.async_set(BATTERY, "unavailable")
    await hass.async_block_till_done()
    assert battery.soc == 51 and levels == []
    hass.states.async_set(BATTERY, "49.5")
    await hass.async_block_till_done()
    assert levels == ["soc_50"]
    battery.async_stop()


async def test_grid_blink_does_not_repeat(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    battery, levels = await start(hass, 26)
    await discharge(hass, freezer, 26, 24)
    assert levels.count("soc_25") == 1
    hass.states.async_set(BATTERY, "27")  # charged for a moment
    await hass.async_block_till_done()
    await discharge(hass, freezer, 27, 24)
    assert levels.count("soc_25") == 1
    battery.async_stop()


async def test_real_charge_rearms(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    battery, levels = await start(hass, 60)
    await discharge(hass, freezer, 60, 1, per_hour=18)
    assert "runtime_10" in levels and "soc_50" in levels
    levels.clear()
    hass.states.async_set(BATTERY, "70")
    await hass.async_block_till_done()
    assert battery.median_rate is None  # the old rate is gone with the charge
    await discharge(hass, freezer, 70, 1, per_hour=18)
    assert levels == ["soc_50", "runtime_120", "soc_25", "runtime_60", "runtime_30", "runtime_10"]
    battery.async_stop()


async def test_passed_survive_a_restart(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    battery, levels = await start(hass, 51)
    await discharge(hass, freezer, 51, 45)
    assert levels == ["soc_50"]
    battery.async_stop()
    await battery.async_flush()

    again, levels = await start(hass, 45)
    await discharge(hass, freezer, 45, 40)
    assert levels == []
    again.async_stop()


async def setup(hass: HomeAssistant, soc: float, **announcement):
    hass.states.async_set(PLAYER, "idle")
    hass.states.async_set(VOLTAGE, "228")
    hass.states.async_set(BATTERY, str(soc))
    player = Player(hass)
    entry = make_entry(
        options={CONF_BATTERY_ENTITY: BATTERY, CONF_BATTERY_RESERVE: 0},
        loads=(),
        announcements=({**ANNOUNCEMENT, **SOUNDS, CONF_BATTERY_VOLUME: 70, **announcement},),
    )
    await setup_entry(hass, entry)
    return entry, player


async def test_plays_at_the_battery_volume(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    at(freezer, 14)
    _, player = await setup(hass, 51)
    await discharge(hass, freezer, 51, 49)
    assert player.played == [SOUNDS["sound_soc_50"]["media_content_id"]]
    assert player.calls[0]["service_data"]["volume_level"] == 0.7


async def test_grid_sound_keeps_its_volume(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    at(freezer, 14)
    _, player = await setup(hass, 90)
    hass.states.async_set(VOLTAGE, "0")
    await hass.async_block_till_done()
    assert player.calls[0]["service_data"]["volume_level"] == 0.4


async def test_outside_the_window_stays_silent(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    at(freezer, 3)
    entry, player = await setup(hass, 51)
    await discharge(hass, freezer, 51, 49)
    assert player.calls == []
    assert "soc_50" in entry.runtime_data.battery.passed


async def test_runtime_sensor(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    at(freezer, 14)
    await setup(hass, 90)
    await hass.async_block_till_done()
    assert hass.states.get(RUNTIME).state == "unknown"
    await discharge(hass, freezer, 90, 84)
    state = hass.states.get(RUNTIME)
    assert state.state == "420"
    assert state.attributes["unit_of_measurement"] == "min"
    assert state.attributes["median_rate"] == 12


async def test_test_button_uses_the_battery_volume(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    at(freezer, 3)
    entry, player = await setup(hass, 90)
    subentry_id = next(iter(entry.subentries))
    button = er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{subentry_id}_test_sound_runtime_10")
    await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: button}, blocking=True)
    assert player.played == [SOUNDS["sound_runtime_10"]["media_content_id"]]
    assert player.calls[0]["service_data"]["volume_level"] == 0.7


async def test_without_a_battery_nothing_is_added(hass: HomeAssistant) -> None:
    hass.states.async_set(PLAYER, "idle")
    hass.states.async_set(VOLTAGE, "228")
    entry = make_entry(loads=(), announcements=(ANNOUNCEMENT,))
    await setup_entry(hass, entry)
    assert entry.runtime_data.battery is None
    assert hass.states.get(RUNTIME) is None
    subentry_id = next(iter(entry.subentries))
    assert not er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{subentry_id}_test_sound_soc_50")


async def test_options_flow_sets_the_battery(hass: HomeAssistant) -> None:
    hass.states.async_set(VOLTAGE, "228")
    hass.states.async_set(BATTERY, "80", {"device_class": "battery"})
    entry = make_entry(loads=())
    await setup_entry(hass, entry)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "voltage_entity": VOLTAGE,
            "threshold": 170,
            "hold_seconds": 30,
            CONF_BATTERY_ENTITY: BATTERY,
            CONF_BATTERY_RESERVE: 15,
        },
    )
    await hass.async_block_till_done()
    assert entry.runtime_data.battery.entity_id == BATTERY
    assert entry.runtime_data.battery.reserve == 15


async def test_announcement_flow_stores_battery_sounds(hass: HomeAssistant) -> None:
    hass.states.async_set(PLAYER, "idle")
    entry, _ = await setup(hass, 90)
    sub = next(iter(entry.subentries.values()))
    result = await entry.start_subentry_reconfigure_flow(hass, sub.subentry_id)
    sound = {**SOUNDS["sound_soc_25"], "metadata": {"title": "x"}}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"sound_soc_25": sound, CONF_VOLUME: 40, CONF_BATTERY_VOLUME: 20, "enabled": True}
    )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    announcement = next(iter(entry.runtime_data.announcer.announcements.values()))
    assert announcement.battery_volume == 0.2 and announcement.volume == 0.4
    assert announcement.sounds == {"soc_25": SOUNDS["sound_soc_25"]}
    assert CONF_WINDOW_START not in next(iter(entry.subentries.values())).data
    assert CONF_WINDOW_END not in next(iter(entry.subentries.values())).data
