"""Config and options flow."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import PS5ConfigEntry, api_url_for
from .const import (
    CONF_API_URL,
    CONF_CONSOLE_INTERVAL,
    CONF_CONSOLE_METRICS,
    CONF_EXPORTER_PORT,
    CONF_LLM_METRICS,
    CONF_MORNING,
    CONF_READY_URL,
    CONF_REGIST_KEY,
    CONF_SCAN_INTERVAL,
    CONF_TITLE_ID,
    CONF_WEB_PORT,
    DEFAULT_CONSOLE_INTERVAL,
    DEFAULT_CONSOLE_METRICS,
    DEFAULT_EXPORTER_PORT,
    DEFAULT_LLM_METRICS,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TITLE_ID,
    DEFAULT_WEB_PORT,
    DOMAIN,
    MIN_CONSOLE_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .ps5client import discover, parse_credentials

_PORT = NumberSelector(NumberSelectorConfig(min=1, max=65535, step=1, mode=NumberSelectorMode.BOX))
_SECRET = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
_URL = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))


def _settings_schema(d: dict[str, Any]) -> dict:
    """Everything except host and keys. `d` holds the current or default values."""
    return {
        vol.Required(CONF_TITLE_ID, default=d.get(CONF_TITLE_ID, DEFAULT_TITLE_ID)): str,
        vol.Required(CONF_API_URL, default=d[CONF_API_URL]): _URL,
        vol.Required(CONF_WEB_PORT, default=int(d.get(CONF_WEB_PORT, DEFAULT_WEB_PORT))): _PORT,
        vol.Required(CONF_EXPORTER_PORT, default=int(d.get(CONF_EXPORTER_PORT, DEFAULT_EXPORTER_PORT))): _PORT,
        vol.Required(CONF_SCAN_INTERVAL, default=int(d.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))): NumberSelector(
            NumberSelectorConfig(min=MIN_SCAN_INTERVAL, max=600, step=5, unit_of_measurement="s", mode=NumberSelectorMode.BOX)
        ),
        vol.Required(CONF_LLM_METRICS, default=bool(d.get(CONF_LLM_METRICS, DEFAULT_LLM_METRICS))): bool,
        vol.Required(CONF_CONSOLE_METRICS, default=bool(d.get(CONF_CONSOLE_METRICS, DEFAULT_CONSOLE_METRICS))): bool,
        vol.Required(CONF_CONSOLE_INTERVAL, default=int(d.get(CONF_CONSOLE_INTERVAL, DEFAULT_CONSOLE_INTERVAL))): NumberSelector(
            NumberSelectorConfig(min=MIN_CONSOLE_INTERVAL, max=3600, step=30, unit_of_measurement="s", mode=NumberSelectorMode.BOX)
        ),
    }


def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
    """Number selectors return floats; store whole numbers."""
    out = dict(user_input)
    for key in (CONF_WEB_PORT, CONF_EXPORTER_PORT, CONF_SCAN_INTERVAL, CONF_CONSOLE_INTERVAL):
        if key in out:
            out[key] = int(out[key])
    out[CONF_API_URL] = out[CONF_API_URL].strip().rstrip("/")
    out.pop(CONF_READY_URL, None)
    return out


class PS5ConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._base: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 1: the console and its Remote Play keys."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            try:
                parse_credentials(user_input[CONF_REGIST_KEY], user_input[CONF_MORNING])
            except ValueError:
                errors["base"] = "invalid_credentials"
            else:
                if await self.hass.async_add_executor_job(discover, host) is None:
                    errors["base"] = "cannot_connect"
                else:
                    await self.async_set_unique_id(host.lower())
                    self._abort_if_unique_id_configured()
                    self._base = {**user_input, CONF_HOST: host}
                    return await self.async_step_settings()
        schema = vol.Schema({vol.Required(CONF_HOST): str, vol.Required(CONF_REGIST_KEY): _SECRET, vol.Required(CONF_MORNING): _SECRET})
        return self.async_show_form(step_id="user", data_schema=self.add_suggested_values_to_schema(schema, user_input), errors=errors)

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 2: PS5LM and metrics settings, prefilled with the defaults for this host."""
        if user_input is not None:
            return self.async_create_entry(title=f"PS5LM ({self._base[CONF_HOST]})", data={**self._base, **_clean(user_input)})
        defaults = {CONF_API_URL: api_url_for(self._base)}
        return self.async_show_form(step_id="settings", data_schema=vol.Schema(_settings_schema(defaults)))

    @staticmethod
    def async_get_options_flow(config_entry: PS5ConfigEntry) -> OptionsFlow:
        return PS5OptionsFlow()


class PS5OptionsFlow(OptionsFlow):
    """Change every setting except the host. Prefilled with the current values (defaults where never set)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        current = {**self.config_entry.data, **self.config_entry.options}
        current[CONF_API_URL] = api_url_for(current)
        if user_input is not None:
            try:
                parse_credentials(user_input[CONF_REGIST_KEY], user_input[CONF_MORNING])
            except ValueError:
                errors["base"] = "invalid_credentials"
            else:
                return self.async_create_entry(data=_clean(user_input))
            current = {**current, **user_input}
        schema = vol.Schema(
            {
                **_settings_schema(current),
                vol.Required(CONF_REGIST_KEY, default=current.get(CONF_REGIST_KEY, "")): _SECRET,
                vol.Required(CONF_MORNING, default=current.get(CONF_MORNING, "")): _SECRET,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
