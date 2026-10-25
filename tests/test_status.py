"""The Status sensor drops a failed start once the model turns ready after all."""

from __future__ import annotations

import time
from types import SimpleNamespace

from custom_components.ps5_launcher import PS5Coordinator


def status(failure_age: float | None, ready: bool) -> str:
    fake = SimpleNamespace(
        pending=None,
        failure=None if failure_age is None else ("http://ps5.home:8081/health did not answer 200 within 420 s", time.monotonic() - failure_age),
        data={"console": "ready", "app_running": True, "app_ready": ready},
        client=SimpleNamespace(ready_url="http://ps5.home:8081/health"),
    )
    return PS5Coordinator.status.fget(fake)


def test_failure_shown_while_the_model_is_not_ready():
    assert status(10, ready=False).startswith("Failed: ")


def test_failure_hidden_once_the_model_is_ready():
    assert status(10, ready=True) == "Model ready"


def test_old_failure_expires():
    assert status(10_000, ready=False) == "Loading the model"


# -- a sandbox left behind must not look like a running app -----------------------------------------------------------
class _Client:
    ready_url = "http://ps5.home:8081/health"

    def __init__(self, sandbox, answers, process):
        self._sandbox, self._answers, self._process = sandbox, answers, process
        self.process_reads = 0

    def app_running(self):
        return self._sandbox

    def is_ready(self):
        return self._answers

    def app_process_alive(self):
        self.process_reads += 1
        return self._process


async def _app_state(client):
    async def run(func):
        return func()

    fake = SimpleNamespace(hass=SimpleNamespace(async_add_executor_job=run), client=client)
    return await PS5Coordinator._app_state(fake)


async def test_leftover_sandbox_without_a_process_is_not_running():
    assert await _app_state(_Client(sandbox=True, answers=False, process=False)) == (False, None)


async def test_loading_model_with_a_live_process_is_running():
    assert await _app_state(_Client(sandbox=True, answers=False, process=True)) == (True, False)


async def test_unknown_process_state_keeps_the_sandbox_answer():
    assert await _app_state(_Client(sandbox=True, answers=False, process=None)) == (True, False)


async def test_answering_model_never_reads_the_exporter():
    client = _Client(sandbox=True, answers=True, process=False)
    assert await _app_state(client) == (True, True)
    assert client.process_reads == 0


async def test_no_sandbox_means_not_running():
    assert await _app_state(_Client(sandbox=False, answers=False, process=True)) == (False, None)
