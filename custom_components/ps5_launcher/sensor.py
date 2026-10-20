"""Status sensor plus the PS5LM (llama.cpp) and console (ps5-exporter) metrics as sensors."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfInformation, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import PS5ConfigEntry, PS5Coordinator
from .entity import PS5Entity


@dataclass(frozen=True, kw_only=True)
class PS5MetricDescription(SensorEntityDescription):
    """`key` is the key in the metrics dict that PS5Client.fetch_metrics returns."""


TOKENS_PER_SECOND = "tok/s"

METRICS: tuple[PS5MetricDescription, ...] = (
    # --- PS5LM (llama.cpp /metrics and /props) ---
    PS5MetricDescription(key="model", name="Model", icon="mdi:brain"),
    PS5MetricDescription(key="context_size", name="Context size", icon="mdi:text-box-outline", native_unit_of_measurement="tokens",
                         entity_category=EntityCategory.DIAGNOSTIC),
    PS5MetricDescription(key="gen_speed", name="Generation speed (last)", icon="mdi:speedometer", native_unit_of_measurement=TOKENS_PER_SECOND,
                         state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=1),
    PS5MetricDescription(key="prompt_speed", name="Prompt speed (last)", icon="mdi:speedometer-medium", native_unit_of_measurement=TOKENS_PER_SECOND,
                         state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=0),
    PS5MetricDescription(key="requests_active", name="Active requests", icon="mdi:transit-connection-horizontal",
                         state_class=SensorStateClass.MEASUREMENT),
    PS5MetricDescription(key="requests_queued", name="Queued requests", icon="mdi:timer-sand", state_class=SensorStateClass.MEASUREMENT),
    PS5MetricDescription(key="tokens_generated", name="Tokens generated", icon="mdi:counter", native_unit_of_measurement="tokens",
                         state_class=SensorStateClass.TOTAL_INCREASING),
    PS5MetricDescription(key="prompt_tokens", name="Prompt tokens processed", icon="mdi:counter", native_unit_of_measurement="tokens",
                         state_class=SensorStateClass.TOTAL_INCREASING),
    PS5MetricDescription(key="cache_hit_pct", name="Prompt cache hit rate", icon="mdi:cached", native_unit_of_measurement=PERCENTAGE,
                         state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=0),
    # --- console (ps5-exporter) ---
    PS5MetricDescription(key="cpu_temp", name="CPU temperature", device_class=SensorDeviceClass.TEMPERATURE,
                         native_unit_of_measurement=UnitOfTemperature.CELSIUS, state_class=SensorStateClass.MEASUREMENT),
    PS5MetricDescription(key="soc_temp_max", name="SoC temperature (hottest)", device_class=SensorDeviceClass.TEMPERATURE,
                         native_unit_of_measurement=UnitOfTemperature.CELSIUS, state_class=SensorStateClass.MEASUREMENT),
    PS5MetricDescription(key="fan_pct", name="Fan speed", icon="mdi:fan", native_unit_of_measurement=PERCENTAGE,
                         state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=0),
    PS5MetricDescription(key="app_memory", name="PS5LM memory", device_class=SensorDeviceClass.DATA_SIZE,
                         native_unit_of_measurement=UnitOfInformation.BYTES, suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
                         suggested_display_precision=1, state_class=SensorStateClass.MEASUREMENT),
    PS5MetricDescription(key="uptime_s", name="Console uptime", device_class=SensorDeviceClass.DURATION,
                         native_unit_of_measurement=UnitOfTime.SECONDS, suggested_unit_of_measurement=UnitOfTime.MINUTES,
                         suggested_display_precision=0, state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC),
    PS5MetricDescription(key="internal_free", name="Free space (internal)", device_class=SensorDeviceClass.DATA_SIZE,
                         native_unit_of_measurement=UnitOfInformation.BYTES, suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
                         suggested_display_precision=0, state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC),
    PS5MetricDescription(key="usb_free", name="Free space (USB)", device_class=SensorDeviceClass.DATA_SIZE,
                         native_unit_of_measurement=UnitOfInformation.BYTES, suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
                         suggested_display_precision=0, state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC),
)


async def async_setup_entry(hass: HomeAssistant, entry: PS5ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities([PS5StatusSensor(coordinator), *(PS5MetricSensor(coordinator, d) for d in METRICS)])


class PS5StatusSensor(PS5Entity, SensorEntity):
    """Rest mode, Waking the console, Signing in, Starting PS5LM, Loading the model, Model ready, Console on, PS5LM off..."""

    _attr_icon = "mdi:information-outline"

    def __init__(self, coordinator: PS5Coordinator) -> None:
        super().__init__(coordinator, "status", "Status")

    @property
    def native_value(self) -> str:
        return self.coordinator.status

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {"console": data.get("console"), "app_running": data.get("app_running"), "title_id": self.coordinator.client.title_id,
                "last_result": self.coordinator.last_result}


class PS5MetricSensor(PS5Entity, SensorEntity):
    """One value from the console exporter or the model server. Unavailable while its source does not answer
    (console asleep, PS5LM not running, exporter payload not loaded)."""

    entity_description: PS5MetricDescription

    def __init__(self, coordinator: PS5Coordinator, description: PS5MetricDescription) -> None:
        super().__init__(coordinator, description.key, description.name)
        self.entity_description = description

    @property
    def available(self) -> bool:
        return super().available and self.entity_description.key in ((self.coordinator.data or {}).get("metrics") or {})

    @property
    def native_value(self):
        return ((self.coordinator.data or {}).get("metrics") or {}).get(self.entity_description.key)
