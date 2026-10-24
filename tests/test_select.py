"""The model dropdown re-reads the model folder on each scan, so an uploaded model shows up without a reload."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.ps5_launcher.select import PS5ModelSelect


def make_select(listings: list[list[str]], console_on: bool = True) -> PS5ModelSelect:
    """A select wired to a fake coordinator whose client answers each list_models call from `listings` in turn."""
    calls = iter(listings)
    coordinator = MagicMock()
    coordinator.config_entry.data = {"host": "192.0.2.10"}
    coordinator.console_is_on.return_value = console_on
    coordinator.client.list_models.side_effect = lambda: next(calls)
    coordinator.data = {}

    entity = PS5ModelSelect(coordinator)

    async def run_in_executor(func, *args):
        return func(*args)

    pending: list[asyncio.Task] = []
    entity.hass = SimpleNamespace(
        async_add_executor_job=run_in_executor,
        async_create_task=lambda coro: pending.append(asyncio.ensure_future(coro)),
    )
    entity.async_write_ha_state = MagicMock()
    entity._pending = pending
    return entity


async def refresh(entity: PS5ModelSelect) -> None:
    entity._maybe_load_models()
    await asyncio.gather(*entity._pending)
    entity._pending.clear()


async def test_new_upload_appears_on_the_next_scan():
    entity = make_select([["A", "B"], ["A", "B", "C"]])
    await refresh(entity)
    assert entity.options == ["A", "B"]
    await refresh(entity)
    assert entity.options == ["A", "B", "C"]
    assert entity.async_write_ha_state.call_count == 2


async def test_unchanged_folder_does_not_rewrite_state():
    entity = make_select([["A", "B"], ["A", "B"]])
    await refresh(entity)
    await refresh(entity)
    assert entity.async_write_ha_state.call_count == 1


async def test_failed_listing_keeps_the_last_list():
    entity = make_select([["A", "B"], []])
    await refresh(entity)
    await refresh(entity)
    assert entity.options == ["A", "B"]


async def test_no_listing_while_the_console_is_off():
    entity = make_select([["A"]], console_on=False)
    await refresh(entity)
    assert entity.options == []
    entity.coordinator.client.list_models.assert_not_called()
