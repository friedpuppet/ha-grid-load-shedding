"""Per-load flags: subentry data is the source of truth, shared by the dialog and the switches."""

from pytest_homeassistant_custom_component.common import mock_restore_cache

from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType

from custom_components.grid_load_shedding.const import (
    CONF_RUN_ON_SCHEDULE,
    CONF_SHED_ON_GRID_LOSS,
    CONF_SWITCH_ENTITY,
    CONF_WINDOW_END,
    CONF_WINDOW_START,
)

from .conftest import VOLTAGE, flag_entity, make_entry, setup_entry, setup_loads

KETTLE = "switch.kettle"


def _subentry(entry, load):
    return next(s for s in entry.subentries.values() if s.data[CONF_SWITCH_ENTITY] == load)


async def _setup(hass: HomeAssistant, **kwargs):
    await setup_loads(hass)
    hass.states.async_set(VOLTAGE, "228")
    entry = make_entry(loads=(KETTLE,), **kwargs)
    await setup_entry(hass, entry)
    return entry


async def test_switch_writes_subentry_without_reload(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    runtime = entry.runtime_data
    flag = flag_entity(hass, entry, KETTLE)

    await hass.services.async_call("switch", "turn_off", {ATTR_ENTITY_ID: flag}, blocking=True)
    await hass.async_block_till_done()
    assert _subentry(entry, KETTLE).data[CONF_SHED_ON_GRID_LOSS] is False
    assert hass.states.get(flag).state == STATE_OFF
    assert entry.runtime_data is runtime  # applied live, no reload
    assert runtime.shedder.enabled[_subentry(entry, KETTLE).subentry_id] is False


async def test_dialog_changes_show_on_switches(hass: HomeAssistant) -> None:
    entry = await _setup(hass, windows={KETTLE: ("01:00:00", "07:00:00")})
    runtime = entry.runtime_data
    sub = _subentry(entry, KETTLE)

    result = await entry.start_subentry_reconfigure_flow(hass, sub.subentry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_SHED_ON_GRID_LOSS: False,
            CONF_RUN_ON_SCHEDULE: False,
            CONF_WINDOW_START: "01:00:00",
            CONF_WINDOW_END: "07:00:00",
        },
    )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.runtime_data is runtime  # same window: flags applied live
    assert hass.states.get(flag_entity(hass, entry, KETTLE)).state == STATE_OFF
    assert hass.states.get(flag_entity(hass, entry, KETTLE, "run_on_schedule")).state == STATE_OFF


async def test_migrates_flag_state_from_entity(hass: HomeAssistant) -> None:
    """v0.2.x kept the flags only in the switch entities; carry them over into the subentry."""
    mock_restore_cache(
        hass,
        [
            State("switch.shed_switch_kettle_on_grid_loss", STATE_OFF),
            State("switch.run_switch_kettle_on_schedule", STATE_OFF),
        ],
    )
    entry = await _setup(hass, windows={KETTLE: ("01:00:00", "07:00:00")})
    sub = _subentry(entry, KETTLE)
    assert sub.data[CONF_SHED_ON_GRID_LOSS] is False
    assert sub.data[CONF_RUN_ON_SCHEDULE] is False
    assert entry.runtime_data.shedder.enabled[sub.subentry_id] is False
    assert entry.runtime_data.shedder.scheduled[sub.subentry_id] is False
    assert hass.states.get("switch.shed_switch_kettle_on_grid_loss").state == STATE_OFF


async def test_new_flags_default_on(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    sub = _subentry(entry, KETTLE)
    assert sub.data[CONF_SHED_ON_GRID_LOSS] is True
    assert hass.states.get(flag_entity(hass, entry, KETTLE)).state == STATE_ON
