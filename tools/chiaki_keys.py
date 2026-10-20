#!/usr/bin/env python3
"""Print the Remote Play keys that chiaki-ng stored when you registered your PS5.

The PS5LM integration needs two of them:
  regist key   (chiaki-ng: rp_regist_key, shown here as hex)
  morning key  (chiaki-ng: rp_key, 32 hex characters)

Register the console in chiaki-ng once (on the PS5, Settings > System > Remote Play > Link Device shows the PIN), then run:
  python3 chiaki_keys.py
Reads ~/Library/Preferences/com.chiaki.Chiaki.plist on macOS and ~/.config/Chiaki/Chiaki.conf on Linux.
On Windows chiaki-ng keeps them in the registry (HKCU\\Software\\Chiaki\\Chiaki\\registered_hosts).
"""

from __future__ import annotations

import os
import plistlib
import re
import sys

MAC_PLIST = os.path.expanduser("~/Library/Preferences/com.chiaki.Chiaki.plist")
LINUX_CONF = [os.path.expanduser(p) for p in ("~/.config/Chiaki/Chiaki.conf", "~/.var/app/io.github.streetpea.Chiaki4deck/config/Chiaki/Chiaki.conf")]


def _qt_bytearray(value: str) -> bytes:
    """Decode a Qt settings value written as @ByteArray(...) with C-style escapes."""
    match = re.fullmatch(r"@ByteArray\((.*)\)", value.strip())
    if not match:
        return value.encode()
    out, text, i = bytearray(), match.group(1), 0
    while i < len(text):
        ch = text[i]
        if ch != "\\":
            out += ch.encode("latin-1"); i += 1; continue
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if nxt == "x":
            hexdigits = re.match(r"[0-9a-fA-F]{1,2}", text[i + 2:])
            out.append(int(hexdigits.group(0), 16)); i += 2 + len(hexdigits.group(0))
        elif nxt == "0":
            out.append(0); i += 2
        else:
            out += {"n": b"\n", "r": b"\r", "t": b"\t", "\\": b"\\", '"': b'"'}.get(nxt, nxt.encode("latin-1")); i += 2
    return bytes(out)


def hosts() -> list[dict[str, bytes | str]]:
    raw: dict[str, object] = {}
    if os.path.exists(MAC_PLIST):
        raw = plistlib.load(open(MAC_PLIST, "rb"))
    else:
        conf = next((p for p in LINUX_CONF if os.path.exists(p)), None)
        if conf is None:
            sys.exit("chiaki-ng settings not found: register the console in chiaki-ng first")
        for line in open(conf, encoding="utf-8", errors="replace"):
            if "=" in line:
                key, value = line.rstrip("\n").split("=", 1)
                raw[key.replace("\\", ".")] = _qt_bytearray(value) if value.startswith("@ByteArray") else value
    found = []
    for n in range(1, int(raw.get("registered_hosts.size", 0) or 0) + 1):
        regist, morning = raw.get(f"registered_hosts.{n}.rp_regist_key"), raw.get(f"registered_hosts.{n}.rp_key")
        if isinstance(regist, (bytes, bytearray)) and isinstance(morning, (bytes, bytearray)):
            found.append({"name": str(raw.get(f"registered_hosts.{n}.server_nickname", f"host {n}")),
                          "regist_key": bytes(regist).split(b"\0")[0].decode(), "morning": bytes(morning).hex()})
    return found


if __name__ == "__main__":
    entries = hosts()
    if not entries:
        sys.exit("no registered console found in chiaki-ng's settings")
    for entry in entries:
        print(f"{entry['name']}\n  regist key:  {entry['regist_key']}\n  morning key: {entry['morning']}\n")
