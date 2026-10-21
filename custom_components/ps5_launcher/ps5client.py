"""Blocking client for a jailbroken PS5: state, wake from rest mode, Remote Play login, app launch.

Waking a PS5 from rest mode over the network leaves it at the "press the PS button" screen, where
ps5-payload-websrv's /launch fails because no user is signed in. A Remote Play control connection signs
the registered user in, so start() holds one open while it asks websrv to launch the title.

The Remote Play handshake, constants and key tables follow chiaki-ng (lib/src/session.c, ctrl.c,
rpcrypt.c, discovery.c; AGPL-3.0). Only the PS5 / RP 1.0 path is implemented. Every method blocks:
call them through hass.async_add_executor_job.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

try:  # newer cryptography moved the legacy CFB mode here
    from cryptography.hazmat.decrepit.ciphers.modes import CFB
except ImportError:  # pragma: no cover
    from cryptography.hazmat.primitives.ciphers.modes import CFB

from .metrics import first, parse_prometheus
from .ps5_keys import KEYS_A, KEYS_B

_LOGGER = logging.getLogger(__name__)

DISCOVERY_PORT = 9302
RP_PORT = 9295
PROTO = "00030010"
HMAC_KEY_PS5 = bytes([0x46, 0x46, 0x87, 0xB3, 0x49, 0xCA, 0x8C, 0xE8, 0x59, 0xC5, 0x27, 0x0F, 0x5D, 0x7A, 0x69, 0xD6])
OSTYPE = b"Win10.0.0\0"
DID_PREFIX = bytes([0x00, 0x18, 0x00, 0x00, 0x00, 0x07, 0x00, 0x40, 0x00, 0x80])

_discovery_lock = threading.Lock()


def should_scrape_console(now: float, last_at: float | None, interval: float, busy: bool) -> bool:
    """Whether to read the console exporter now. Reading it makes the console stall the model for several seconds, so it
    is read rarely, and not while a request is running (unless the values are more than 5 intervals old)."""
    if last_at is None:
        return True
    age = now - last_at
    return age >= 5 * interval or (age >= interval and not busy)


class PS5Error(Exception):
    """Raised when a step of the start sequence fails."""


def parse_credentials(regist_key: str, morning: str) -> tuple[bytes, bytes]:
    """Validate the Remote Play credentials. Returns (regist key as ASCII bytes, 16 byte morning key)."""
    regist_key = regist_key.strip()
    morning = morning.strip()
    if not 1 <= len(regist_key) <= 16 or any(c not in "0123456789abcdefABCDEF" for c in regist_key):
        raise ValueError("regist_key must be 1 to 16 hex characters")
    if len(morning) != 32 or any(c not in "0123456789abcdefABCDEF" for c in morning):
        raise ValueError("morning must be 32 hex characters")
    return regist_key.encode(), bytes.fromhex(morning)


def discover(host: str, timeout: float = 2.0) -> str | None:
    """Ask the console for its state on the Remote Play discovery port: 'ready', 'standby' or None (no answer)."""
    with _discovery_lock:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            try:
                sock.bind(("", 9303))
            except OSError:
                sock.bind(("", 0))
            sock.sendto(f"SRCH * HTTP/1.1\ndevice-discovery-protocol-version:{PROTO}\n".encode() + b"\0", (host, DISCOVERY_PORT))
            end = time.monotonic() + timeout
            while (remaining := end - time.monotonic()) > 0:
                sock.settimeout(remaining)
                try:
                    data, _ = sock.recvfrom(2048)
                except TimeoutError:
                    return None
                first = data.decode(errors="replace").splitlines()[0] if data else ""
                if first.startswith("HTTP/1.1"):
                    if " 200" in first:
                        return "ready"
                    if " 620" in first:
                        return "standby"
                    return first
        finally:
            sock.close()
    return None


def send_wake(host: str, regist_key: bytes) -> None:
    """Send the Remote Play wake-up packet (same as chiaki-ng) a few times."""
    credential = int(regist_key.decode(), 16)
    packet = (
        "WAKEUP * HTTP/1.1\nclient-type:vr\nauth-type:R\nmodel:w\napp-type:r\n"
        f"user-credential:{credential}\ndevice-discovery-protocol-version:{PROTO}\n"
    ).encode() + b"\0"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _ in range(3):
            sock.sendto(packet, (host, DISCOVERY_PORT))
            time.sleep(0.3)
    finally:
        sock.close()


def _recv_headers(sock: socket.socket, timeout: float = 8.0) -> tuple[str, dict[str, str], bytes]:
    sock.settimeout(timeout)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    lines = head.decode(errors="replace").split("\r\n")
    headers = {line.split(":", 1)[0].strip().lower(): line.split(":", 1)[1].strip() for line in lines[1:] if ":" in line}
    return lines[0], headers, rest


REFUSAL_REASONS = {
    0x80108B10: "Remote Play is in use (another session is open or still closing)",
    0x80108B09: "registration rejected (register the console again)",
    0x80108B02: "invalid PSN ID",
    0x80108B11: "Remote Play version mismatch",
    0x80108B15: "Remote Play crashed on the console",
}


class SessionRefused(Exception):
    """The console refused the Remote Play session request."""

    def __init__(self, reason: int | None) -> None:
        self.reason = reason
        super().__init__(REFUSAL_REASONS.get(reason, f"session request refused (reason {reason:#x})" if reason else "session request refused"))

    @property
    def in_use(self) -> bool:
        return self.reason in (0x80108B10, None)


def _session_request(host: str, regist_key: bytes) -> bytes:
    """HTTP session request on port 9295. Returns the 16 byte nonce or raises SessionRefused.

    The console handles one session at a time: while one is open, or for a while after a request that was not followed
    by a control connection, it answers 403 with reason 0x80108b10 (in use)."""
    request = (
        f"GET /sie/ps5/rp/sess/init HTTP/1.1\r\nHost: {host}:{RP_PORT}\r\nUser-Agent: remoteplay Windows\r\n"
        f"Connection: close\r\nContent-Length: 0\r\nRP-Registkey: {regist_key.hex()}\r\nRp-Version: 1.0\r\n\r\n"
    ).encode()
    sock = socket.create_connection((host, RP_PORT), timeout=5)
    try:
        sock.sendall(request)
        status, headers, _ = _recv_headers(sock)
    finally:
        sock.close()
    if " 200" not in status or "rp-nonce" not in headers:
        try:
            reason = int(headers.get("rp-application-reason", ""), 16)
        except ValueError:
            reason = None
        raise SessionRefused(reason)
    return base64.b64decode(headers["rp-nonce"])


class _Crypt:
    """The Remote Play auth cipher (AES-128-CFB, IV from an HMAC over the counter)."""

    def __init__(self, nonce: bytes, morning: bytes) -> None:
        key_a = KEYS_A[(nonce[0] >> 3) * 0x70 :][:0x70]
        key_b = KEYS_B[(nonce[7] >> 3) * 0x70 :][:0x70]
        self.ambassador = bytes(((nonce[i] - 0x2D - i) & 0xFF) ^ key_a[i] for i in range(16))
        self.bright = bytes((((morning[i] + 0x18 + i) & 0xFF) ^ nonce[i] ^ key_b[i]) for i in range(16))

    def encrypt(self, counter: int, data: bytes) -> bytes:
        iv = hmac.new(HMAC_KEY_PS5, self.ambassador + counter.to_bytes(8, "big"), hashlib.sha256).digest()[:16]
        encryptor = Cipher(algorithms.AES(self.bright), CFB(iv)).encryptor()
        return encryptor.update(data) + encryptor.finalize()


def _control_connect(host: str, nonce: bytes, regist_key: bytes, morning: bytes) -> tuple[socket.socket, str]:
    """Open the control connection. The console needs a moment after the session request before it listens for it
    (connecting at once gets 'connection refused'), so wait a little and retry the TCP connect a few times."""
    crypt = _Crypt(nonce, morning)
    b64 = lambda data: base64.b64encode(data).decode()  # noqa: E731
    did = DID_PREFIX + os.urandom(16) + bytes(6)
    auth, did_e, os_e, bitrate, stream_type = (
        b64(crypt.encrypt(0, regist_key.ljust(16, b"\0"))),
        b64(crypt.encrypt(1, did)),
        b64(crypt.encrypt(2, OSTYPE)),
        b64(crypt.encrypt(3, bytes(4))),
        b64(crypt.encrypt(4, (1).to_bytes(4, "little"))),
    )
    request = (
        f"GET /sie/ps5/rp/sess/ctrl HTTP/1.1\r\nHost: {host}:{RP_PORT}\r\nUser-Agent: remoteplay Windows\r\n"
        f"Connection: keep-alive\r\nContent-Length: 0\r\nRP-Auth: {auth}\r\nRP-Version: 1.0\r\nRP-Did: {did_e}\r\n"
        f"RP-ControllerType: 3\r\nRP-ClientType: 11\r\nRP-OSType: {os_e}\r\nRP-ConPath: 1\r\n"
        f"RP-StartBitrate: {bitrate}\r\nRP-StreamingType: {stream_type}\r\n\r\n"
    ).encode()
    time.sleep(0.5)
    last_error: OSError | None = None
    for _ in range(12):
        try:
            sock = socket.create_connection((host, RP_PORT), timeout=5)
        except ConnectionRefusedError as err:
            last_error = err
            time.sleep(0.4)
            continue
        sock.sendall(request)
        status, _, _ = _recv_headers(sock)
        return sock, status
    raise last_error or OSError("control connection refused")


def remote_play_login(host: str, regist_key: bytes, morning: bytes, hold: float, stop: threading.Event) -> str:
    """Open a Remote Play control connection (this signs the registered user in) and hold it until `stop` or `hold` s.

    Returns the control response status line, or a short error text."""
    try:
        nonce = _session_request(host, regist_key)
    except SessionRefused as err:
        return f"session request refused: {err}"
    except OSError as err:
        return f"session request failed: {err}"
    try:
        sock, status = _control_connect(host, nonce, regist_key, morning)
    except OSError as err:
        return f"control connection failed: {err}"
    end = time.monotonic() + hold
    sock.settimeout(1.0)
    try:
        while time.monotonic() < end and not stop.is_set():
            try:
                if not sock.recv(4096):  # the console closed the connection
                    break
            except TimeoutError:
                pass
            except OSError:
                break
    finally:
        sock.close()
    return status


def goto_bed(host: str, regist_key: bytes, morning: bytes, wait: float = 6.0, busy_limit: float = 90.0) -> str:
    """Put the console in rest mode: the Remote Play control message chiaki-ng's 'Sleep' sends (type 0x50, no payload).

    While Remote Play is in use the session request is retried every 5 s for up to `busy_limit` seconds.
    Returns the control response status line, or a short error text."""
    end = time.monotonic() + busy_limit
    while True:
        try:
            nonce = _session_request(host, regist_key)
            break
        except SessionRefused as err:
            if not err.in_use or time.monotonic() > end:
                return str(err)
            time.sleep(5)
        except OSError as err:
            return f"session request failed: {err}"
    try:
        sock, status = _control_connect(host, nonce, regist_key, morning)
    except OSError as err:
        return f"control connection failed: {err}"
    try:
        sock.sendall(struct.pack(">IHH", 0, 0x50, 0))
        sock.settimeout(1.0)
        end = time.monotonic() + wait
        while time.monotonic() < end:
            try:
                if not sock.recv(4096):
                    break
            except TimeoutError:
                pass
            except OSError:
                break
    finally:
        sock.close()
    return status


class PS5Client:
    """One PS5 plus the websrv payload that launches apps on it."""

    def __init__(self, host: str, regist_key: str, morning: str, title_id: str, web_port: int = 8080, ready_url: str = "", exporter_port: int = 9100,
                 llm_metrics: bool = True, console_metrics: bool = True) -> None:
        self.host = host
        self.regist_key, self.morning = parse_credentials(regist_key, morning)
        self.title_id = title_id
        self.web_port = web_port
        self.ready_url = ready_url
        self.exporter_port = exporter_port
        self.llm_metrics = llm_metrics
        self.console_metrics = console_metrics
        self._counters: dict[str, tuple[float, float]] = {}
        self._last_speed: dict[str, float] = {}

    # -- reads -------------------------------------------------------------------------------------------------
    def state(self) -> str | None:
        return discover(self.host)

    def _http(self, url: str, timeout: float = 4.0) -> int | None:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 (local network URL)
                return response.status
        except urllib.error.HTTPError as err:
            return err.code
        except (OSError, ValueError):
            return None

    def is_ready(self) -> bool:
        """True when the optional ready URL (for example the model server's /health) answers 200."""
        return bool(self.ready_url) and self._http(self.ready_url, timeout=3) == 200

    @property
    def llama_base(self) -> str | None:
        """Base URL of the PS5LM server, taken from the health URL (for example http://192.168.1.50:8081)."""
        match = re.match(r"^(https?://[^/]+)", self.ready_url or "")
        return match.group(1) if match else None

    def _get_text(self, url: str, timeout: float = 3.0) -> str | None:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
                return response.read().decode(errors="replace")
        except (OSError, ValueError):
            return None

    def fetch_metrics(self, console_ready: bool, app_running: bool, scrape_console: bool = True) -> dict[str, float | str]:
        """Flat dict of numbers for the sensors: console (ps5-exporter) and model server (llama.cpp /metrics, /props).

        `scrape_console=False` skips the exporter (see should_scrape_console); the caller keeps the last console values."""
        out: dict[str, float | str] = {}
        if console_ready and self.console_metrics and scrape_console:
            text = self._get_text(f"http://{self.host}:{self.exporter_port}/metrics")
            if text:
                m = parse_prometheus(text)
                temps = [v for labels, v in m.get("ps5_temperature_celsius", []) if labels.get("sensor", "").startswith("soc")]
                if (v := first(m, "ps5_temperature_celsius", sensor="cpu")) is not None:
                    out["cpu_temp"] = v
                if temps:
                    out["soc_temp_max"] = max(temps)
                if (v := first(m, "ps5_fan_duty_ratio")) is not None:
                    out["fan_pct"] = round(v * 100, 1)
                if (v := first(m, "ps5_uptime_seconds")) is not None:
                    out["uptime_s"] = v
                if app_running and (v := first(m, "ps5_process_resident_bytes", name="eboot.bin")) is not None:
                    out["app_memory"] = v
                if (v := first(m, "ps5_filesystem_avail_bytes", mountpoint="/user")) is not None:
                    out["internal_free"] = v
                if (v := first(m, "ps5_filesystem_avail_bytes", mountpoint="/mnt/usb0")) is not None:
                    out["usb_free"] = v
        base = self.llama_base
        if app_running and base and self.llm_metrics:
            text = self._get_text(f"{base}/metrics")
            if text:
                m = parse_prometheus(text)
                # llama.cpp's own tok/s gauges reset on every scrape, so derive the speeds from the running counters
                # and keep the last value seen (the speed of the most recent work, not 0 while idle).
                for key, tokens_name, seconds_name in (
                    ("gen_speed", "llamacpp:tokens_predicted_total", "llamacpp:tokens_predicted_seconds_total"),
                    ("prompt_speed", "llamacpp:prompt_tokens_total", "llamacpp:prompt_seconds_total"),
                ):
                    tokens, seconds = first(m, tokens_name), first(m, seconds_name)
                    if tokens is None or seconds is None:
                        continue
                    prev = self._counters.get(key)
                    self._counters[key] = (tokens, seconds)
                    if prev and tokens > prev[0] and seconds > prev[1]:
                        self._last_speed[key] = (tokens - prev[0]) / (seconds - prev[1])
                    elif prev is None and tokens > 0 and seconds > 0 and key not in self._last_speed:
                        self._last_speed[key] = tokens / seconds  # first poll: average since PS5LM started
                    if key in self._last_speed:
                        out[key] = round(self._last_speed[key], 1)
                for key, name in (
                    ("requests_active", "llamacpp:requests_processing"),
                    ("requests_queued", "llamacpp:requests_deferred"),
                    ("tokens_generated", "llamacpp:tokens_predicted_total"),
                    ("prompt_tokens", "llamacpp:prompt_tokens_total"),
                    ("prompt_cached", "llamacpp:prompt_tokens_cached_total"),
                ):
                    if (v := first(m, name)) is not None:
                        out[key] = v
                if "prompt_tokens" in out and "prompt_cached" in out:
                    total = out["prompt_tokens"] + out["prompt_cached"]
                    if total:
                        out["cache_hit_pct"] = round(100 * out["prompt_cached"] / total, 1)
            props = self._get_text(f"{base}/props")
            if props:
                try:
                    data = json.loads(props)
                    out["model"] = os.path.basename(data["model_path"]).removesuffix(".gguf")
                    out["context_size"] = data["default_generation_settings"]["n_ctx"]
                except (ValueError, KeyError):
                    pass
        return out

    def websrv_up(self) -> bool:
        return self._http(f"http://{self.host}:{self.web_port}/version") == 200

    def app_running(self, title_id: str | None = None) -> bool | None:
        """True when the title's sandbox exists (a successful launch creates it); None when websrv does not answer."""
        title = title_id or self.title_id
        try:
            with urllib.request.urlopen(f"http://{self.host}:{self.web_port}/fs/mnt/sandbox/?fmt=json", timeout=4) as response:  # noqa: S310
                return any(entry.get("name", "").startswith(title) for entry in json.load(response))
        except (OSError, ValueError):
            return None

    # -- actions -----------------------------------------------------------------------------------------------
    def wake(self) -> None:
        send_wake(self.host, self.regist_key)

    def standby(self) -> None:
        """Put the console in rest mode (it must be awake)."""
        if self.state() != "ready":
            return
        result = goto_bed(self.host, self.regist_key, self.morning)
        if " 200" not in result:
            hint = " (close chiaki-ng or the PS Remote Play app and try again)" if "in use" in result else ""
            raise PS5Error(f"could not send the console to rest mode: {result}{hint}")
        self._wait(lambda: self.state() != "ready", 40, step=2.0)  # it answers for a few more seconds

    def _wake_until_ready(self, limit: float = 120) -> bool:
        """Send the wake packet and repeat it every 10 s until the console is ready. A console that entered rest mode
        less than about a minute ago ignores wake packets, so a single packet is not enough."""
        end = time.monotonic() + limit
        while time.monotonic() < end:
            self.wake()
            if self._wait(lambda: self.state() == "ready", min(10, max(end - time.monotonic(), 0)), step=2.0):
                return True
        return False

    def wake_and_wait(self) -> None:
        """Wake the console and wait until it reports ready (no app launch)."""
        if self.state() == "ready":
            return
        if not self._wake_until_ready():
            raise PS5Error("the console did not wake within 120 s (is 'Enable turning on PS5 from network' on?)")

    def stop_app(self) -> None:
        """Close the running app. websrv kills the running app before it launches the requested title, so asking for an
        empty title closes the app and then fails harmlessly (a made-up title id would show a 'not found' dialog)."""
        if not self.websrv_up():
            raise PS5Error("websrv is not answering")
        if not self.app_running():
            return
        self._http(f"http://{self.host}:{self.web_port}/launch?titleId=", timeout=20)
        if not self._wait(lambda: not self.app_running(), 30, step=1.0):
            raise PS5Error("PS5LM did not stop")

    def launch_once(self, title_id: str | None = None) -> None:
        """Ask websrv to launch the title. Its HTTP status is meaningless (503 even on success)."""
        self._http(f"http://{self.host}:{self.web_port}/launch?titleId={title_id or self.title_id}", timeout=20)

    def _wait(self, test: Callable[[], bool], limit: float, step: float = 3.0, stop: threading.Event | None = None) -> bool:
        end = time.monotonic() + limit
        while time.monotonic() < end:
            if test():
                return True
            if stop is not None and stop.wait(step):
                return False
            if stop is None:
                time.sleep(step)
        return False

    def start(self, title_id: str | None = None, ready_url: str | None = None, progress: Callable[[str], None] | None = None) -> dict[str, float]:
        """Wake the console if needed, sign the user in, launch the title, optionally wait for a URL. Returns step timings."""
        title = title_id or self.title_id
        ready = self.ready_url if ready_url is None else ready_url
        say = progress or (lambda message: _LOGGER.debug("%s", message))
        timings: dict[str, float] = {}
        t_start = time.monotonic()

        if self.app_running(title):
            say("app already running")
        else:
            if self.state() != "ready":
                say("waking the console")
                if not self._wake_until_ready():
                    raise PS5Error("the console did not wake within 120 s (is 'Enable turning on PS5 from network' on?)")
                timings["wake"] = time.monotonic() - t_start
                say("console awake")
            say("waiting for websrv")
            if not self._wait(self.websrv_up, 90):
                raise PS5Error("websrv is not answering (send websrv-ps5.elf to the console again after a jailbreak)")
            timings["websrv"] = time.monotonic() - t_start

            stop = threading.Event()

            def login_worker() -> None:
                # Right after a wake the Remote Play control port is not listening yet, and the console refuses
                # repeated session requests, so retry with a pause that depends on the failure.
                for attempt in range(1, 13):
                    if stop.is_set():
                        return
                    say(f"signing in via Remote Play (attempt {attempt})")
                    result = remote_play_login(self.host, self.regist_key, self.morning, 120, stop)
                    if stop.is_set() or " 200" in result:
                        say("signed in via Remote Play" if " 200" in result else "sign-in no longer needed")
                        return
                    say(f"Remote Play sign-in failed: {result}")
                    stop.wait(20 if "session request refused" in result else 10)

            worker = threading.Thread(target=login_worker, name="ps5-login", daemon=True)
            worker.start()
            t_launch = time.monotonic()
            say("launching the app")
            try:
                while not self.app_running(title):
                    if time.monotonic() - t_launch > 150:
                        raise PS5Error("PS5LM did not start within 150 s (no user signed in? wrong title id?)")
                    self.launch_once(title)
                    self._wait(lambda: bool(self.app_running(title)), 6, step=1.0)
            finally:
                stop.set()
            timings["launch"] = time.monotonic() - t_start
            say("app launched")

        if ready:
            say("waiting for the ready URL")
            if not self._wait(lambda: self._http(ready) == 200, 300):
                raise PS5Error(f"{ready} did not answer 200 within 300 s")
            timings["ready"] = time.monotonic() - t_start
        timings["total"] = time.monotonic() - t_start
        say("done")
        return timings

    # -- model selection ------------------------------------------------------------------------------------------------
    def list_models(self) -> list[str]:
        """List the model files available on the console (excluding mmproj files).
        
        Returns a list of model names (without .gguf extension) from GET /fs/data/PS5LM/models/?fmt=json.
        """
        data = self._get_text(f"http://{self.host}:{self.web_port}/fs/data/PS5LM/models/?fmt=json")
        if not data:
            return []
        try:
            names = [os.path.basename(e.get("name", "")) for e in json.loads(data)]
            return [n.removesuffix(".gguf") for n in names if n.lower().endswith(".gguf") and "mmproj" not in n.lower()]
        except (ValueError, TypeError):
            return []

    def read_console_file(self, path: str) -> str | None:
        """Read a file from the console via websrv.
        
        Args:
            path: The file path on the console (e.g., /data/PS5LM/app-args.txt)
            
        Returns:
            The file contents as a string, or None if the file doesn't exist or can't be read.
        """
        return self._get_text(f"http://{self.host}:{self.web_port}/fs{path}")

    def write_console_file(self, path: str, content: str) -> bool:
        """Write a file to the console via the /tools endpoint.
        
        Args:
            path: The file path on the console
            content: The file contents
            
        Returns:
            True if the write succeeded, False otherwise.
        """
        try:
            payload = json.dumps({"tool": "write_file", "params": {"path": path, "content": content}})
            sock = socket.create_connection((self.host, self.web_port), timeout=10)
            try:
                # POST with JSON body
                request = f"POST /tools HTTP/1.1\r\nHost: {self.host}:{self.web_port}\r\n" \
                          f"Content-Type: application/json\r\n" \
                          f"Content-Length: {len(payload)}\r\n" \
                          f"Connection: close\r\n\r\n{payload}"
                sock.sendall(request.encode())
                status, headers, _ = _recv_headers(sock)
                return " 200" in status
            finally:
                sock.close()
        except Exception:
            _LOGGER.exception("Failed to write file to console")
            return False

    def build_app_args_for_model(self, model_name: str, current_app_args: str | None = None) -> str:
        """Build the app-args.txt content for a given model.
        
        Args:
            model_name: The model file name (with .gguf extension)
            current_app_args: Optional current app-args.txt content to use as base
            
        Returns:
            The app-args.txt content as a string.
        """
        # Check if a profile exists for this model
        profile_path = f"/data/PS5LM/profiles/{model_name}.args"
        profile_content = self.read_console_file(profile_path)
        
        if profile_content:
            return profile_content
        
        # Build from current app-args or defaults
        if current_app_args:
            lines = current_app_args.splitlines()
        else:
            lines = []
        
        # Find and replace the model path
        new_lines = []
        model_path = f"/data/PS5LM/models/{model_name}.gguf"
        for line in lines:
            if line.strip().startswith("-m"):
                new_lines.append(f"-m {model_path}")
            elif line.strip().startswith("-c"):
                new_lines.append("-c 8192")
            elif line.strip().startswith("-ctk"):
                new_lines.append("-ctk q4_0")
            elif line.strip().startswith("-ctv"):
                new_lines.append("-ctv q4_0")
            elif line.strip().startswith("-ctxcp"):
                new_lines.append("-ctxcp 8")
            elif line.strip().startswith("--tools"):
                new_lines.append("--tools")
            else:
                new_lines.append(line)
        
        return "\n".join(new_lines)
