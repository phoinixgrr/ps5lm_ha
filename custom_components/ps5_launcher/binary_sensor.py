"""Model ready: the PS5LM health URL answers."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import PS5ConfigEntry
from .entity import PS5Entity


async def async_setup_entry(hass: HomeAssistant, entry: PS5ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data.coordinator
    if coordinator.client.ready_url:
        async_add_entities([PS5AppReady(coordinator)])


class PS5AppReady(PS5Entity, BinarySensorEntity):
    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "app_ready", "Model ready")

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.data and self.coordinator.data.get("app_ready"))
