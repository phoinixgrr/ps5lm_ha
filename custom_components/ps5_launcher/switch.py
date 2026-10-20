"""Two switches: the console (wake / rest mode) and the PS5LM server (start / stop)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import _LOGGER, PS5ConfigEntry, PS5Coordinator
from .entity import PS5Entity


async def async_setup_entry(hass: HomeAssistant, entry: PS5ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities([PS5ConsoleSwitch(coordinator), PS5AppSwitch(coordinator)])


class _PS5Switch(PS5Entity, SwitchEntity):
    async def _background(self, coro) -> None:
        try:
            await coro
        except HomeAssistantError as err:
            _LOGGER.error("PS5 operation failed: %s", err)

    @property
    def extra_state_attributes(self) -> dict:
        return {"status": self.coordinator.status, "last_result": self.coordinator.last_result}


class PS5ConsoleSwitch(_PS5Switch):
    """On wakes the console from rest mode; off puts it in rest mode."""

    _attr_icon = "mdi:sony-playstation"

    def __init__(self, coordinator: PS5Coordinator) -> None:
        super().__init__(coordinator, "console", "Console")

    @property
    def is_on(self) -> bool:
        return self.coordinator.console_is_on()

    async def async_turn_on(self, **kwargs: Any) -> None:
        if self.coordinator.op_lock.locked():
            return
        self.hass.async_create_background_task(self._background(self.coordinator.async_console_on()), "ps5_console_on")
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_console_off()


class PS5AppSwitch(_PS5Switch):
    """On wakes the console if needed, signs in, starts PS5LM and waits for the model; off stops PS5LM (the console stays on)."""

    _attr_icon = "mdi:brain"

    def __init__(self, coordinator: PS5Coordinator) -> None:
        super().__init__(coordinator, "app", "Server")

    @property
    def is_on(self) -> bool:
        return self.coordinator.app_is_on()

    @property
    def extra_state_attributes(self) -> dict:
        return {**super().extra_state_attributes, "title_id": self.coordinator.client.title_id}

    async def async_turn_on(self, **kwargs: Any) -> None:
        if self.coordinator.op_lock.locked():
            return
        self.hass.async_create_background_task(self._background(self.coordinator.async_app_on()), "ps5_app_on")
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_app_off()
