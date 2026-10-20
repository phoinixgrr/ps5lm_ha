"""Tests for the blocking PS5 client: credentials, Remote Play crypto, discovery, metrics, stop."""

from __future__ import annotations

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
