"""PS5LM as a fleet worker serves no model API: readiness is its RPC endpoint, and Stop must not wait for a start."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.ps5_launcher import PS5Coordinator
from custom_components.ps5_launcher import ps5client
from custom_components.ps5_launcher.binary_sensor import PS5AppReady
from custom_components.ps5_launcher.ps5client import PS5Error, PS5Interrupted, parse_fleet_endpoint

WORKER_FILE = "192.168.101.180:30052\nI_UNDERSTAND_RPC_IS_UNAUTHENTICATED\n"


# -- the file ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text, expected", [
    (WORKER_FILE, ("192.168.101.180", 30052)),
    ("10.0.0.5:1\n", ("10.0.0.5", 1)),
    ("  10.0.0.5:65535  \r\nack", ("10.0.0.5", 65535)),
])
def test_endpoint_is_read_from_the_first_line(text, expected):
    assert parse_fleet_endpoint(text) == expected


@pytest.mark.parametrize("text", ["", "\n", "ps5.home:30052", "192.168.1.5", "192.168.1.5:0", "192.168.1.5:70000",
                                  "192.168.1.5:abc", "300.1.1.1:5000", ":5000", "[::1]:5000"])
def test_endpoint_rejects_what_the_app_would_not_accept(text):
    assert parse_fleet_endpoint(text) is None


def test_a_missing_file_is_a_normal_server(client, monkeypatch):
    monkeypatch.setattr(client, "read_console_file", lambda path: None)
    assert client.fleet_worker() is None


def test_the_file_is_read_through_the_console_path(client, monkeypatch):
    seen = []
    monkeypatch.setattr(client, "read_console_file", lambda path: seen.append(path) or WORKER_FILE)
    assert client.fleet_worker() == WORKER_FILE
    assert seen == ["/data/PS5LM/fleet-worker.txt"]


def test_polling_reuses_the_read_but_a_start_reads_again(client, monkeypatch):
    reads = []
    monkeypatch.setattr(client, "read_console_file", lambda path: reads.append(path) or WORKER_FILE)
    client.fleet_worker()
    client.fleet_worker()
    assert len(reads) == 1
    client.fleet_worker(fresh=True)
    assert len(reads) == 2


def test_the_read_expires(client, monkeypatch):
    reads = []
    monkeypatch.setattr(client, "read_console_file", lambda path: reads.append(path) or None)
    client.fleet_worker()
    client._fleet_at -= ps5client.FLEET_CHECK_EVERY + 1  # the file was deleted and the app restarted meanwhile
    client.fleet_worker()
    assert len(reads) == 2


# -- readiness --------------------------------------------------------------------------------------------------------
def test_a_worker_is_ready_when_its_endpoint_accepts_a_connection(client, monkeypatch):
    opened = []
    monkeypatch.setattr(client, "read_console_file", lambda path: WORKER_FILE)
    monkeypatch.setattr(client, "_http", lambda *a, **k: pytest.fail("a worker has no /health"))
    monkeypatch.setattr(client, "_tcp_open", lambda host, port, timeout=3.0: opened.append((host, port)) or True)
    assert client.is_ready() is True
    assert opened == [("192.168.101.180", 30052)]


def test_a_worker_is_not_ready_while_the_endpoint_is_closed(client, monkeypatch):
    monkeypatch.setattr(client, "read_console_file", lambda path: WORKER_FILE)
    monkeypatch.setattr(client, "_tcp_open", lambda *a, **k: False)
    assert client.is_ready() is False


def test_a_worker_file_without_an_endpoint_is_never_ready(client, monkeypatch):
    monkeypatch.setattr(client, "read_console_file", lambda path: "garbage\n")
    monkeypatch.setattr(client, "_tcp_open", lambda *a, **k: pytest.fail("no endpoint to open"))
    assert client.is_ready() is False


def test_a_normal_server_is_still_ready_on_its_health_url(client, monkeypatch):
    monkeypatch.setattr(client, "read_console_file", lambda path: None)
    monkeypatch.setattr(client, "_http", lambda url, timeout=4.0: 200 if url.endswith("/health") else None)
    assert client.is_ready() is True


def test_the_wait_ends_when_the_worker_endpoint_opens_though_health_never_answers(client, monkeypatch):
    monkeypatch.setattr(client, "read_console_file", lambda path: WORKER_FILE)
    monkeypatch.setattr(client, "_http", lambda *a, **k: None)  # /health: nothing there
    answers = iter([False, False, True])
    monkeypatch.setattr(client, "_tcp_open", lambda *a, **k: next(answers))
    monkeypatch.setattr(ps5client.time, "sleep", lambda s: None)
    client._wait_until_ready(client.ready_url, "PPSA99575")  # returns instead of timing out after 420 s


def test_the_wait_fails_at_once_on_a_worker_file_that_cannot_work(client, monkeypatch):
    monkeypatch.setattr(client, "read_console_file", lambda path: "not an endpoint")
    with pytest.raises(PS5Error, match="fleet-worker.txt"):
        client._wait_until_ready(client.ready_url, "PPSA99575")


def test_the_timeout_names_the_worker_endpoint(client, monkeypatch):
    monkeypatch.setattr(client, "read_console_file", lambda path: WORKER_FILE)
    monkeypatch.setattr(client, "_tcp_open", lambda *a, **k: False)
    monkeypatch.setattr(ps5client, "READY_TIMEOUT", 0)
    with pytest.raises(PS5Error, match=r"fleet worker at 192\.168\.101\.180:30052"):
        client._wait_until_ready(client.ready_url, "PPSA99575")


# -- cancelling a start -----------------------------------------------------------------------------------------------
def test_a_cancelled_start_stops_waiting_for_the_model(client, monkeypatch):
    monkeypatch.setattr(client, "read_console_file", lambda path: None)
    monkeypatch.setattr(client, "_http", lambda *a, **k: None)  # the model never answers
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    with pytest.raises(PS5Interrupted, match="cancelled"):
        client._wait_until_ready(client.ready_url, "PPSA99575", cancel)  # would otherwise wait 420 s


def test_a_cancel_set_before_the_wait_ends_it_without_a_request(client, monkeypatch):
    monkeypatch.setattr(client, "read_console_file", lambda path: None)
    monkeypatch.setattr(client, "_http", lambda *a, **k: pytest.fail("cancelled before the first check"))
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(PS5Interrupted):
        client._wait_until_ready(client.ready_url, "PPSA99575", cancel)


def test_start_says_which_thing_it_waits_for(client, monkeypatch):
    monkeypatch.setattr(client, "app_started", lambda title=None: True)
    monkeypatch.setattr(client, "_wait_until_ready", lambda ready, title, cancel=None: None)
    for text, expected in ((WORKER_FILE, "waiting for the fleet worker"), (None, "waiting for the ready URL")):
        monkeypatch.setattr(client, "read_console_file", lambda path, text=text: text)
        steps = []
        client.start(progress=steps.append)
        assert expected in steps


# -- the Status sensor ------------------------------------------------------------------------------------------------
def _status(worker: bool, ready: bool, running: bool = True) -> str:
    fake = SimpleNamespace(
        pending=None, failure=None,
        data={"console": "ready", "app_running": running, "app_ready": ready, "worker": worker},
        client=SimpleNamespace(ready_url="http://ps5.home:8081/health"),
    )
    return PS5Coordinator.status.fget(fake)


def test_status_of_a_worker():
    assert _status(worker=True, ready=False) == "Starting the fleet worker"
    assert _status(worker=True, ready=True) == "Fleet worker ready"


def test_status_of_a_normal_server_is_unchanged():
    assert _status(worker=False, ready=False) == "Loading the model"
    assert _status(worker=False, ready=True) == "Model ready"


# -- Stop and Rest mode while a start is waiting ----------------------------------------------------------------------
def _coordinator(pending: str | None):
    return SimpleNamespace(pending=pending, cancel_start=threading.Event(), op_lock=asyncio.Lock())


async def test_stop_without_a_pending_start_does_nothing_special():
    fake = _coordinator(None)
    await PS5Coordinator._cancel_pending_start(fake)
    assert not fake.cancel_start.is_set()


async def test_stop_cancels_a_start_and_waits_for_it_to_wind_down():
    fake = _coordinator("app_on")
    await fake.op_lock.acquire()  # the start holds the lock while it waits

    async def start_winds_down():
        while not fake.cancel_start.is_set():
            await asyncio.sleep(0.01)
        fake.op_lock.release()

    task = asyncio.create_task(start_winds_down())
    await PS5Coordinator._cancel_pending_start(fake)
    await asyncio.wait_for(task, 2)  # fails, instead of hanging, if Stop never cancelled the start
    assert fake.cancel_start.is_set()
    assert not fake.op_lock.locked()  # the stop can take it now


async def test_stop_reports_a_start_that_will_not_stop(monkeypatch):
    from custom_components.ps5_launcher import CANCEL_WAIT  # noqa: F401  (the constant exists)
    monkeypatch.setattr("custom_components.ps5_launcher.CANCEL_WAIT", 0.05)
    fake = _coordinator("app_on")
    await fake.op_lock.acquire()  # never released
    with pytest.raises(HomeAssistantError, match="did not stop"):
        await PS5Coordinator._cancel_pending_start(fake)


async def test_other_operations_are_left_alone():
    for name in ("console_on", "console_off", "app_off"):
        fake = _coordinator(name)
        await PS5Coordinator._cancel_pending_start(fake)
        assert not fake.cancel_start.is_set()


# -- Model ready ------------------------------------------------------------------------------------------------------
def _model_ready(data) -> bool:
    return PS5AppReady.is_on.fget(SimpleNamespace(coordinator=SimpleNamespace(data=data)))


def test_model_ready_follows_the_health_url_of_a_normal_server():
    assert _model_ready({"app_ready": True, "worker": False}) is True
    assert _model_ready({"app_ready": False, "worker": False}) is False


def test_a_ready_fleet_worker_is_not_a_ready_model():
    """The job-stop automation closes PS5LM while this is on, which would end a worker another machine is using."""
    assert _model_ready({"app_ready": True, "worker": True}) is False


def test_model_ready_without_data_is_off():
    assert _model_ready(None) is False
    assert _model_ready({}) is False


# -- the model dropdown -----------------------------------------------------------------------------------------------
async def test_choosing_a_model_on_a_fleet_worker_is_refused_before_anything_is_written():
    from .test_select import make_select

    entity = make_select([["A", "B"]])
    entity._model_list = ["A", "B"]
    entity.coordinator.client.fleet_worker.return_value = WORKER_FILE
    with pytest.raises(HomeAssistantError, match="fleet worker"):
        await entity.async_select_option("A")
    entity.coordinator.client.upload_file.assert_not_called()
    entity.coordinator.client.stop_app.assert_not_called()
    entity.coordinator.client.start.assert_not_called()


async def test_choosing_a_model_on_a_normal_server_still_switches():
    from .test_select import make_select

    entity = make_select([["A", "B"]])
    entity._model_list = ["A", "B"]
    client = entity.coordinator.client
    client.fleet_worker.return_value = None
    client.build_app_args_for_model.return_value = "-m\nA.gguf\n"
    client.loaded_model.return_value = "A"

    async def run_operation(name, first, work):
        await work()

    entity.coordinator._operation = run_operation
    await entity.async_select_option("A")
    client.upload_file.assert_called_once()
    client.stop_app.assert_called_once()
    client.start.assert_called_once()
