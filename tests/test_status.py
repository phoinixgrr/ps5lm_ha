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
