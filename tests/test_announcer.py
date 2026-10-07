"""Sound notifications on grid loss and return."""

from datetime import datetime

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.grid_load_shedding.const import (
    CONF_ENABLED,
    CONF_MEDIA_PLAYER,
    CONF_SOUND_LOST,
    CONF_SOUND_RESTORED,
    CONF_VOLUME,
    CONF_WINDOW_END,
    CONF_WINDOW_START,
    DOMAIN,
    SUBENTRY_ANNOUNCEMENT,
)

from .conftest import VOLTAGE, make_entry, setup_entry

PLAYER = "media_player.speaker"
LOST = {"media_content_id": "media-source://media_source/local/grid_lost.wav", "media_content_type": "audio/x-wav"}
RESTORED = {
    "media_content_id": "media-source://media_source/local/grid_restored.wav",
    "media_content_type": "audio/x-wav",
}
ANNOUNCEMENT = {
    CONF_MEDIA_PLAYER: PLAYER,
    CONF_SOUND_LOST: LOST,
    CONF_SOUND_RESTORED: RESTORED,
    CONF_VOLUME: 40,
    CONF_WINDOW_START: "11:00:00",
    CONF_WINDOW_END: "22:00:00",
    CONF_ENABLED: True,
}


class Player:
    """Recorded media_player service calls, in order."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.calls: list[ServiceCall] = []
        for service in ("volume_set", "play_media"):
            async_mock_service(hass, "media_player", service)
        hass.bus.async_listen("call_service", self._record)

    def _record(self, event) -> None:
        if event.data["domain"] == "media_player":
            self.calls.append(event.data)

    @property
    def played(self) -> list[str]:
        return [c["service_data"]["media_content_id"] for c in self.calls if c["service"] == "play_media"]


def at(freezer: FrozenDateTimeFactory, hour: int, minute: int = 0) -> None:
    freezer.move_to(datetime(2026, 10, 7, hour, minute, tzinfo=dt_util.get_default_time_zone()))


async def setup(hass: HomeAssistant, **announcement):
    hass.states.async_set(PLAYER, "idle")
    hass.states.async_set(VOLTAGE, "228")
    player = Player(hass)
    entry = make_entry(loads=(), announcements=({**ANNOUNCEMENT, **announcement},))
    await setup_entry(hass, entry)
    return entry, player


async def grid(hass: HomeAssistant, volts: str) -> None:
    hass.states.async_set(VOLTAGE, volts)
    await hass.async_block_till_done()


def entity(hass: HomeAssistant, entry, platform: str, key: str) -> str:
    subentry_id = next(iter(entry.subentries))
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{subentry_id}_{key}")
    assert entity_id
    return entity_id


async def test_lost_and_restored_in_window(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    at(freezer, 14)
    _, player = await setup(hass)
    assert player.calls == []  # startup is silent

    await grid(hass, "0")
    assert [c["service"] for c in player.calls] == ["volume_set", "play_media"]
    assert player.calls[0]["service_data"] == {ATTR_ENTITY_ID: PLAYER, "volume_level": 0.4}
    assert player.calls[1]["service_data"] == {ATTR_ENTITY_ID: PLAYER, **LOST}

    await grid(hass, "230")
    assert player.played == [LOST["media_content_id"], RESTORED["media_content_id"]]


@pytest.mark.parametrize(
    ("hour", "window", "plays"),
    [
        (23, None, True),
        (23, ("11:00:00", "22:00:00"), False),
        (10, ("11:00:00", "22:00:00"), False),
        (21, ("11:00:00", "22:00:00"), True),
        (2, ("22:00:00", "07:00:00"), True),
        (12, ("22:00:00", "07:00:00"), False),
    ],
)
async def test_window(hass: HomeAssistant, freezer: FrozenDateTimeFactory, hour, window, plays) -> None:
    at(freezer, hour)
    start, end = window or (None, None)
    _, player = await setup(hass, **{CONF_WINDOW_START: start, CONF_WINDOW_END: end})
    await grid(hass, "0")
    assert bool(player.played) is plays


async def test_switch_off_is_silent_and_live(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    at(freezer, 14)
    entry, player = await setup(hass)
    runtime = entry.runtime_data
    switch = entity(hass, entry, "switch", CONF_ENABLED)

    await hass.services.async_call("switch", "turn_off", {ATTR_ENTITY_ID: switch}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(switch).state == STATE_OFF
    assert entry.runtime_data is runtime  # applied live, no reload
    assert next(iter(entry.subentries.values())).data[CONF_ENABLED] is False

    await grid(hass, "0")
    assert player.calls == []


async def test_missing_sound_is_silent(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    at(freezer, 14)
    entry, player = await setup(hass)
    sub = next(iter(entry.subentries.values()))
    data = {k: v for k, v in sub.data.items() if k != CONF_SOUND_LOST}
    hass.config_entries.async_update_subentry(entry, sub, data=data)
    await hass.async_block_till_done()  # reloads

    await grid(hass, "0")
    assert player.calls == []
    await grid(hass, "230")
    assert player.played == [RESTORED["media_content_id"]]


async def test_unknown_transitions_are_silent(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    at(freezer, 14)
    _, player = await setup(hass)
    await grid(hass, STATE_UNAVAILABLE)  # hold, then unknown (no fallback in effect)
    freezer.tick(60)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    await grid(hass, "230")
    assert player.calls == []


async def test_test_buttons_ignore_window(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    at(freezer, 3)
    entry, player = await setup(hass)
    for key, sound in (("test_sound_lost", LOST), ("test_sound_restored", RESTORED)):
        await hass.services.async_call(
            "button", "press", {ATTR_ENTITY_ID: entity(hass, entry, "button", key)}, blocking=True
        )
        assert player.played[-1] == sound["media_content_id"]


async def test_add_and_reconfigure_flow(hass: HomeAssistant) -> None:
    hass.states.async_set(PLAYER, "idle")
    hass.states.async_set(VOLTAGE, "228")
    entry = make_entry(loads=())
    await setup_entry(hass, entry)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_ANNOUNCEMENT), context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_MEDIA_PLAYER: PLAYER,
            CONF_SOUND_LOST: {**LOST, "metadata": {"title": "x"}},
            CONF_VOLUME: 30,
            CONF_WINDOW_START: "11:00:00",
            CONF_WINDOW_END: "22:00:00",
            CONF_ENABLED: True,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    sub = next(iter(entry.subentries.values()))
    assert sub.data[CONF_SOUND_LOST] == LOST
    assert CONF_SOUND_RESTORED not in sub.data
    announcement = next(iter(entry.runtime_data.announcer.announcements.values()))
    assert announcement.volume == 0.3 and announcement.entity_id == PLAYER

    result = await entry.start_subentry_reconfigure_flow(hass, sub.subentry_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_SOUND_LOST: LOST, CONF_SOUND_RESTORED: RESTORED, CONF_VOLUME: 70, CONF_ENABLED: True},
    )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    announcement = next(iter(entry.runtime_data.announcer.announcements.values()))
    assert announcement.volume == 0.7
    assert announcement.window is None  # both times cleared: any time
    assert set(announcement.sounds) == {"lost", "restored"}

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_ANNOUNCEMENT), context={"source": SOURCE_USER}
    )
    # The same player can't be added twice: the selector already excludes it.
    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(
            result["flow_id"], {CONF_MEDIA_PLAYER: PLAYER, CONF_VOLUME: 50, CONF_ENABLED: True}
        )


async def test_flow_suggests_default_window(hass: HomeAssistant) -> None:
    hass.states.async_set(VOLTAGE, "228")
    entry = make_entry(loads=())
    await setup_entry(hass, entry)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_ANNOUNCEMENT), context={"source": SOURCE_USER}
    )
    suggested = {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
        if key.description and "suggested_value" in key.description
    }
    assert suggested[CONF_WINDOW_START] == "11:00:00"
    assert suggested[CONF_WINDOW_END] == "22:00:00"
