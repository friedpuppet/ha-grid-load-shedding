"""Config, options and load-subentry flows."""

import pytest

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData

from custom_components.grid_load_shedding.const import (
    CONF_FALLBACK_ENTITY,
    CONF_HOLD_SECONDS,
    CONF_SWITCH_ENTITY,
    CONF_THRESHOLD,
    CONF_VOLTAGE_ENTITY,
    DOMAIN,
    SUBENTRY_LOAD,
)

from .conftest import FALLBACK, GRID, VOLTAGE, make_entry, setup_entry, setup_loads

USER_INPUT = {
    CONF_NAME: "Grid",
    CONF_VOLTAGE_ENTITY: VOLTAGE,
    CONF_THRESHOLD: 170,
    CONF_HOLD_SECONDS: 30,
    CONF_FALLBACK_ENTITY: FALLBACK,
}


async def test_user_flow_creates_entry(hass: HomeAssistant) -> None:
    hass.states.async_set(VOLTAGE, "228")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], dict(USER_INPUT))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Grid"
    assert result["options"][CONF_VOLTAGE_ENTITY] == VOLTAGE
    await hass.async_block_till_done()
    assert hass.states.get(GRID).state == "on"


async def test_same_voltage_sensor_aborts(hass: HomeAssistant) -> None:
    make_entry(loads=()).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], dict(USER_INPUT))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow_changes_threshold(hass: HomeAssistant) -> None:
    hass.states.async_set(VOLTAGE, "200")
    entry = make_entry(loads=())
    await setup_entry(hass, entry)
    assert hass.states.get(GRID).state == "on"

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_VOLTAGE_ENTITY: VOLTAGE, CONF_THRESHOLD: 210, CONF_HOLD_SECONDS: 30},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_THRESHOLD] == 210
    assert CONF_FALLBACK_ENTITY not in entry.options
    assert hass.states.get(GRID).state == "off"  # reloaded with the new threshold


async def test_add_load_subentry(hass: HomeAssistant) -> None:
    await setup_loads(hass)
    hass.states.async_set(VOLTAGE, "228")
    entry = make_entry(loads=())
    await setup_entry(hass, entry)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_LOAD), context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_SWITCH_ENTITY: "switch.kettle"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert len(entry.subentries) == 1
    load = next(iter(entry.runtime_data.shedder.loads.values()))
    assert (load.entity_id, load.window) == ("switch.kettle", None)

    # The same switch can't be added twice: the selector already excludes it.
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_LOAD), context={"source": SOURCE_USER}
    )
    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(
            result["flow_id"], {CONF_SWITCH_ENTITY: "switch.kettle"}
        )
