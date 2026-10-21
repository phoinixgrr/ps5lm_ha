"""Select entity for choosing which model to load on the PS5."""

from __future__ import annotations

import json
import logging
from typing import Any

import urllib.request
from urllib.parse import quote

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import _LOGGER, PS5ConfigEntry, PS5Coordinator
from .entity import PS5Entity


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
        """Read the model list once the console is on. The listing is a network call, so it runs in the executor."""
        if self._loading or self._model_list or not self.coordinator.console_is_on():
            return
        self._loading = True
        self.hass.async_create_task(self._async_load_models())

    async def _async_load_models(self) -> None:
        try:
            self._model_list = await self.hass.async_add_executor_job(self.coordinator.client.list_models)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Could not list the models on the console")
        finally:
            self._loading = False
        self.async_write_ha_state()

    @property
    def options(self) -> list[str]:
        current = self.current_option
        if current and current not in self._model_list:
            return [current, *self._model_list]
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
        """Switch to the selected model.
        
        Args:
            option: The model name to switch to (without .gguf extension)
        """
        if self.coordinator.op_lock.locked():
            _LOGGER.warning("Another PS5 operation is in progress, skipping model switch")
            return

        if not self._model_list or option not in self._model_list:
            _LOGGER.error("Invalid model option: %s", option)
            return

        if not self.coordinator.console_is_on():
            raise HomeAssistantError("Console is off or unreachable")

        if not self.coordinator.client.ready_url or not self.coordinator.client.is_ready():
            raise HomeAssistantError("PS5LM is not ready")

        model_path = f"/data/PS5LM/models/{option}.gguf"
        progress_callback = lambda msg: self.coordinator._progress(msg)

        async def switch_model() -> None:
            """Perform the model switch sequence."""
            # Step 1: Build app-args.txt for the new model
            current_app_args = self.coordinator.client.read_console_file("/data/PS5LM/app-args.txt")
            new_app_args = self.coordinator.client.build_app_args_for_model(model_path, current_app_args)

            if not new_app_args:
                raise HomeAssistantError("Failed to build app arguments for model")

            # Step 2: Write app-args.txt using the file manager (works while app is stopped)
            if not self._upload_file_via_wfm("/data/PS5LM/app-args.txt", new_app_args):
                raise HomeAssistantError("Failed to upload app-args.txt to console")

            progress_callback("Switching model")

            # Step 3: Stop the app
            progress_callback("Stopping PS5LM")
            try:
                await self.coordinator.async_app_off()
            except HomeAssistantError:
                # App might already be stopped
                pass

            # Step 4: Start the app again
            progress_callback(f"Starting PS5LM with {option}")
            try:
                await self.coordinator.async_app_on(ready_url=self.coordinator.client.ready_url)
            except HomeAssistantError as err:
                raise HomeAssistantError(f"Failed to start PS5LM: {err}")

        try:
            await self.coordinator._operation("switch_model", "Switching model", switch_model)
        except HomeAssistantError as err:
            _LOGGER.error("Model switch failed: %s", err)
            raise


    def _upload_file_via_wfm(self, path: str, content: str) -> bool:
        """Upload a file to the console using the file manager on port 8888.
        
        This uses the three-call sequence from the file manager:
        1. POST /api/upload/prepare - get task_id
        2. POST /api/upload-file - send file data
        3. POST /api/upload/finish?task_id=N - complete upload
        
        Args:
            path: Full path on console (e.g., /data/PS5LM/app-args.txt)
            content: File contents as string
            
        Returns:
            True if upload succeeded, False otherwise
        """
        try:
            # Extract host from path
            if path.startswith("/"):
                parts = path.split("/")
                if len(parts) >= 2:
                    host = parts[1]
                else:
                    host = "ps5.home"
            else:
                host = "ps5.home"
            
            api_base = f"http://{host}:8888/api"
            file_size = len(content.encode("utf-8"))
            
            # Step 1: Prepare upload
            prepare_data = {
                "path": path.rstrip("/"),
                "src": path.split("/")[-1],
                "total": file_size,
                "count": 1,
                "rels": path.split("/")[-1],
                "sizes": file_size,
                "overwrite": 1,
            }
            
            prepare_req = urllib.request.Request(
                f"{api_base}/upload/prepare",
                data=urllib.parse.urlencode(prepare_data).encode(),
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            
            with urllib.request.urlopen(prepare_req, timeout=10) as response:
                prepare_result = json.loads(response.read().decode())
                if not prepare_result.get("ok"):
                    _LOGGER.error("Upload prepare failed: %s", prepare_result.get("error"))
                    return False
                task_id = prepare_result["task_id"]
            
            # Step 2: Send file data
            upload_req = urllib.request.Request(
                f"{api_base}/upload-file",
                data=content.encode("utf-8"),
                method="POST",
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-WFM-Task-ID": str(task_id),
                    "X-WFM-Path": quote(path.rstrip("/")),
                    "X-WFM-Rel": path.split("/")[-1],
                    "X-WFM-Size": str(file_size),
                    "X-WFM-Overwrite": "1",
                },
            )
            
            with urllib.request.urlopen(upload_req, timeout=10) as response:
                if not response.read():
                    _LOGGER.error("Upload file failed")
                    return False
            
            # Step 3: Finish upload
            finish_req = urllib.request.Request(
                f"{api_base}/upload/finish?task_id={task_id}",
                method="POST",
            )
            
            with urllib.request.urlopen(finish_req, timeout=10) as response:
                if not response.read():
                    _LOGGER.error("Upload finish failed")
                    return False
            
            return True
            
        except Exception as err:
            _LOGGER.error("File upload failed: %s", err)
            return False
