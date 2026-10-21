"""PS5LM: run PS5LM (llama.cpp) on a jailbroken PS5 on demand from Home Assistant: wake, start, stop, rest mode."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import intent
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
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
    DEFAULT_API_PORT,
    DEFAULT_CONSOLE_INTERVAL,
    DEFAULT_CONSOLE_METRICS,
    DEFAULT_EXPORTER_PORT,
    DEFAULT_LLM_METRICS,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TITLE_ID,
    DEFAULT_WEB_PORT,
    DOMAIN,
    MIN_SCAN_INTERVAL,
    SERVICE_STANDBY,
    SERVICE_START_APP,
    SERVICE_STOP_APP,
    SERVICE_WAKE,
)
from .ps5client import PS5Client, PS5Error, should_scrape_console

CONSOLE_KEYS = ("cpu_temp", "soc_temp_max", "fan_pct", "uptime_s", "app_memory", "internal_free", "usb_free")

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SWITCH, Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SELECT]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional("config_entry_id"): cv.string,
        vol.Optional("title_id"): cv.string,
        vol.Optional("ready_url"): cv.string,
        vol.Optional("wait", default=False): cv.boolean,
    }
)

# plain-language labels for what the start sequence reports
STEP_LABELS = {
    "waking the console": "Waking the console",
    "console awake": "Console awake",
    "waiting for websrv": "Waiting for the console to finish booting",
    "launching the app": "Starting PS5LM",
    "app launched": "PS5LM started",
    "waiting for the ready URL": "Loading the model",
    "app already running": "PS5LM already running",
}
FAILURE_SHOWN_FOR = 300  # seconds a failed start or stop stays in the Status sensor
REFRESH_ON = {"console awake", "app launched", "signed in via Remote Play", "done"}


class PS5Coordinator(DataUpdateCoordinator[dict]):
    """Polls the console, and runs one operation at a time (wake, rest mode, start app, stop app)."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: PS5Client, scan_interval: int = DEFAULT_SCAN_INTERVAL,
                 console_interval: int = DEFAULT_CONSOLE_INTERVAL) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=f"PS5 {entry.data[CONF_HOST]}",
                         update_interval=timedelta(seconds=max(MIN_SCAN_INTERVAL, scan_interval)))
        self.client = client
        self.console_interval = console_interval
        self._console_at: float | None = None
        self._console_last: dict[str, float] = {}
        self.op_lock = asyncio.Lock()
        self.pending: str | None = None  # console_on, console_off, app_on, app_off while an operation runs
        self.step: str = ""
        self.last_result: str | None = None
        self.failure: tuple[str, float] | None = None  # (reason, when) of the last failed operation, shown in Status for a while

    async def _async_update_data(self) -> dict:
        state = await self.hass.async_add_executor_job(self.client.state)
        running = ready = None
        if state == "ready":
            running = await self.hass.async_add_executor_job(self.client.app_running)
            if running and self.client.ready_url:
                ready = await self.hass.async_add_executor_job(self.client.is_ready)
        # The console exporter is read rarely and not while the model is answering: each read stalls the model for seconds.
        busy = bool(((self.data or {}).get("metrics") or {}).get("requests_active"))
        scrape = state == "ready" and should_scrape_console(time.monotonic(), self._console_at, self.console_interval, busy)
        metrics = await self.hass.async_add_executor_job(self.client.fetch_metrics, state == "ready", bool(running), scrape)
        if state != "ready":
            self._console_last, self._console_at = {}, None
        else:
            if scrape:
                self._console_at = time.monotonic()
                self._console_last = {k: metrics[k] for k in CONSOLE_KEYS if k in metrics}
            metrics = {**self._console_last, **metrics}
        return {"console": state or "off", "app_running": running, "app_ready": ready, "metrics": metrics}

    # -- what the entities show -------------------------------------------------------------------------------------
    @property
    def status(self) -> str:
        if self.pending:
            return self.step or "Working"
        if self.failure and time.monotonic() - self.failure[1] < FAILURE_SHOWN_FOR:
            return f"Failed: {self.failure[0]}"[:255]
        data = self.data or {}
        console = data.get("console")
        if console == "standby":
            return "Rest mode"
        if console != "ready":
            return "Off or unreachable"
        if data.get("app_running"):
            if self.client.ready_url:
                return "Model ready" if data.get("app_ready") else "Loading the model"
            return "PS5LM running"
        return "Console on, PS5LM off"

    # The switches show only the real state; the Status sensor shows what is in progress.
    def console_is_on(self) -> bool:
        return bool(self.data and self.data["console"] == "ready")

    def app_is_on(self) -> bool:
        return bool(self.data and self.data["console"] == "ready" and self.data["app_running"])

    def _set_step(self, message: str) -> None:
        # the sign-in runs next to the launch; once the app is up, its late messages would step the status backwards
        if self.step in ("PS5LM started", "Loading the model") and (
            message.startswith(("signed in", "signing in", "Remote Play sign-in", "sign-in"))
        ):
            return
        self.step = STEP_LABELS.get(message) or (
            "Signing in" if message.startswith("signing in via") else
            "Signing in (retrying)" if message.startswith("Remote Play sign-in failed") else
            "Signed in" if message.startswith("signed in") else
            message[:1].upper() + message[1:]
        )
        self.async_update_listeners()
        if message in REFRESH_ON:
            self.hass.async_create_task(self.async_request_refresh())

    def _progress(self, message: str) -> None:
        _LOGGER.info("%s: %s", self.client.host, message)
        self.hass.loop.call_soon_threadsafe(self._set_step, message)

    # -- operations -------------------------------------------------------------------------------------------------
    async def _operation(self, name: str, first_step: str, work: Callable[[], Awaitable[Any]]) -> Any:
        if self.op_lock.locked():
            raise HomeAssistantError("another PS5 operation is still running")
        async with self.op_lock:
            started = time.monotonic()
            self.failure = None
            self.pending, self.step = name, first_step
            self.async_update_listeners()
            try:
                result = await work()
            except PS5Error as err:
                self.last_result = f"failed: {err}"
                self.failure = (str(err), time.monotonic())
                raise HomeAssistantError(str(err)) from err
            else:
                self.last_result = f"ok in {time.monotonic() - started:.0f} s"
                return result
            finally:
                self.pending, self.step = None, ""
                await self.async_request_refresh()

    async def async_console_on(self) -> None:
        """Wake the console from rest mode and wait until it is awake. Does not start any app."""
        await self._operation("console_on", "Waking the console", lambda: self.hass.async_add_executor_job(self.client.wake_and_wait))

    async def async_console_off(self) -> None:
        """Put the console in rest mode (this also ends the running app)."""
        await self._operation("console_off", "Sending the console to rest mode", lambda: self.hass.async_add_executor_job(self.client.standby))

    async def async_app_on(self, title_id: str | None = None, ready_url: str | None = None) -> None:
        """Wake the console if needed, sign in, start PS5LM and wait for the model if a health URL is set."""
        await self._operation(
            "app_on", "Starting",
            lambda: self.hass.async_add_executor_job(self.client.start, title_id, ready_url, self._progress),
        )

    async def async_app_off(self) -> None:
        """Close the running app. The console stays on."""
        await self._operation("app_off", "Stopping PS5LM", lambda: self.hass.async_add_executor_job(self.client.stop_app))


@dataclass
class PS5Data:
    coordinator: PS5Coordinator


type PS5ConfigEntry = ConfigEntry[PS5Data]


SPEECH = {
    "el": {
        "none": "Δεν έχει ρυθμιστεί PS5.",
        "already": "Το τοπικό μοντέλο τρέχει ήδη.",
        "starting": "Εντάξει, ξεκινάω το τοπικό μοντέλο. Θα είναι έτοιμο σε περίπου δύο λεπτά.",
        "stopping": "Εντάξει, το PS5 πάει για ύπνο.",
        "busy": "Το PS5 είναι απασχολημένο, δοκίμασε ξανά σε λίγο.",
        "asleep": "Το PS5 κοιμάται ήδη.",
    },
    "en": {
        "none": "No PS5 is configured.",
        "already": "The local model is already running.",
        "starting": "OK, starting the local model. It will be ready in about two minutes.",
        "stopping": "OK, sending the PS5 to sleep.",
        "busy": "The PS5 is busy, try again in a moment.",
        "asleep": "The PS5 is already asleep.",
    },
}


class PS5Intent(intent.IntentHandler):
    """Voice commands for Assist: start the local model (PS5Start) or send the PS5 to sleep (PS5Stop)."""

    def __init__(self, intent_type: str) -> None:
        self.intent_type = intent_type

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        speech = SPEECH["el" if (intent_obj.language or "").startswith("el") else "en"]
        response = intent_obj.create_response()
        entries = [e for e in hass.config_entries.async_entries(DOMAIN) if getattr(e, "runtime_data", None)]
        if not entries:
            response.async_set_speech(speech["none"])
            return response
        coordinator: PS5Coordinator = entries[0].runtime_data.coordinator
        if coordinator.op_lock.locked():
            response.async_set_speech(speech["busy"])
        elif self.intent_type == "PS5Start":
            if coordinator.app_is_on():
                response.async_set_speech(speech["already"])
            else:
                hass.async_create_background_task(_log_failure(coordinator.async_app_on()), f"{DOMAIN}_voice_start")
                response.async_set_speech(speech["starting"])
        else:
            if not coordinator.console_is_on():
                response.async_set_speech(speech["asleep"])
            else:
                hass.async_create_background_task(_log_failure(coordinator.async_console_off()), f"{DOMAIN}_voice_stop")
                response.async_set_speech(speech["stopping"])
        return response


async def _log_failure(coro: Awaitable[None]) -> None:
    try:
        await coro
    except HomeAssistantError as err:
        _LOGGER.error("PS5LM operation failed: %s", err)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the services and the voice intents once for the domain."""
    intent.async_register(hass, PS5Intent("PS5Start"))
    intent.async_register(hass, PS5Intent("PS5Stop"))

    def coordinator_for(call: ServiceCall) -> PS5Coordinator:
        entries = [e for e in hass.config_entries.async_entries(DOMAIN) if getattr(e, "runtime_data", None)]
        wanted = call.data.get("config_entry_id")
        if wanted:
            entries = [e for e in entries if e.entry_id == wanted]
        if len(entries) != 1:
            raise ServiceValidationError("pass config_entry_id: there is not exactly one PS5 configured")
        return entries[0].runtime_data.coordinator

    async def in_background(coro: Awaitable[None]) -> None:
        try:
            await coro
        except HomeAssistantError as err:
            _LOGGER.error("PS5LM operation failed: %s", err)

    async def handle_wake(call: ServiceCall) -> None:
        coro = coordinator_for(call).async_console_on()
        if call.data["wait"]:
            await coro
        else:
            hass.async_create_background_task(in_background(coro), f"{DOMAIN}_wake")

    async def handle_standby(call: ServiceCall) -> None:
        await coordinator_for(call).async_console_off()

    async def handle_start_app(call: ServiceCall) -> None:
        coro = coordinator_for(call).async_app_on(call.data.get("title_id"), call.data.get("ready_url"))
        if call.data["wait"]:
            await coro
        else:
            hass.async_create_background_task(in_background(coro), f"{DOMAIN}_start_app")

    async def handle_stop_app(call: ServiceCall) -> None:
        await coordinator_for(call).async_app_off()

    hass.services.async_register(DOMAIN, SERVICE_WAKE, handle_wake, schema=SERVICE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_STANDBY, handle_standby, schema=SERVICE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_START_APP, handle_start_app, schema=SERVICE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_STOP_APP, handle_stop_app, schema=SERVICE_SCHEMA)
    return True


def api_url_for(options: dict) -> str:
    """PS5LM's base URL: the configured one, else derived from an older health URL, else http://<host>:8081."""
    if url := (options.get(CONF_API_URL) or "").strip().rstrip("/"):
        return url
    if old := (options.get(CONF_READY_URL) or "").strip():
        return old.rsplit("/health", 1)[0].rstrip("/")
    return f"http://{options[CONF_HOST]}:{DEFAULT_API_PORT}"


async def async_setup_entry(hass: HomeAssistant, entry: PS5ConfigEntry) -> bool:
    # options win over the values from the first setup; the keys may live in either
    options = {**entry.data, **entry.options}
    client = PS5Client(
        host=entry.data[CONF_HOST],
        regist_key=options[CONF_REGIST_KEY],
        morning=options[CONF_MORNING],
        title_id=options.get(CONF_TITLE_ID, DEFAULT_TITLE_ID),
        web_port=int(options.get(CONF_WEB_PORT, DEFAULT_WEB_PORT)),
        ready_url=f"{api_url_for(options)}/health",
        exporter_port=int(options.get(CONF_EXPORTER_PORT, DEFAULT_EXPORTER_PORT)),
        llm_metrics=bool(options.get(CONF_LLM_METRICS, DEFAULT_LLM_METRICS)),
        console_metrics=bool(options.get(CONF_CONSOLE_METRICS, DEFAULT_CONSOLE_METRICS)),
    )
    coordinator = PS5Coordinator(hass, entry, client, int(options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
                                 int(options.get(CONF_CONSOLE_INTERVAL, DEFAULT_CONSOLE_INTERVAL)))
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = PS5Data(coordinator)
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _options_updated(hass: HomeAssistant, entry: PS5ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: PS5ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
