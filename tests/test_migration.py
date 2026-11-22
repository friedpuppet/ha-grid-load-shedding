"""1.1 → 1.2: the battery and the sound notifications are dropped."""

from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.grid_load_shedding.const import DOMAIN, SUBENTRY_LOAD

from .conftest import VOLTAGE, make_entry, setup_entry


async def test_old_entry_is_cleaned(hass: HomeAssistant, hass_storage) -> None:
    hass.states.async_set(VOLTAGE, "228")
    entry = make_entry(
        options={
            "battery_entity": "sensor.battery",
            "battery_power_entity": "sensor.battery_power",
            "battery_capacity": 15,
            "battery_floor_entity": "number.shutdown_soc",
        },
        loads=("switch.kettle",),
        minor_version=1,
    )
    entry.add_to_hass(hass)
    sound = ConfigSubentry(
        data={"media_player": "media_player.speaker", "volume": 80},
        subentry_type="announcement",
        title="speaker",
        unique_id="media_player.speaker",
    )
    hass.config_entries.async_add_subentry(entry, sound)
    sound_id = next(s for s, sub in entry.subentries.items() if sub.subentry_type == "announcement")
    reg = er.async_get(hass)
    battery = reg.async_get_or_create("sensor", DOMAIN, f"{entry.entry_id}_battery_runtime", config_entry=entry)
    button = reg.async_get_or_create(
        "button", DOMAIN, f"{sound_id}_test_sound_lost", config_entry=entry, config_subentry_id=sound_id
    )
    hass_storage[f"{DOMAIN}.{entry.entry_id}.battery"] = {"version": 1, "data": {"passed": []}}

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.minor_version == 2
    assert not any(k.startswith("battery_") for k in entry.options)
    assert [sub.subentry_type for sub in entry.subentries.values()] == [SUBENTRY_LOAD]
    assert reg.async_get(battery.entity_id) is None
    assert reg.async_get(button.entity_id) is None
    assert f"{DOMAIN}.{entry.entry_id}.battery" not in hass_storage
    assert hass.states.get("sensor.grid_shed_loads") is not None  # the rest still works


async def test_current_entry_is_left_alone(hass: HomeAssistant) -> None:
    hass.states.async_set(VOLTAGE, "228")
    entry = make_entry(loads=("switch.kettle",))
    await setup_entry(hass, entry)
    assert entry.minor_version == 2 and len(entry.subentries) == 1
