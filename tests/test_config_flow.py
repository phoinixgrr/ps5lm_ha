"""Tests for the config and options flows."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from homeassistant.const import CONF_HOST
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ps5_launcher import api_url_for
from custom_components.ps5_launcher.config_flow import PS5ConfigFlow, PS5OptionsFlow
from custom_components.ps5_launcher.const import (
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
)

from .conftest import MORNING, REGIST_KEY

SETTINGS = {CONF_TITLE_ID: "PPSA99575", CONF_API_URL: "http://192.0.2.10:8081/", CONF_WEB_PORT: 8080.0, CONF_EXPORTER_PORT: 9100.0,
            CONF_SCAN_INTERVAL: 30.0, CONF_CONSOLE_INTERVAL: 120.0, CONF_LLM_METRICS: True, CONF_CONSOLE_METRICS: True}


@pytest.fixture
def flow():
    f = PS5ConfigFlow()
    f.hass = MagicMock()
    f.hass.async_add_executor_job = AsyncMock(return_value="standby")
    f.async_set_unique_id = AsyncMock()
    f._abort_if_unique_id_configured = MagicMock()
    return f


def defaults(result) -> dict:
    return {str(key): key.default() for key in result["data_schema"].schema if callable(getattr(key, "default", None))}


async def test_user_step_shows_form(flow):
    result = await flow.async_step_user()
    assert result["type"] == FlowResultType.FORM and result["step_id"] == "user"


async def test_user_step_rejects_bad_keys(flow):
    result = await flow.async_step_user({CONF_HOST: "192.0.2.10", CONF_REGIST_KEY: "nothex", CONF_MORNING: MORNING})
    assert result["errors"] == {"base": "invalid_credentials"}


async def test_user_step_needs_a_console(flow):
    flow.hass.async_add_executor_job = AsyncMock(return_value=None)
    result = await flow.async_step_user({CONF_HOST: "192.0.2.10", CONF_REGIST_KEY: REGIST_KEY, CONF_MORNING: MORNING})
    assert result["errors"] == {"base": "cannot_connect"}


async def test_full_setup_prefills_defaults_and_creates_entry(flow):
    result = await flow.async_step_user({CONF_HOST: " 192.0.2.10 ", CONF_REGIST_KEY: REGIST_KEY, CONF_MORNING: MORNING})
    assert result["step_id"] == "settings"
    prefilled = defaults(result)
    assert prefilled[CONF_API_URL] == "http://192.0.2.10:8081"
    assert prefilled[CONF_TITLE_ID] == "PPSA99575" and prefilled[CONF_WEB_PORT] == 8080 and prefilled[CONF_EXPORTER_PORT] == 9100
    assert prefilled[CONF_SCAN_INTERVAL] == 30 and prefilled[CONF_LLM_METRICS] and prefilled[CONF_CONSOLE_METRICS]
    assert prefilled[CONF_CONSOLE_INTERVAL] == 120

    result = await flow.async_step_settings(SETTINGS)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "PS5LM (192.0.2.10)"
    data = result["data"]
    assert data[CONF_HOST] == "192.0.2.10" and data[CONF_REGIST_KEY] == REGIST_KEY
    assert data[CONF_API_URL] == "http://192.0.2.10:8081"  # trailing slash removed
    assert data[CONF_WEB_PORT] == 8080 and isinstance(data[CONF_WEB_PORT], int)
    assert data[CONF_CONSOLE_INTERVAL] == 120 and isinstance(data[CONF_CONSOLE_INTERVAL], int)


async def test_options_prefill_from_an_older_entry():
    entry = SimpleNamespace(data={CONF_HOST: "192.0.2.10", CONF_REGIST_KEY: REGIST_KEY, CONF_MORNING: MORNING,
                                  CONF_READY_URL: "http://192.0.2.10:8081/health"}, options={})
    options_flow = PS5OptionsFlow()
    with patch.object(PS5OptionsFlow, "config_entry", new_callable=PropertyMock, return_value=entry):
        result = await options_flow.async_step_init()
        prefilled = defaults(result)
        assert prefilled[CONF_API_URL] == "http://192.0.2.10:8081"
        assert prefilled[CONF_REGIST_KEY] == REGIST_KEY and prefilled[CONF_MORNING] == MORNING

        bad = await options_flow.async_step_init({**SETTINGS, CONF_REGIST_KEY: "x", CONF_MORNING: MORNING})
        assert bad["errors"] == {"base": "invalid_credentials"}

        ok = await options_flow.async_step_init({**SETTINGS, CONF_REGIST_KEY: REGIST_KEY, CONF_MORNING: MORNING})
        assert ok["type"] == FlowResultType.CREATE_ENTRY and ok["data"][CONF_SCAN_INTERVAL] == 30


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({CONF_HOST: "h", CONF_API_URL: "http://x:1/"}, "http://x:1"),
        ({CONF_HOST: "h", CONF_READY_URL: "http://y:2/health"}, "http://y:2"),
        ({CONF_HOST: "h"}, "http://h:8081"),
    ],
)
def test_api_url_for(options, expected):
    assert api_url_for(options) == expected
