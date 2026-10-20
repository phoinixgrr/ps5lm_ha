"""Shared entity base."""

from __future__ import annotations

from homeassistant.const import CONF_HOST
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import PS5Coordinator
from .const import DOMAIN


class PS5Entity(CoordinatorEntity[PS5Coordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: PS5Coordinator, key: str, name: str) -> None:
        super().__init__(coordinator)
        host = coordinator.config_entry.data[CONF_HOST]
        self._attr_unique_id = f"{host}_{key}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, host)}, name=f"PS5LM ({host})", manufacturer="Sony", model="PlayStation 5 running PS5LM")
