"""Select entity for choosing which model to load on the PS5."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import _LOGGER, PS5ConfigEntry, PS5Coordinator
from .entity import PS5Entity
from .ps5client import ARGS_DIR, ARGS_NAME, PS5Error


async def async_setup_entry(hass: HomeAssistant, entry: PS5ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities([PS5ModelSelect(coordinator)])


class PS5ModelSelect(PS5Entity, SelectEntity):
    """Select entity for choosing the model to load."""

    _attr_icon = "mdi:brain"

    def __init__(self, coordinator: PS5Coordinator) -> None:
        super().__init__(coordinator, "model_select", "Model")
        self._model_list: list[str] = []
        self._loading = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._maybe_load_models()

    def _handle_coordinator_update(self) -> None:
        self._maybe_load_models()
        super()._handle_coordinator_update()

    def _maybe_load_models(self) -> None:
        """Re-read the model folder on every coordinator update while the console is on, so an upload shows up within
        one scan interval. The listing is one small request to websrv, run in the executor."""
        if self._loading or not self.coordinator.console_is_on():
            return
        self._loading = True
        self.hass.async_create_task(self._async_load_models())

    async def _async_load_models(self) -> None:
        try:
            models = await self.hass.async_add_executor_job(self.coordinator.client.list_models)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Could not list the models on the console")
            models = []
        finally:
            self._loading = False
        # [] means websrv did not answer (a real folder is never empty here): keep the list we have.
        if models and models != self._model_list:
            self._model_list = models
            self.async_write_ha_state()

    @property
    def options(self) -> list[str]:
        current = self.current_option
        if current and current not in self._model_list:  # a model that is loaded but no longer in the folder
            return sorted([current, *self._model_list], key=str.lower)
        return self._model_list

    @property
    def current_option(self) -> str | None:
        """The model the server reports as loaded."""
        return ((self.coordinator.data or {}).get("metrics") or {}).get("model")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes for the entity."""
        attrs = {"status": self.coordinator.status}
        if self._model_list:
            attrs["available_models"] = self._model_list
        return attrs

    async def async_select_option(self, option: str) -> None:
        """Switch PS5LM to `option` (a model name without .gguf).

        app-args.txt is the command line PS5LM reads when it starts, so the switch writes it and restarts the app:
        the model cannot be swapped in place, because unloading leaves most of its memory held.
        """
        if option not in self._model_list:
            raise HomeAssistantError(f"{option} is not one of the models on the console")
        if not self.coordinator.console_is_on():
            raise HomeAssistantError("The console is off or unreachable")

        client = self.coordinator.client
        file_name = f"{option}.gguf"

        async def switch() -> None:
            # Named here so the Model sensor and this dropdown show what is loading, not what was unloaded.
            self.coordinator.switching_to = option
            text = await self.hass.async_add_executor_job(client.build_app_args_for_model, file_name, None)
            if not text:
                raise HomeAssistantError(f"Could not build the arguments for {option}")
            # Written through the file manager, which answers whether or not PS5LM is running.
            try:
                await self.hass.async_add_executor_job(client.upload_file, ARGS_DIR, ARGS_NAME, text)
            except PS5Error as err:
                raise HomeAssistantError(f"Could not write {ARGS_NAME} on the console: {err}") from err

            self.coordinator._progress("stopping PS5LM")
            await self.hass.async_add_executor_job(client.stop_app)
            self.coordinator._progress(f"starting PS5LM with {option}")
            await self.hass.async_add_executor_job(client.start, None, client.ready_url, self.coordinator._progress)

            # PS5LM falls back to its own planner if it cannot read app-args.txt, so confirm what actually loaded.
            loaded = await self.hass.async_add_executor_job(client.loaded_model)
            if loaded and loaded != option:
                raise HomeAssistantError(f"PS5LM started with {loaded}, not {option}")

        try:
            await self.coordinator._operation("model switch", f"switching to {option}", switch)
        finally:
            self.coordinator.switching_to = None
