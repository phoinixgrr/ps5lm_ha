"""Tests for tools/chiaki_keys.py (Qt INI parsing used on Linux)."""

from __future__ import annotations

from tools import chiaki_keys

CONF = """\
[General]
log_verbose=false

[registered_hosts]
1\\rp_key=@ByteArray(\\xde\\xad\\xbe\\xef\\0\\x01\\x02\\x03\\x04\\x05\\x06\\a\\b\\t\\n\\x0b)
1\\rp_regist_key=@ByteArray(abcd1234\\0\\0\\0\\0\\0\\0\\0\\0)
1\\server_nickname=PS5
size=1
"""


def test_parse_prefixes_sections():
    raw = chiaki_keys.parse_qt_ini(CONF)
    assert raw["log_verbose"] == "false"
    assert raw["registered_hosts.size"] == "1"
    assert raw["registered_hosts.1.server_nickname"] == "PS5"


def test_bytearray_escapes():
    raw = chiaki_keys.parse_qt_ini(CONF)
    assert raw["registered_hosts.1.rp_key"] == b"\xde\xad\xbe\xef\x00\x01\x02\x03\x04\x05\x06\a\b\t\n\x0b"


def test_hosts_from_linux_conf(tmp_path, monkeypatch):
    conf = tmp_path / "Chiaki.conf"
    conf.write_text(CONF)
    monkeypatch.setattr(chiaki_keys, "MAC_PLIST", str(tmp_path / "missing.plist"))
    monkeypatch.setattr(chiaki_keys, "LINUX_CONF", [str(conf)])
    assert chiaki_keys.hosts() == [
        {"name": "PS5", "regist_key": "abcd1234", "morning": "deadbeef000102030405060708090a0b"}
    ]


def test_crlf_line_endings():
    raw = chiaki_keys.parse_qt_ini(CONF.replace("\n", "\r\n"))
    assert raw["registered_hosts.size"] == "1"
