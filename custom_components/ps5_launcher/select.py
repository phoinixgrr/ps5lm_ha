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
    _attr_options = []

    def __init__(self, coordinator: PS5Coordinator) -> None:
        super().__init__(coordinator, "model_select", "Model")
        self._current_model: str | None = None
        self._model_list: list[str] = []
        self._update_options()

    def _update_options(self) -> None:
        """Refresh the model options from the console."""
        if not self.coordinator.console_is_on():
            self._model_list = []
            self._current_model = None
            return

        if not self.coordinator.client.ready_url or not self.coordinator.client.is_ready():
            self._model_list = []
            self._current_model = None
            return

        try:
            self._model_list = self.coordinator.client.list_models()
            # Get the current model from props if available
            if self.coordinator.data and "metrics" in self.coordinator.data:
                metrics = self.coordinator.data["metrics"]
                if "model" in metrics:
                    self._current_model = metrics["model"]
        except Exception as err:
            _LOGGER.error("Failed to update model options: %s", err)
            self._model_list = []
            self._current_model = None

        self.async_write_ha_state()

    @property
    def current_option(self) -> str | None:
        """Return the currently selected model."""
        return self._current_model

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes for the entity."""
        attrs = {"status": self.coordinator.status}
        if self._model_list:
            attrs["available_models"] = self._model_list
        if self.coordinator.data and "metrics" in self.coordinator.data:
            attrs["current_model"] = self.coordinator.data["metrics"].get("model")
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

        # Update the current model after successful switch
        self._current_model = option
        self.async_write_ha_state()

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
