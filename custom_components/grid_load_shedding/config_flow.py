"""Config, options and load-subentry flows."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er, selector

from .const import (
    CONF_FALLBACK_ENTITY,
    CONF_HOLD_SECONDS,
    CONF_SWITCH_ENTITY,
    CONF_THRESHOLD,
    CONF_VOLTAGE_ENTITY,
    DEFAULT_HOLD_SECONDS,
    DEFAULT_THRESHOLD,
    DOMAIN,
    SUBENTRY_LOAD,
)


def _options_schema(defaults: dict[str, Any]) -> vol.Schema:
    fallback = defaults.get(CONF_FALLBACK_ENTITY)
    return vol.Schema(
        {
            vol.Required(CONF_VOLTAGE_ENTITY, default=defaults.get(CONF_VOLTAGE_ENTITY, vol.UNDEFINED)): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class=SensorDeviceClass.VOLTAGE)
            ),
            vol.Required(CONF_THRESHOLD, default=defaults.get(CONF_THRESHOLD, DEFAULT_THRESHOLD)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=500, step=1, unit_of_measurement="V", mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_HOLD_SECONDS, default=defaults.get(CONF_HOLD_SECONDS, DEFAULT_HOLD_SECONDS)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=3600, step=1, unit_of_measurement="s", mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Optional(
                CONF_FALLBACK_ENTITY,
                description={"suggested_value": fallback} if fallback else None,
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="binary_sensor")),
        }
    )


class GridLoadSheddingConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create one entry per grid source."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._async_abort_entries_match({CONF_VOLTAGE_ENTITY: user_input[CONF_VOLTAGE_ENTITY]})
            name = user_input.pop(CONF_NAME)
            return self.async_create_entry(title=name, data={}, options=user_input)

        schema = vol.Schema({vol.Required(CONF_NAME, default="Grid"): selector.TextSelector()}).extend(
            _options_schema({}).schema
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return GridLoadSheddingOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_LOAD: LoadSubentryFlow}


class GridLoadSheddingOptionsFlow(OptionsFlow):
    """Change the grid-detection settings (the entry reloads afterwards)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(step_id="init", data_schema=_options_schema(dict(self.config_entry.options)))


class LoadSubentryFlow(ConfigSubentryFlow):
    """Add a switch to shed when the grid is lost."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        entry = self._get_entry()
        ent_reg = er.async_get(self.hass)
        errors: dict[str, str] = {}

        if user_input is not None:
            entity_id = user_input[CONF_SWITCH_ENTITY]
            reg_entry = ent_reg.async_get(entity_id)
            # Store the registry id (survives entity_id renames) when there is one.
            ref = reg_entry.id if reg_entry else entity_id
            if any(s.unique_id == ref for s in entry.subentries.values()):
                errors[CONF_SWITCH_ENTITY] = "already_configured"
            else:
                state = self.hass.states.get(entity_id)
                title = (state.name if state else None) or entity_id
                return self.async_create_entry(title=title, data={CONF_SWITCH_ENTITY: ref}, unique_id=ref)

        existing = [
            entity_id
            for s in entry.subentries.values()
            if s.subentry_type == SUBENTRY_LOAD
            and (entity_id := er.async_resolve_entity_id(ent_reg, s.data[CONF_SWITCH_ENTITY]))
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_SWITCH_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="switch", exclude_entities=existing)
                )
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
