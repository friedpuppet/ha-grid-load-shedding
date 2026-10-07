"""Fixtures for Grid Load Shedding tests."""

from __future__ import annotations

from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.grid_load_shedding.const import (
    CONF_FALLBACK_ENTITY,
    CONF_HOLD_SECONDS,
    CONF_SWITCH_ENTITY,
    CONF_THRESHOLD,
    CONF_VOLTAGE_ENTITY,
    DOMAIN,
    SUBENTRY_LOAD,
)

VOLTAGE = "sensor.grid_voltage"
FALLBACK = "binary_sensor.grid_ping"
GRID = "binary_sensor.grid_grid"
SHED = "sensor.grid_shed_loads"
DELAY = "number.grid_restore_delay"
RESTORE_NOW = "button.grid_restore_now"
LOADS = ("switch.kettle", "switch.boiler", "switch.fridge")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


async def setup_loads(hass: HomeAssistant) -> None:
    """Optimistic template switches standing in for smart plugs."""
    assert await async_setup_component(
        hass,
        "template",
        {
            "template": [
                {
                    "switch": [
                        {"name": name.split(".")[1], "optimistic": True, "turn_on": [], "turn_off": []}
                        for name in LOADS
                    ]
                }
            ]
        },
    )
    await hass.async_block_till_done()


def make_entry(options: dict[str, Any] | None = None, loads: tuple[str, ...] = LOADS) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Grid",
        data={},
        options={
            CONF_VOLTAGE_ENTITY: VOLTAGE,
            CONF_THRESHOLD: 170,
            CONF_HOLD_SECONDS: 30,
            CONF_FALLBACK_ENTITY: FALLBACK,
            **(options or {}),
        },
        subentries_data=[
            ConfigSubentryData(
                data={CONF_SWITCH_ENTITY: load},
                subentry_type=SUBENTRY_LOAD,
                title=load,
                unique_id=load,
            )
            for load in loads
        ],
    )


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def flag_entity(hass: HomeAssistant, entry: MockConfigEntry, load: str) -> str:
    """entity_id of the "shed on grid loss" switch for a load."""
    subentry_id = next(s.subentry_id for s in entry.subentries.values() if s.data[CONF_SWITCH_ENTITY] == load)
    entity_id = er.async_get(hass).async_get_entity_id("switch", DOMAIN, f"{subentry_id}_shed")
    assert entity_id
    return entity_id
