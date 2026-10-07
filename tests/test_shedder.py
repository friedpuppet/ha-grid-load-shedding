"""Shedding and restoring loads."""

from datetime import timedelta

from pytest_homeassistant_custom_component.common import async_capture_events, async_fire_time_changed

from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.grid_load_shedding.const import DOMAIN, EVENT_RESTORED, EVENT_SHED

from .conftest import DELAY, RESTORE_NOW, SHED, VOLTAGE, flag_entity, make_entry, setup_entry, setup_loads


def later(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))


async def _state(hass: HomeAssistant, entity_id: str) -> str:
    await hass.async_block_till_done()
    return hass.states.get(entity_id).state


async def _switch(hass: HomeAssistant, service: str, entity_id: str) -> None:
    await hass.services.async_call("switch", service, {ATTR_ENTITY_ID: entity_id}, blocking=True)


async def _setup(hass: HomeAssistant):
    """Kettle on, boiler off, fridge on but its shed flag off."""
    await setup_loads(hass)
    hass.states.async_set(VOLTAGE, "228")
    entry = make_entry()
    await setup_entry(hass, entry)
    await _switch(hass, "turn_on", "switch.kettle")
    await _switch(hass, "turn_off", "switch.boiler")
    await _switch(hass, "turn_on", "switch.fridge")
    await _switch(hass, "turn_off", flag_entity(hass, entry, "switch.fridge"))
    return entry


async def _grid(hass: HomeAssistant, volts: str) -> None:
    hass.states.async_set(VOLTAGE, volts)
    await hass.async_block_till_done()


async def test_flags_default_on_and_live_on_load_device(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    assert await _state(hass, flag_entity(hass, entry, "switch.kettle")) == STATE_ON
    assert await _state(hass, flag_entity(hass, entry, "switch.fridge")) == STATE_OFF


async def test_shed_then_restore_after_delay(hass: HomeAssistant) -> None:
    await _setup(hass)
    shed_events = async_capture_events(hass, EVENT_SHED)
    restored_events = async_capture_events(hass, EVENT_RESTORED)

    await _grid(hass, "0.0")
    assert await _state(hass, "switch.kettle") == STATE_OFF
    assert await _state(hass, "switch.boiler") == STATE_OFF
    assert await _state(hass, "switch.fridge") == STATE_ON  # flag off
    assert await _state(hass, SHED) == "1"
    assert hass.states.get(SHED).attributes["entities"] == ["switch.kettle"]
    assert shed_events[0].data[ATTR_ENTITY_ID] == ["switch.kettle"]

    await _grid(hass, "225")
    later(hass, 30)
    assert await _state(hass, "switch.kettle") == STATE_OFF  # default delay is 60 s
    later(hass, 61)
    assert await _state(hass, "switch.kettle") == STATE_ON
    assert await _state(hass, SHED) == "0"
    assert restored_events[0].data[ATTR_ENTITY_ID] == ["switch.kettle"]


async def test_grid_flaps_during_delay(hass: HomeAssistant) -> None:
    await _setup(hass)
    await _grid(hass, "0.0")
    await _grid(hass, "225")
    later(hass, 30)
    await _grid(hass, "0.0")
    later(hass, 120)
    assert await _state(hass, "switch.kettle") == STATE_OFF
    assert await _state(hass, SHED) == "1"


async def test_restore_delay_number(hass: HomeAssistant) -> None:
    await _setup(hass)
    await hass.services.async_call("number", "set_value", {ATTR_ENTITY_ID: DELAY, "value": 5}, blocking=True)
    await _grid(hass, "0.0")
    await _grid(hass, "225")
    later(hass, 6)
    assert await _state(hass, "switch.kettle") == STATE_ON


async def test_forget_service(hass: HomeAssistant) -> None:
    await _setup(hass)
    await _grid(hass, "0.0")
    await hass.services.async_call(DOMAIN, "forget", {ATTR_ENTITY_ID: "switch.kettle"}, blocking=True)
    assert await _state(hass, SHED) == "0"
    await _grid(hass, "225")
    later(hass, 61)
    assert await _state(hass, "switch.kettle") == STATE_OFF


async def test_restore_now_button(hass: HomeAssistant) -> None:
    await _setup(hass)
    await _grid(hass, "0.0")
    await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: RESTORE_NOW}, blocking=True)
    assert await _state(hass, "switch.kettle") == STATE_ON
    assert await _state(hass, SHED) == "0"


async def test_shed_list_survives_reload(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    await _grid(hass, "0.0")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert await _state(hass, SHED) == "1"
    # Reload during the outage must not re-shed or forget anything.
    assert await _state(hass, "switch.kettle") == STATE_OFF

    await _grid(hass, "225")
    later(hass, 61)
    assert await _state(hass, "switch.kettle") == STATE_ON
    assert await _state(hass, SHED) == "0"


async def test_reload_after_grid_returned_resumes_restore(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    await _grid(hass, "0.0")
    await _grid(hass, "225")
    assert await hass.config_entries.async_reload(entry.entry_id)
    later(hass, 61)
    assert await _state(hass, "switch.kettle") == STATE_ON
