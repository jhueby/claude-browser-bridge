#!/usr/bin/env python3
"""claude-browser-bridge: MCP server that lets Claude Code drive your Firefox.

Zero third-party dependencies (stdlib only). Two halves in one process:

  * MCP over stdio (newline-delimited JSON-RPC) to Claude Code.
  * A loopback-only HTTP long-poll endpoint the Firefox extension connects to.
    The extension pulls commands from GET /poll and returns results to POST /result.

Security model (see SECURITY.md):
  * Binds 127.0.0.1 only and rejects any Host header that is not loopback
    (blocks DNS-rebinding).
  * Rejects requests carrying a web Origin and all CORS preflights, so ordinary
    web pages cannot talk to it.
  * Requires a pairing token (~/.claude-browser-bridge/token) in X-Bridge-Token,
    compared in constant time.
  * Writes an audit log of every tool call (typed text is redacted).
"""

from __future__ import annotations

import base64
import hmac
import itertools
import json
import os
import queue
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "0.4.0"
PORT_RANGE = range(int(os.environ.get("BRIDGE_PORT_START", "8777")), int(os.environ.get("BRIDGE_PORT_START", "8777")) + 10)
HOME = Path(os.environ.get("BRIDGE_HOME", Path.home() / ".claude-browser-bridge"))
TOKEN_FILE = HOME / "token"
AUDIT_LOG = HOME / "audit.log"
SHOT_DIR = HOME / "screenshots"
POLL_WAIT = 20.0          # seconds a /poll request is held open
EXTENSION_GRACE = 8.0     # how long a tool call waits for an extension to show up
DEFAULT_TIMEOUT = 30.0
DEBUG = bool(os.environ.get("BRIDGE_DEBUG"))
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")


def log(msg: str) -> None:
    print(f"[bridge] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- token

def load_token() -> str:
    HOME.mkdir(parents=True, exist_ok=True)
    if TOKEN_FILE.exists():
        tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if len(tok) >= 32:
            return tok
    tok = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(tok, encoding="utf-8")
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    return tok


TOKEN = load_token()


# --------------------------------------------------------------------------- audit

_audit_lock = threading.Lock()
REDACT_KEYS = {"text", "value"}


def audit(event: str, **fields) -> None:
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "pid": os.getpid(), "event": event}
    for k, v in fields.items():
        if k == "args" and isinstance(v, dict):
            v = {ak: (f"<{len(str(av))} chars>" if ak in REDACT_KEYS else av) for ak, av in v.items()}
            if "code" in v:
                v["code"] = str(v["code"])[:500]
        rec[k] = v
    with _audit_lock:
        try:
            with AUDIT_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except OSError:
            pass


# --------------------------------------------------------------------------- command broker

class Broker:
    """Hands commands to whichever extension poll arrives next and routes results back."""

    def __init__(self) -> None:
        self.outbox: "queue.Queue[dict]" = queue.Queue()
        self.pending: dict[str, "queue.Queue[dict]"] = {}
        self.lock = threading.Lock()
        self.ids = itertools.count(1)
        self.last_poll = 0.0
        self.browser_info: dict = {}

    def connected(self) -> bool:
        return time.monotonic() - self.last_poll < POLL_WAIT + 5

    def call(self, tool: str, args: dict, timeout: float) -> dict:
        deadline = time.monotonic() + EXTENSION_GRACE
        while not self.connected():
            if time.monotonic() > deadline:
                raise BridgeError(
                    "No Firefox extension is connected to this bridge. Make sure Firefox is running, "
                    "the Claude Browser Bridge extension is installed and paired (token from "
                    f"{TOKEN_FILE}), and it is not paused."
                )
            time.sleep(0.25)
        cid = f"{os.getpid()}-{next(self.ids)}"
        box: "queue.Queue[dict]" = queue.Queue(maxsize=1)
        with self.lock:
            self.pending[cid] = box
        self.outbox.put({"id": cid, "tool": tool, "args": args, "timeout_ms": int(timeout * 1000)})
        try:
            res = box.get(timeout=timeout + 5)
        except queue.Empty:
            raise BridgeError(f"Timed out after {timeout:.0f}s waiting for the browser to run {tool}.")
        finally:
            with self.lock:
                self.pending.pop(cid, None)
        if not res.get("ok"):
            raise BridgeError(res.get("error") or "browser reported an unknown error")
        return res.get("result") or {}

    def next_command(self) -> dict | None:
        self.last_poll = time.monotonic()
        try:
            cmd = self.outbox.get(timeout=POLL_WAIT)
        except queue.Empty:
            return None
        finally:
            self.last_poll = time.monotonic()
        return cmd

    def deliver(self, res: dict) -> bool:
        with self.lock:
            box = self.pending.get(str(res.get("id")))
        if box is None:
            return False
        try:
            box.put_nowait(res)
        except queue.Full:
            return False
        return True


class BridgeError(Exception):
    pass


BROKER = Broker()


# --------------------------------------------------------------------------- HTTP (extension side)

class Handler(BaseHTTPRequestHandler):
    server_version = "claude-browser-bridge"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):  # keep stdout clean for MCP
        pass

    def _reply(self, code: int, body: dict | None = None) -> None:
        data = b"" if body is None else json.dumps(body).encode()
        if DEBUG:
            log(f"{self.command} {self.path} -> {code} origin={self.headers.get('Origin')}")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _allowed(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
        if host not in ("127.0.0.1", "localhost", "::1"):
            self._reply(403, {"error": "bad host"})
            return False
        origin = self.headers.get("Origin")
        if origin and not origin.startswith("moz-extension://"):
            self._reply(403, {"error": "bad origin"})
            return False
        if not hmac.compare_digest(self.headers.get("X-Bridge-Token", "").encode(), TOKEN.encode()):
            self._reply(401, {"error": "bad token"})
            return False
        return True

    def do_OPTIONS(self):
        self._reply(403, {"error": "no cross-origin access"})

    def do_GET(self):
        if not self._allowed():
            return
        if self.path.startswith("/poll"):
            cmd = BROKER.next_command()
            if cmd is None:
                self._reply(204)
            else:
                self._reply(200, cmd)
        elif self.path.startswith("/hello"):
            self._reply(200, {"name": "claude-browser-bridge", "version": VERSION, "pid": os.getpid(),
                              "cwd": os.getcwd()})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        if not self._allowed():
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > 50_000_000:
            self._reply(413, {"error": "too large"})
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._reply(400, {"error": "bad json"})
            return
        if self.path.startswith("/result"):
            self._reply(200, {"accepted": BROKER.deliver(body)})
        elif self.path.startswith("/shutdown"):
            # Sent by the extension popup's "Kill" button. The pid check stops a stale click
            # from killing a newer session that has since taken over this port.
            if body.get("pid") != os.getpid():
                self._reply(409, {"error": "pid mismatch", "pid": os.getpid()})
                return
            audit("killed", by="extension popup")
            log("shut down by the user from the Firefox extension")
            self._reply(200, {"bye": os.getpid()})
            threading.Timer(0.2, lambda: os._exit(0)).start()
        else:
            self._reply(404, {"error": "not found"})


class ExclusiveServer(ThreadingHTTPServer):
    """Refuse to share a port. http.server sets SO_REUSEADDR, which on Windows lets a second
    process bind a port that is already listening — two sessions would silently split traffic
    (or another program could squat on ours). Use SO_EXCLUSIVEADDRUSE there instead."""

    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self):
        if os.name == "nt":
            import socket
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        # Browsers drop long-polls when tabs/windows close; that's not worth a traceback on stderr.
        if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            super().handle_error(request, client_address)


def start_http() -> int:
    for port in PORT_RANGE:
        try:
            srv = ExclusiveServer(("127.0.0.1", port), Handler)
        except OSError:
            continue

        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return port
    raise SystemExit(f"[bridge] no free port in {PORT_RANGE.start}-{PORT_RANGE.stop - 1}")


# --------------------------------------------------------------------------- tool definitions

def _obj(props: dict, required: list[str] | None = None) -> dict:
    s = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        s["required"] = required
    return s


TAB = {"tab_id": {"type": "integer", "description": "Tab id from tabs_list. Defaults to the active tab of the focused window."}}
TARGET = {
    "ref": {"type": "string", "description": "Element ref from snapshot/find, e.g. 'e12', or 'f23:e7' for an element inside iframe f23."},
    "selector": {"type": "string", "description": "CSS selector."},
    "text": {"type": "string", "description": "Visible text of the element (exact match preferred, then substring)."},
}

FRAME = {"frame": {"type": "string", "description": "Iframe to act in, e.g. 'f23' from snapshot. Optional: refs already carry their frame, and selector/text targets not found in the top page are searched for in iframes."}}

TOOLS: list[dict] = [
    {"name": "tabs_list", "description": "List open Firefox tabs (id, title, url, active, window).",
     "inputSchema": _obj({})},
    {"name": "tab_open", "description": "Open a new tab, optionally at a URL.",
     "inputSchema": _obj({"url": {"type": "string"}, "active": {"type": "boolean", "default": True}})},
    {"name": "tab_close", "description": "Close a tab.", "inputSchema": _obj(TAB, ["tab_id"])},
    {"name": "tab_focus", "description": "Bring a tab (and its window) to the front.", "inputSchema": _obj(TAB, ["tab_id"])},
    {"name": "navigate", "description": "Go to a URL, or go back/forward/reload. hard=true bypasses the cache.",
     "inputSchema": _obj({**TAB, "url": {"type": "string"},
                          "action": {"type": "string", "enum": ["goto", "back", "forward", "reload"], "default": "goto"},
                          "hard": {"type": "boolean", "default": False}})},
    {"name": "reload_matching", "description": "Reload every tab whose URL contains a substring (e.g. 'localhost:3000') — handy after restarting a dev server.",
     "inputSchema": _obj({"url_contains": {"type": "string"}, "hard": {"type": "boolean", "default": True}}, ["url_contains"])},
    {"name": "screenshot", "description": "Capture the visible part of a tab. Returns the image; also saved to disk unless save=false.",
     "inputSchema": _obj({**TAB, "max_width": {"type": "integer", "default": 1280},
                          "format": {"type": "string", "enum": ["jpeg", "png"], "default": "jpeg"},
                          "save": {"type": "boolean", "default": True}})},
    {"name": "snapshot", "description": "Outline of the page: headings and every visible interactive element with a ref (e.g. [e7] button \"Save\"), including inside iframes (nested under [frame fN], refs like fN:e3) and shadow DOM. Use refs with click/type/etc.",
     "inputSchema": _obj({**TAB, **FRAME, "max_items": {"type": "integer", "default": 300},
                          "include_frames": {"type": "boolean", "default": True, "description": "Expand iframes inline."}})},
    {"name": "get_text", "description": "Visible text of the page or of one element.",
     "inputSchema": _obj({**TAB, **TARGET, **FRAME, "max_chars": {"type": "integer", "default": 20000}})},
    {"name": "find", "description": "Find elements by visible text or CSS selector, in the page and its iframes; returns refs.",
     "inputSchema": _obj({**TAB, **FRAME, "query": {"type": "string", "description": "Text to look for, or CSS selector if css=true."},
                          "css": {"type": "boolean", "default": False}, "limit": {"type": "integer", "default": 20}}, ["query"])},
    {"name": "click", "description": "Click an element (by ref, selector, or text), or at x/y given in pixels of the most recent screenshot of that tab (clicks pass into iframes; with `frame`, x/y are CSS pixels in that frame).",
     "inputSchema": _obj({**TAB, **TARGET, **FRAME, "x": {"type": "number"}, "y": {"type": "number"},
                          "double": {"type": "boolean", "default": False}})},
    {"name": "type", "description": "Type text into an input, textarea, or contenteditable. Refuses password fields.",
     "inputSchema": _obj({**TAB, **FRAME, "ref": TARGET["ref"], "selector": TARGET["selector"],
                          "text": {"type": "string", "description": "Text to enter."},
                          "clear": {"type": "boolean", "default": True},
                          "submit": {"type": "boolean", "default": False, "description": "Submit the enclosing form / press Enter afterwards."}},
                         ["text"])},
    {"name": "press_key", "description": "Send a key (Enter, Escape, Tab, ArrowDown, a, ...) to the focused element or a target.",
     "inputSchema": _obj({**TAB, **FRAME, "key": {"type": "string"}, "ref": TARGET["ref"], "selector": TARGET["selector"]}, ["key"])},
    {"name": "select_option", "description": "Choose an option in a <select> by value or visible label.",
     "inputSchema": _obj({**TAB, **FRAME, "ref": TARGET["ref"], "selector": TARGET["selector"], "option": {"type": "string"}}, ["option"])},
    {"name": "hover", "description": "Move the (synthetic) pointer over an element.",
     "inputSchema": _obj({**TAB, **TARGET, **FRAME})},
    {"name": "scroll", "description": "Scroll the page by a number of pixels, to top/bottom, or until an element is in view.",
     "inputSchema": _obj({**TAB, **TARGET, **FRAME, "dy": {"type": "number", "description": "Pixels; negative scrolls up."},
                          "to": {"type": "string", "enum": ["top", "bottom"]}})},
    {"name": "wait_for", "description": "Wait until a selector or text appears (or disappears with gone=true).",
     "inputSchema": _obj({**TAB, **FRAME, "selector": TARGET["selector"], "text": TARGET["text"],
                          "gone": {"type": "boolean", "default": False},
                          "timeout_ms": {"type": "integer", "default": 10000}})},
    {"name": "evaluate", "description": "Run JavaScript in the page (main world). Expression or function body; may use await; return value is JSON-serialised. Subject to the page's CSP.",
     "inputSchema": _obj({**TAB, **FRAME, "code": {"type": "string"}}, ["code"])},
    {"name": "logs_start", "description": "Start capturing console messages and fetch/XHR requests in a tab or one of its iframes (resets on navigation).",
     "inputSchema": _obj({**TAB, **FRAME})},
    {"name": "logs_read", "description": "Return captured console/network entries.",
     "inputSchema": _obj({**TAB, **FRAME, "kind": {"type": "string", "enum": ["all", "console", "network"], "default": "all"},
                          "errors_only": {"type": "boolean", "default": False},
                          "clear": {"type": "boolean", "default": False}})},
    {"name": "bridge_status", "description": "Show whether a browser is connected to this bridge, its port, and file locations.",
     "inputSchema": _obj({})},
]
TOOL_NAMES = {t["name"] for t in TOOLS}


def run_tool(name: str, args: dict) -> list[dict]:
    if name == "bridge_status":
        return [text(json.dumps({
            "version": VERSION, "port": PORT, "browser_connected": BROKER.connected(),
            "token_file": str(TOKEN_FILE), "audit_log": str(AUDIT_LOG), "screenshots": str(SHOT_DIR),
        }, indent=2))]

    timeout = DEFAULT_TIMEOUT
    if name == "wait_for":
        timeout = max(DEFAULT_TIMEOUT, args.get("timeout_ms", 10000) / 1000 + 5)
    result = BROKER.call(name, args, timeout)

    if name == "screenshot":
        mime = result.get("mime", "image/jpeg")
        data = result["data"]
        parts = [{"type": "image", "data": data, "mimeType": mime}]
        meta = {k: v for k, v in result.items() if k not in ("data",)}
        if args.get("save", True):
            SHOT_DIR.mkdir(parents=True, exist_ok=True)
            ext = "png" if mime.endswith("png") else "jpg"
            path = SHOT_DIR / f"shot-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.{ext}"
            path.write_bytes(base64.b64decode(data))
            meta["saved_to"] = str(path)
        parts.append(text(json.dumps(meta)))
        return parts

    if isinstance(result, dict) and "text" in result and set(result) <= {"text", "frame"}:
        return [text((f"[in frame {result['frame']}]\n" if result.get("frame") else "") + result["text"])]
    return [text(json.dumps(result, indent=2, ensure_ascii=False))]


def text(s: str) -> dict:
    return {"type": "text", "text": s}


# --------------------------------------------------------------------------- MCP (stdio side)

_out_lock = threading.Lock()


def send(msg: dict) -> None:
    line = json.dumps(msg, ensure_ascii=False)
    with _out_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def handle_call(mid, params: dict) -> None:
    name = params.get("name")
    args = params.get("arguments") or {}
    if name not in TOOL_NAMES:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {name}"}})
        return
    audit("call", tool=name, args=args)
    try:
        content = run_tool(name, args)
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": content, "isError": False}})
        audit("ok", tool=name)
    except BridgeError as e:
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [text(str(e))], "isError": True}})
        audit("error", tool=name, error=str(e))
    except Exception as e:  # never kill the server over one call
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [text(f"internal error: {e!r}")], "isError": True}})
        audit("error", tool=name, error=repr(e))


def handle(msg: dict) -> None:
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        want = (msg.get("params") or {}).get("protocolVersion")
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": want if want in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "claude-browser-bridge", "version": VERSION},
            "instructions": "Drives the user's real Firefox (their logged-in sessions). Start with tabs_list or "
                            "snapshot; act on elements by ref. Ask the user before submitting forms, sending "
                            "messages, purchasing, or anything irreversible.",
        }})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        threading.Thread(target=handle_call, args=(mid, msg.get("params") or {}), daemon=True).start()
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}})
    # notifications (no id) are ignored


def main() -> None:
    global PORT
    if len(sys.argv) > 1 and sys.argv[1] in ("--token", "token"):
        print(TOKEN)
        return
    sys.stdout.reconfigure(encoding="utf-8")
    PORT = start_http()
    log(f"v{VERSION} listening on 127.0.0.1:{PORT}; token file {TOKEN_FILE}")
    audit("start", port=PORT, cwd=os.getcwd())
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        for m in (msg if isinstance(msg, list) else [msg]):
            handle(m)
    audit("stop", port=PORT)


PORT = 0

if __name__ == "__main__":
    main()
