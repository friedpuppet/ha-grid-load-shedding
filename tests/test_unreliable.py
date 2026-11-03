"""Loads that drop commands or go unavailable (Zigbee plugs behind the inverter)."""

import pytest

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.grid_load_shedding import shedder as shedder_mod
from custom_components.grid_load_shedding.shedder import Shedder

from .conftest import SHED
from .test_shedder import _grid, _setup, _state, _switch, later


@pytest.fixture(autouse=True)
def no_retry_pause(monkeypatch):
    monkeypatch.setattr(shedder_mod, "RETRY_DELAYS", (0.0, 0.0))


@pytest.fixture
def failing(monkeypatch):
    """entity_id -> how many more calls fail (-1: all of them)."""
    fails: dict[str, int] = {}
    calls: list[tuple[str, str]] = []
    original = Shedder._call

    async def _call(self, service, entity_id):
        calls.append((service, entity_id))
        left = fails.get(entity_id, 0)
        if left:
            fails[entity_id] = left - 1
            raise HomeAssistantError("Failed to send request: MAC_NO_ACK")
        await original(self, service, entity_id)

    monkeypatch.setattr(Shedder, "_call", _call)
    fails["calls"] = calls  # type: ignore[assignment]
    return fails


def _shed_list(hass: HomeAssistant) -> list[str]:
    return hass.states.get(SHED).attributes["entities"]


async def test_lost_command_is_retried(hass: HomeAssistant, failing) -> None:
    await _setup(hass)
    failing["switch.kettle"] = 2
    await _grid(hass, "0.0")
    assert await _state(hass, "switch.kettle") == STATE_OFF
    assert failing["calls"].count(("turn_off", "switch.kettle")) == 3


async def test_failing_load_stays_pending_and_others_switch(hass: HomeAssistant, failing) -> None:
    entry = await _setup(hass)
    from .conftest import flag_entity

    await _switch(hass, "turn_on", flag_entity(hass, entry, "switch.fridge"))
    failing["switch.kettle"] = -1
    await _grid(hass, "0.0")
    assert await _state(hass, "switch.fridge") == STATE_OFF
    assert await _state(hass, "switch.kettle") == STATE_ON
    assert sorted(_shed_list(hass)) == ["switch.fridge", "switch.kettle"]

    failing["switch.kettle"] = 0  # the plug answers again
    later(hass, 61)
    assert await _state(hass, "switch.kettle") == STATE_OFF

    await _grid(hass, "225")
    later(hass, 61)
    assert await _state(hass, "switch.kettle") == STATE_ON
    assert await _state(hass, "switch.fridge") == STATE_ON
    assert await _state(hass, SHED) == "0"


async def test_load_that_comes_back_on_during_outage_is_shed(hass: HomeAssistant) -> None:
    await _setup(hass)
    hass.states.async_set("switch.kettle", STATE_UNAVAILABLE)  # its power is gone
    await _grid(hass, "0.0")
    assert await _state(hass, SHED) == "0"

    hass.states.async_set("switch.kettle", STATE_ON)  # power back, the plug restores "on"
    assert await _state(hass, "switch.kettle") == STATE_OFF
    assert _shed_list(hass) == ["switch.kettle"]

    await _grid(hass, "225")
    later(hass, 61)
    assert await _state(hass, "switch.kettle") == STATE_ON


async def test_shed_load_back_on_after_power_loss_is_shed_again(hass: HomeAssistant) -> None:
    await _setup(hass)
    await _grid(hass, "0.0")
    assert await _state(hass, "switch.kettle") == STATE_OFF
    hass.states.async_set("switch.kettle", STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    hass.states.async_set("switch.kettle", STATE_ON)
    assert await _state(hass, "switch.kettle") == STATE_OFF
    assert _shed_list(hass) == ["switch.kettle"]


async def test_manual_on_during_outage_is_respected(hass: HomeAssistant) -> None:
    await _setup(hass)
    await _grid(hass, "0.0")
    await _switch(hass, "turn_on", "switch.kettle")
    assert await _state(hass, "switch.kettle") == STATE_ON


async def test_unavailable_load_stays_shed_until_back(hass: HomeAssistant) -> None:
    await _setup(hass)
    await _grid(hass, "0.0")
    hass.states.async_set("switch.kettle", STATE_UNAVAILABLE)
    await _grid(hass, "225")
    later(hass, 61)
    await hass.async_block_till_done()
    assert _shed_list(hass) == ["switch.kettle"]

    hass.states.async_set("switch.kettle", STATE_OFF)  # back, but off
    assert await _state(hass, "switch.kettle") == STATE_ON
    assert await _state(hass, SHED) == "0"


async def test_failed_restore_not_resent_after_new_outage(hass: HomeAssistant, failing) -> None:
    await _setup(hass)
    await _grid(hass, "0.0")
    failing["switch.kettle"] = -1
    await _grid(hass, "225")
    later(hass, 61)
    assert await _state(hass, "switch.kettle") == STATE_OFF
    assert _shed_list(hass) == ["switch.kettle"]

    await _grid(hass, "0.0")  # grid gone again before the plug answered
    failing["switch.kettle"] = 0
    later(hass, 200)
    assert await _state(hass, "switch.kettle") == STATE_OFF
    assert _shed_list(hass) == ["switch.kettle"]


async def test_failed_shed_not_resent_after_grid_returns(hass: HomeAssistant, failing) -> None:
    await _setup(hass)
    failing["switch.kettle"] = -1
    await _grid(hass, "0.0")
    assert await _state(hass, "switch.kettle") == STATE_ON
    await _grid(hass, "225")  # grid back before the plug answered
    failing["switch.kettle"] = 0
    sent = len(failing["calls"])
    later(hass, 61)
    later(hass, 200)
    assert await _state(hass, "switch.kettle") == STATE_ON
    assert ("turn_off", "switch.kettle") not in failing["calls"][sent:]
    assert await _state(hass, SHED) == "0"
