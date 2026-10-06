#!/usr/bin/env python3
"""Native messaging host: hands the bridge pairing token to the Firefox extension.

Firefox starts this program itself, and only for the extension ID listed in the host
manifest's allowed_extensions, so no token pasting is needed. It answers one message
type ({"type": "token"}) and exits when Firefox closes stdin. Stdlib only.
"""

import json
import os
import secrets
import struct
import sys
from pathlib import Path

HOME = Path(os.environ.get("BRIDGE_HOME", Path.home() / ".claude-browser-bridge"))
TOKEN_FILE = HOME / "token"


def token() -> str:
    # Same rules as server/bridge.py: reuse a valid token, otherwise create one.
    HOME.mkdir(parents=True, exist_ok=True)
    if TOKEN_FILE.exists():
        t = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if len(t) >= 32:
            return t
    t = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(t, encoding="utf-8")
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    return t


def read_message():
    raw = sys.stdin.buffer.read(4)
    if len(raw) < 4:
        return None
    (n,) = struct.unpack("=I", raw)
    if n > 1_000_000:
        return None
    return json.loads(sys.stdin.buffer.read(n))


def send(obj) -> None:
    data = json.dumps(obj).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("=I", len(data)) + data)
    sys.stdout.buffer.flush()


def note(msg: str) -> None:
    # Tiny troubleshooting log next to the host manifest (never contains the token).
    try:
        log = Path.home() / ".claude-browser-bridge" / "native" / "host.log"
        if log.parent.exists():
            if log.exists() and log.stat().st_size > 100_000:
                log.unlink()
            from datetime import datetime
            with log.open("a", encoding="utf-8") as f:
                f.write(f"{datetime.now().isoformat(timespec='seconds')} pid={os.getpid()} home={HOME} {msg}\n")
    except OSError:
        pass


def main() -> None:
    note("started")
    if os.name == "nt":  # binary-safe stdio on Windows
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    while True:
        msg = read_message()
        if msg is None:
            return
        if msg.get("type") == "token":
            send({"token": token()})
            note("served token")
        else:
            send({"error": f"unknown message type {msg.get('type')!r}"})


if __name__ == "__main__":
    main()
