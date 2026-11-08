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
        data = self.coordinator.data or {}
        # A fleet worker answers on its RPC port but has no model: this stays off for it, because automations read it
        # as "the model API can be used, and may be closed when the job is done" (the idle rest mode, a job stop).
        return bool(data.get("app_ready")) and not data.get("worker")
