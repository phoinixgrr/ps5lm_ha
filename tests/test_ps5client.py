"""Tests for the blocking PS5 client: credentials, Remote Play crypto, discovery, metrics, stop."""

from __future__ import annotations

import os
import socket
import threading

import pytest

from custom_components.ps5_launcher import ps5client
from custom_components.ps5_launcher.ps5client import PS5Error, _Crypt, parse_credentials

from .conftest import EXPORTER_METRICS, MORNING, PROPS, REGIST_KEY, llama_metrics


# -- credentials -------------------------------------------------------------------------------------------------------
def test_parse_credentials_ok():
    regist, morning = parse_credentials(f" {REGIST_KEY} ", MORNING.upper())
    assert regist == REGIST_KEY.encode()
    assert morning == bytes.fromhex(MORNING)


@pytest.mark.parametrize(
    ("regist", "morning"),
    [("", MORNING), ("zz", MORNING), ("1" * 17, MORNING), (REGIST_KEY, "00"), (REGIST_KEY, "g" * 32)],
)
def test_parse_credentials_rejects_bad_input(regist, morning):
    with pytest.raises(ValueError):
        parse_credentials(regist, morning)


# -- Remote Play crypto (reference values from the implementation verified against a real PS5) ---------------------
def test_crypt_key_derivation():
    crypt = _Crypt(bytes(range(16)), bytes(range(0x10, 0x20)))
    assert crypt.ambassador.hex() == "ac7d50f394ea6588f0488f76e4170c8a"
    assert crypt.bright.hex() == "c91f3930c120e8d02bd53a46799b210b"


@pytest.mark.parametrize(
    ("counter", "expected"),
    [(0, "fdd7b22b5e48a5adcad9"), (2, "951b7ebeb566e0e36716"), (4, "a1f4f6402b668d26f7be")],
)
def test_crypt_encrypt(counter, expected):
    crypt = _Crypt(bytes(range(16)), bytes(range(0x10, 0x20)))
    assert crypt.encrypt(counter, b"Win10.0.0\0").hex() == expected


# -- discovery ---------------------------------------------------------------------------------------------------------
@pytest.fixture
def fake_console(monkeypatch):
    """A UDP responder on localhost that answers discovery requests with a configurable status line."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    monkeypatch.setattr(ps5client, "DISCOVERY_PORT", sock.getsockname()[1])
    state = {"reply": b"HTTP/1.1 620 Server Standby\nhost-type:PS5\n", "received": []}

    def serve():
        while True:
            try:
                data, addr = sock.recvfrom(2048)
            except OSError:
                return
            state["received"].append(data)
            if state["reply"] is not None and data.startswith(b"SRCH"):
                sock.sendto(b"junk that is not a reply", addr)  # must be ignored
                sock.sendto(state["reply"], addr)

    threading.Thread(target=serve, daemon=True).start()
    yield state
    sock.close()


def test_discover_standby_and_ready(fake_console):
    assert ps5client.discover("127.0.0.1") == "standby"
    fake_console["reply"] = b"HTTP/1.1 200 Ok\nhost-type:PS5\n"
    assert ps5client.discover("127.0.0.1") == "ready"


def test_discover_no_answer(fake_console):
    fake_console["reply"] = None
    assert ps5client.discover("127.0.0.1", timeout=0.3) is None


def test_wake_packet_carries_credential(fake_console):
    ps5client.send_wake("127.0.0.1", REGIST_KEY.encode())
    deadline = threading.Event()
    deadline.wait(0.5)
    packet = next(p for p in fake_console["received"] if p.startswith(b"WAKEUP"))
    assert f"user-credential:{int(REGIST_KEY, 16)}".encode() in packet
    assert b"device-discovery-protocol-version:00030010" in packet


# -- metrics -----------------------------------------------------------------------------------------------------------
def _serve(client, monkeypatch, pages: dict[str, str | None]):
    monkeypatch.setattr(client, "_get_text", lambda url, timeout=3.0: next((v for k, v in pages.items() if url.endswith(k)), None))


def test_fetch_metrics_console_and_llm(client, monkeypatch):
    pages = {":9100/metrics": EXPORTER_METRICS, ":8081/metrics": llama_metrics(100, 2.0, 50, 1.0, cached=150), "/props": PROPS}
    _serve(client, monkeypatch, pages)
    m = client.fetch_metrics(console_ready=True, app_running=True)
    assert m["cpu_temp"] == 45 and m["soc_temp_max"] == 47
    assert m["fan_pct"] == 18.6
    assert m["app_memory"] == 9974677504
    assert m["internal_free"] == 208546955264 and m["usb_free"] == 199925694464
    assert m["model"] == "Qwen3.5-9B-Q6_K" and m["context_size"] == 65536
    assert m["requests_active"] == 1
    assert m["cache_hit_pct"] == 75.0
    assert m["gen_speed"] == 50.0  # first poll: average since PS5LM started


def test_speeds_come_from_counter_deltas_and_stick(client, monkeypatch):
    pages = {":9100/metrics": EXPORTER_METRICS, "/props": PROPS}
    _serve(client, monkeypatch, {**pages, ":8081/metrics": llama_metrics(100, 2.0, 50, 1.0)})
    client.fetch_metrics(True, True)
    _serve(client, monkeypatch, {**pages, ":8081/metrics": llama_metrics(400, 8.0, 450, 3.0)})
    m = client.fetch_metrics(True, True)
    assert m["gen_speed"] == 50.0 and m["prompt_speed"] == 200.0
    _serve(client, monkeypatch, {**pages, ":8081/metrics": llama_metrics(400, 8.0, 450, 3.0)})  # idle: no change
    m = client.fetch_metrics(True, True)
    assert m["gen_speed"] == 50.0 and m["prompt_speed"] == 200.0


def test_metrics_respect_switches_and_state(client, monkeypatch):
    _serve(client, monkeypatch, {":9100/metrics": EXPORTER_METRICS, ":8081/metrics": llama_metrics(1, 1, 1, 1), "/props": PROPS})
    assert client.fetch_metrics(console_ready=False, app_running=False) == {}
    m = client.fetch_metrics(console_ready=True, app_running=False)
    assert "cpu_temp" in m and "app_memory" not in m and "gen_speed" not in m
    client.console_metrics = False
    assert "cpu_temp" not in client.fetch_metrics(True, True)
    client.llm_metrics = False
    assert client.fetch_metrics(True, True) == {}


# -- stopping the app --------------------------------------------------------------------------------------------------
def test_stop_app_uses_empty_title(client, monkeypatch):
    calls, running = [], iter([True, False])
    monkeypatch.setattr(client, "websrv_up", lambda: True)
    monkeypatch.setattr(client, "app_running", lambda title_id=None: next(running, False))
    monkeypatch.setattr(client, "_http", lambda url, timeout=4.0: calls.append(url) or 503)
    client.stop_app()
    assert calls == ["http://192.0.2.10:8080/launch?titleId="]



# -- a sandbox left behind is not a running app ------------------------------------------------------------------------
ALIVE = EXPORTER_METRICS + 'ps5_process_instances{name="eboot.bin"} 1\nps5_process_instances{name="SceShellUI"} 1\n'
GONE = EXPORTER_METRICS + 'ps5_process_instances{name="SceShellUI"} 1\n'


def test_process_alive_from_exporter(client, monkeypatch):
    _serve(client, monkeypatch, {":9100/metrics": ALIVE})
    assert client.app_process_alive() is True
    _serve(client, monkeypatch, {":9100/metrics": GONE})
    assert client.app_process_alive() is False


def test_process_unknown_falls_back_to_the_sandbox(client, monkeypatch):
    monkeypatch.setattr(client, "app_running", lambda title_id=None: True)
    _serve(client, monkeypatch, {":9100/metrics": EXPORTER_METRICS})  # no process metrics
    assert client.app_process_alive() is None and client.app_started()
    _serve(client, monkeypatch, {})  # exporter down
    assert client.app_process_alive() is None and client.app_started()
    client.console_metrics = False
    assert client.app_process_alive() is None


def test_start_relaunches_over_a_stale_sandbox(client, monkeypatch):
    """Sandbox there, process gone: start() must launch and wait for the process, not report it running at once."""
    launches, pages = [], {":9100/metrics": GONE}
    monkeypatch.setattr(client, "_get_text", lambda url, timeout=3.0: next((v for k, v in pages.items() if url.endswith(k)), None))
    monkeypatch.setattr(client, "app_running", lambda title_id=None: True)
    monkeypatch.setattr(client, "state", lambda: "ready")
    monkeypatch.setattr(client, "websrv_up", lambda: True)
    monkeypatch.setattr(ps5client, "remote_play_login", lambda *a: "HTTP/1.1 200 OK")

    def launch(title_id=None):
        launches.append(title_id)
        pages[":9100/metrics"] = ALIVE

    monkeypatch.setattr(client, "launch_once", launch)
    steps = []
    client.start(ready_url="", progress=steps.append)
    assert launches and "app already running" not in steps and "app launched" in steps


def test_stop_waits_for_the_process_not_the_sandbox(client, monkeypatch):
    pages = {":9100/metrics": ALIVE}
    monkeypatch.setattr(client, "_get_text", lambda url, timeout=3.0: next((v for k, v in pages.items() if url.endswith(k)), None))
    monkeypatch.setattr(client, "app_running", lambda title_id=None: True)  # the sandbox never goes away
    monkeypatch.setattr(client, "websrv_up", lambda: True)
    monkeypatch.setattr(client, "_http", lambda url, timeout=4.0: pages.update({":9100/metrics": GONE}) or 503)
    client.stop_app()  # returns once eboot.bin is gone

def test_stop_app_needs_websrv(client, monkeypatch):
    monkeypatch.setattr(client, "websrv_up", lambda: False)
    with pytest.raises(PS5Error):
        client.stop_app()


def test_llama_base_from_health_url(client):
    assert client.llama_base == "http://192.0.2.10:8081"
    client.ready_url = ""
    assert client.llama_base is None


# -- Remote Play refusals ----------------------------------------------------------------------------------------------
def test_session_refused_reasons():
    assert ps5client.SessionRefused(0x80108B10).in_use
    assert "in use" in str(ps5client.SessionRefused(0x80108B10))
    assert not ps5client.SessionRefused(0x80108B09).in_use
    assert "0x80108bff" in str(ps5client.SessionRefused(0x80108BFF))


class _FakeSocket:
    def __init__(self):
        self.sent = b""

    def sendall(self, data):
        self.sent += data

    def settimeout(self, _):
        pass

    def recv(self, _):
        return b""

    def close(self):
        pass


def test_goto_bed_waits_while_remote_play_is_in_use(monkeypatch):
    answers = iter([ps5client.SessionRefused(0x80108B10), ps5client.SessionRefused(0x80108B10), bytes(16)])

    def session(host, key):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    sock = _FakeSocket()
    monkeypatch.setattr(ps5client, "_session_request", session)
    monkeypatch.setattr(ps5client, "_control_connect", lambda *a: (sock, "HTTP/1.1 200 OK"))
    monkeypatch.setattr(ps5client.time, "sleep", lambda s: None)
    assert ps5client.goto_bed("192.0.2.10", REGIST_KEY.encode(), bytes(16), wait=0) == "HTTP/1.1 200 OK"
    assert sock.sent == bytes([0, 0, 0, 0, 0, 0x50, 0, 0])  # empty control message of type 0x50


def test_goto_bed_gives_up_on_other_refusals(monkeypatch):
    def session(host, key):
        raise ps5client.SessionRefused(0x80108B09)

    monkeypatch.setattr(ps5client, "_session_request", session)
    assert "registration rejected" in ps5client.goto_bed("192.0.2.10", REGIST_KEY.encode(), bytes(16))


# -- reading the console exporter stalls the model, so it is read rarely ------------------------------------------------
def test_console_is_read_on_the_first_poll():
    assert ps5client.should_scrape_console(1000.0, None, 120, busy=False)
    assert ps5client.should_scrape_console(1000.0, None, 120, busy=True)


def test_console_is_not_read_before_the_interval():
    assert not ps5client.should_scrape_console(1100.0, 1000.0, 120, busy=False)
    assert ps5client.should_scrape_console(1120.0, 1000.0, 120, busy=False)


def test_console_is_not_read_while_the_model_is_busy():
    assert not ps5client.should_scrape_console(1200.0, 1000.0, 120, busy=True)
    assert ps5client.should_scrape_console(1600.0, 1000.0, 120, busy=True)  # 5 intervals old: read anyway


def test_fetch_metrics_can_skip_the_console(client, monkeypatch):
    asked = []
    pages = {":9100/metrics": EXPORTER_METRICS, ":8081/metrics": llama_metrics(1, 1, 1, 1), "/props": PROPS}
    monkeypatch.setattr(client, "_get_text", lambda url, timeout=3.0: asked.append(url) or next((v for k, v in pages.items() if url.endswith(k)), None))
    m = client.fetch_metrics(console_ready=True, app_running=True, scrape_console=False)
    assert not any(":9100" in u for u in asked)
    assert "cpu_temp" not in m and m["model"] == "Qwen3.5-9B-Q6_K"
    m = client.fetch_metrics(console_ready=True, app_running=True, scrape_console=True)
    assert m["cpu_temp"] == 45


def test_standby_explains_a_remote_play_in_use_refusal(client, monkeypatch):
    monkeypatch.setattr(client, "state", lambda: "ready")
    monkeypatch.setattr(ps5client, "goto_bed", lambda *a, **k: str(ps5client.SessionRefused(0x80108B10)))
    with pytest.raises(PS5Error) as err:
        client.standby()
    assert "in use" in str(err.value) and "chiaki-ng" in str(err.value)


def test_standby_does_nothing_when_the_console_is_not_awake(client, monkeypatch):
    monkeypatch.setattr(client, "state", lambda: "standby")
    monkeypatch.setattr(ps5client, "goto_bed", lambda *a, **k: pytest.fail("must not be called"))
    client.standby()


# -- model selection ---------------------------------------------------------------------------------------------------
def test_list_models_returns_model_names(client, monkeypatch):
    """Test that list_models returns model names without mmproj files."""
    models_json = '''[
        {"name": "/data/PS5LM/models/Qwen3.5-9B-Q6_K.gguf"},
        {"name": "/data/PS5LM/models/Qwen3-14B-Q4_K_M.gguf"},
        {"name": "/data/PS5LM/models/gemma-4-12b-it-Q5_K_M.gguf"},
        {"name": "/data/PS5LM/models/vision-mmproj-f16.gguf"}
    ]'''
    monkeypatch.setattr(client, "_get_text", lambda url, timeout=3.0: models_json if "/models/" in url else None)
    models = client.list_models()
    assert models == ["gemma-4-12b-it-Q5_K_M", "Qwen3-14B-Q4_K_M", "Qwen3.5-9B-Q6_K"]  # A to Z, ignoring case
    assert "vision-mmproj-f16" not in models


def test_list_models_skips_dot_entries_and_other_files(client, monkeypatch):
    """websrv lists '.' and '..' and any other file in the folder; only .gguf files are models."""
    models_json = '[{"name": "."}, {"name": ".."}, {"name": "notes.txt"}, {"name": "Qwen3.5-9B-Q6_K.gguf"}, {"name": "x-mmproj.gguf"}]'
    monkeypatch.setattr(client, "_get_text", lambda url, timeout=3.0: models_json if "/models/" in url else None)
    assert client.list_models() == ["Qwen3.5-9B-Q6_K"]


def test_list_models_empty_or_invalid(client, monkeypatch):
    """Test that list_models handles empty or invalid responses."""
    monkeypatch.setattr(client, "_get_text", lambda url, timeout=3.0: "invalid json" if "/models/" in url else None)
    models = client.list_models()
    assert models == []

    monkeypatch.setattr(client, "_get_text", lambda url, timeout=3.0: None)
    models = client.list_models()
    assert models == []


def test_read_console_file(client, monkeypatch):
    """Test reading a file from the console."""
    content = "test content\nline 2"
    monkeypatch.setattr(client, "_get_text", lambda url, timeout=3.0: content if "/app-args.txt" in url else None)
    result = client.read_console_file("/data/PS5LM/app-args.txt")
    assert result == content


def test_read_console_file_not_found(client, monkeypatch):
    """Test reading a file that doesn't exist."""
    monkeypatch.setattr(client, "_get_text", lambda url, timeout=3.0: None)
    result = client.read_console_file("/data/PS5LM/nonexistent.txt")
    assert result is None


def test_loaded_model_reads_props(client, monkeypatch):
    monkeypatch.setattr(client, "_get_text", lambda url, timeout=3.0: PROPS if url.endswith("/props") else None)
    assert client.loaded_model() == "Qwen3.5-9B-Q6_K"


def test_loaded_model_when_the_server_is_silent(client, monkeypatch):
    monkeypatch.setattr(client, "_get_text", lambda url, timeout=3.0: None)
    assert client.loaded_model() is None


# -- writing a file through the file manager ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> bytes:
        return self._text.encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _record_uploads(monkeypatch, answers):
    """Capture the requests upload_file sends, answering each with the matching entry of `answers`."""
    sent = []

    def fake_urlopen(request, timeout=None):
        sent.append(request)
        return _FakeResponse(answers[len(sent) - 1])

    monkeypatch.setattr(ps5client.urllib.request, "urlopen", fake_urlopen)
    return sent


def test_upload_file_sends_prepare_bytes_and_finish(client, monkeypatch):
    sent = _record_uploads(monkeypatch, ['{"ok": true, "task_id": 7}', '{"ok": true}', '{"ok": true}'])
    client.upload_file("/data/PS5LM", "app-args.txt", "-m\nmodel.gguf\n")

    assert len(sent) == 3
    prepare, upload, finish = sent
    assert prepare.full_url == "http://192.0.2.10:8888/api/upload/prepare"
    assert b"path=%2Fdata%2FPS5LM" in prepare.data
    assert b"overwrite=1" in prepare.data

    assert upload.full_url == "http://192.0.2.10:8888/api/upload-file"
    assert upload.data == b"-m\nmodel.gguf\n"
    assert upload.get_header("X-wfm-task-id") == "7"
    assert upload.get_header("X-wfm-path") == "%2Fdata%2FPS5LM"
    assert upload.get_header("X-wfm-rel") == "app-args.txt"
    assert upload.get_header("X-wfm-size") == str(len(b"-m\nmodel.gguf\n"))

    assert finish.full_url == "http://192.0.2.10:8888/api/upload/finish?task_id=7"


def test_upload_file_counts_bytes_not_characters(client, monkeypatch):
    """A Greek comment in the file would make a character count disagree with the size the file manager is told."""
    sent = _record_uploads(monkeypatch, ['{"ok": true, "task_id": 1}', "", ""])
    client.upload_file("/data/PS5LM", "notes.txt", "καλημέρα\n")
    assert sent[1].get_header("X-wfm-size") == str(len("καλημέρα\n".encode()))


def test_upload_file_raises_when_the_file_manager_refuses(client, monkeypatch):
    _record_uploads(monkeypatch, ['{"ok": false, "error": "no space left"}'])
    with pytest.raises(PS5Error, match="no space left"):
        client.upload_file("/data/PS5LM", "app-args.txt", "x")


def test_upload_file_raises_when_there_is_no_task_id(client, monkeypatch):
    _record_uploads(monkeypatch, ['{"ok": true}'])
    with pytest.raises(PS5Error, match="task id"):
        client.upload_file("/data/PS5LM", "app-args.txt", "x")


def test_upload_file_raises_when_the_console_does_not_answer(client, monkeypatch):
    def refuse(request, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(ps5client.urllib.request, "urlopen", refuse)
    with pytest.raises(PS5Error, match="did not answer"):
        client.upload_file("/data/PS5LM", "app-args.txt", "x")


# -- the arguments for a model -----------------------------------------------------------------------------------------
# app-args.txt holds one argument per line, so a flag's value is the line after it.
CURRENT_ARGS = "\n".join([
    "--log-file", "/data/PS5LM/llama.log",
    "-m", "/data/PS5LM/models/Qwen3.5-9B-Q6_K.gguf",
    "-c", "65536",
    "-ctk", "q8_0",
    "-ctv", "q8_0",
    "-ctxcp", "8",
    "-cram", "0",
    "--tools", "read_file,write_file",
    "-rea", "off",
]) + "\n"


def _pairs(text: str) -> dict[str, str]:
    """The flags of an app-args.txt and the value on the line after each."""
    lines = text.splitlines()
    return {line: lines[i + 1] for i, line in enumerate(lines[:-1]) if line.startswith("-")}


def test_build_app_args_from_profile(client, monkeypatch):
    """A tuned profile for the model is used exactly as it is, whatever it says."""
    profile = "-m\n/data/PS5LM/models/Qwen3-14B-Q4_K_M.gguf\n-c\n20480\n-ctk\nq4_0\n"
    monkeypatch.setattr(client, "read_console_file", lambda path: profile if "/profiles/" in path else CURRENT_ARGS)
    assert client.build_app_args_for_model("Qwen3-14B-Q4_K_M.gguf") == profile


def test_build_app_args_from_profile_without_a_trailing_newline(client, monkeypatch):
    monkeypatch.setattr(client, "read_console_file", lambda path: "-m\nx.gguf" if "/profiles/" in path else None)
    assert client.build_app_args_for_model("x.gguf").endswith("\n")


def test_build_app_args_falls_back_to_the_current_file(client, monkeypatch):
    """With no profile: the same flags, the new model, and a context and cache small enough to start."""
    monkeypatch.setattr(client, "read_console_file", lambda path: None if "/profiles/" in path else CURRENT_ARGS)
    result = client.build_app_args_for_model("gemma-4-12b-it-Q5_K_M.gguf")
    pairs = _pairs(result)

    assert pairs["-m"] == "/data/PS5LM/models/gemma-4-12b-it-Q5_K_M.gguf"
    assert pairs["-c"] == "8192"
    assert pairs["-ctk"] == "q4_0"
    assert pairs["-ctv"] == "q4_0"
    # Every other flag is left as it was, and nothing of the old model survives.
    assert pairs["--log-file"] == "/data/PS5LM/llama.log"
    assert pairs["--tools"] == "read_file,write_file"
    assert pairs["-rea"] == "off"
    assert "Qwen3.5-9B" not in result
    assert result.endswith("\n")


def test_build_app_args_does_not_mistake_other_c_flags_for_the_context(client, monkeypatch):
    """-ctk, -ctv, -ctxcp and -cram all start with "-c": only the line that IS "-c" is the context size."""
    monkeypatch.setattr(client, "read_console_file", lambda path: None if "/profiles/" in path else CURRENT_ARGS)
    pairs = _pairs(client.build_app_args_for_model("x.gguf"))
    assert pairs["-c"] == "8192"
    assert pairs["-ctxcp"] == "8"
    assert pairs["-cram"] == "0"


def test_build_app_args_takes_the_current_text_when_given(client, monkeypatch):
    """The caller can pass the current file instead of having it read again."""
    monkeypatch.setattr(client, "read_console_file", lambda path: None)
    pairs = _pairs(client.build_app_args_for_model("x.gguf", CURRENT_ARGS))
    assert pairs["-m"] == "/data/PS5LM/models/x.gguf"
    assert pairs["-rea"] == "off"


def test_build_app_args_appends_a_flag_the_current_file_does_not_have(client, monkeypatch):
    monkeypatch.setattr(client, "read_console_file", lambda path: None)
    pairs = _pairs(client.build_app_args_for_model("x.gguf", "-m\nold.gguf\n--metrics\n"))
    assert pairs["-m"] == "/data/PS5LM/models/x.gguf"
    assert pairs["-c"] == "8192"
    assert pairs["-ctk"] == "q4_0"


def test_build_app_args_with_nothing_to_start_from(client, monkeypatch):
    """No profile and no current file: a minimal command line that still serves the API the integration reads."""
    monkeypatch.setattr(client, "read_console_file", lambda path: None)
    pairs = _pairs(client.build_app_args_for_model("test_model.gguf", None))
    assert pairs["-m"] == "/data/PS5LM/models/test_model.gguf"
    assert pairs["-c"] == "8192"
    assert pairs["--port"] == "8081"
    assert "--metrics" in client.build_app_args_for_model("test_model.gguf", None).splitlines()


def test_list_models_is_sorted_ignoring_case(client, monkeypatch):
    """A dropdown is easier to read A to Z, and case must not push every lowercase name to the end."""
    listing = '[{"name": "zephyr.gguf"}, {"name": "Qwen3-14B.gguf"}, {"name": "gemma-4.gguf"}, {"name": "Alpha.gguf"}]'
    monkeypatch.setattr(client, "_get_text", lambda url, timeout=3.0: listing if "/models/" in url else None)
    assert client.list_models() == ["Alpha", "gemma-4", "Qwen3-14B", "zephyr"]


# -- another app started from the controller: measured on the console -------------------------------------------------
# Both snapshots are real exporter output, trimmed to the lines the check reads. In both, eboot.bin runs and PS5LM's
# sandbox folder exists; only PS5LM's download0 mount tells them apart.
_FIX = os.path.join(os.path.dirname(__file__), "fixtures")
GAME_RUNNING = open(os.path.join(_FIX, "exporter_game_running.prom")).read()   # Silent Hill 2 open, PS5LM closed
PS5LM_RUNNING = open(os.path.join(_FIX, "exporter_ps5lm_running.prom")).read()  # PS5LM open, model ready


def _console(client, monkeypatch, pages):
    monkeypatch.setattr(client, "_get_text", lambda url, timeout=3.0: next((v for k, v in pages.items() if url.endswith(k)), None))
    monkeypatch.setattr(client, "app_running", lambda title_id=None: True)  # PS5LM's sandbox folder never goes away


def test_another_app_is_not_ps5lm_although_eboot_runs(client, monkeypatch):
    _console(client, monkeypatch, {":9100/metrics": GAME_RUNNING})
    assert 'name="eboot.bin"} 1' in GAME_RUNNING
    assert client.app_process_alive() is False and client.app_started() is False


def test_ps5lm_running_is_seen_by_its_mount(client, monkeypatch):
    _console(client, monkeypatch, {":9100/metrics": PS5LM_RUNNING})
    assert client.app_process_alive() is True and client.app_started() is True


def test_no_filesystem_data_falls_back_to_eboot(client, monkeypatch):
    failed = GAME_RUNNING.replace('collector="filesystems"} 1', 'collector="filesystems"} 0')
    _console(client, monkeypatch, {":9100/metrics": failed})
    assert client.app_process_alive() is True  # cannot tell the apps apart: behave as 0.3.4 did


def test_stop_leaves_another_app_running(client, monkeypatch):
    calls = []
    _console(client, monkeypatch, {":9100/metrics": GAME_RUNNING})
    monkeypatch.setattr(client, "websrv_up", lambda: True)
    monkeypatch.setattr(client, "_http", lambda url, timeout=4.0: calls.append(url) or 503)
    client.stop_app()
    assert calls == []  # the game is not closed


def test_start_over_another_app_launches_once(client, monkeypatch):
    pages, launches = {":9100/metrics": GAME_RUNNING}, []
    _console(client, monkeypatch, pages)
    monkeypatch.setattr(client, "state", lambda: "ready")
    monkeypatch.setattr(client, "websrv_up", lambda: True)
    monkeypatch.setattr(ps5client, "remote_play_login", lambda *a: "HTTP/1.1 200 OK")

    def launch(title_id=None):
        launches.append(title_id)
        pages[":9100/metrics"] = PS5LM_RUNNING  # websrv closes the game and PS5LM mounts its download0

    monkeypatch.setattr(client, "launch_once", launch)
    steps = []
    client.start(ready_url="", progress=steps.append)
    assert len(launches) == 1 and "app already running" not in steps and "app launched" in steps


def test_start_waits_before_launching_again(client, monkeypatch):
    """A launch closes whatever runs, so a launch that has not shown yet must not be repeated quickly."""
    pages, launches, waits = {":9100/metrics": GAME_RUNNING}, [], []
    _console(client, monkeypatch, pages)
    monkeypatch.setattr(client, "state", lambda: "ready")
    monkeypatch.setattr(client, "websrv_up", lambda: True)
    monkeypatch.setattr(ps5client, "remote_play_login", lambda *a: "HTTP/1.1 200 OK")
    monkeypatch.setattr(client, "launch_once", lambda title_id=None: launches.append(1))
    real_wait = client._wait

    def wait(test, limit, step=3.0, stop=None):
        waits.append(limit)
        if len(launches) == 2:
            pages[":9100/metrics"] = PS5LM_RUNNING
        return test()

    monkeypatch.setattr(client, "_wait", wait)
    client.start(ready_url="", progress=lambda m: None)
    assert len(launches) == 2 and ps5client.LAUNCH_SETTLE in waits and ps5client.LAUNCH_SETTLE >= 20


# -- while the model loads, follow the console --------------------------------------------------------------------------
class _Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _loading(client, monkeypatch, states, alive):
    """A start that waits for the model: `states` and `alive` are what discovery and the mount say at each check."""
    clock = _Clock()
    monkeypatch.setattr(ps5client.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(ps5client.time, "sleep", clock.sleep)
    monkeypatch.setattr(client, "_http", lambda url, timeout=4.0: 503)
    it_s, it_a = iter(states), iter(alive)
    monkeypatch.setattr(client, "state", lambda: next(it_s, states[-1]))
    monkeypatch.setattr(client, "app_process_alive", lambda title_id=None: next(it_a, alive[-1]))
    return clock


def test_rest_mode_while_loading_stops_the_wait(client, monkeypatch):
    clock = _loading(client, monkeypatch, ["ready", "standby", "standby"], [True])
    with pytest.raises(ps5client.PS5Interrupted, match="rest mode"):
        client._wait_until_ready("http://ps5/health", "PPSA99575")
    assert clock.t - 1000 < 60  # seconds, not the 7-minute timeout


def test_another_app_while_loading_stops_the_wait(client, monkeypatch):
    clock = _loading(client, monkeypatch, ["ready"], [True, False, False])
    with pytest.raises(ps5client.PS5Interrupted, match="another app"):
        client._wait_until_ready("http://ps5/health", "PPSA99575")
    assert clock.t - 1000 < 60


def test_one_lost_packet_or_slow_mount_does_not_end_a_good_start(client, monkeypatch):
    _loading(client, monkeypatch, ["ready", None, "ready", "ready"], [False, True, True])
    answers = iter([503] * 20 + [200])
    monkeypatch.setattr(client, "_http", lambda url, timeout=4.0: next(answers))
    client._wait_until_ready("http://ps5/health", "PPSA99575")  # returns, no exception


def test_model_that_never_answers_still_times_out(client, monkeypatch):
    _loading(client, monkeypatch, ["ready"], [True])
    with pytest.raises(ps5client.PS5Error, match="did not answer"):
        client._wait_until_ready("http://ps5/health", "PPSA99575")
