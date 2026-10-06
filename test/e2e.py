"""End-to-end test: real Firefox (throwaway profile via web-ext) + bridge.py over MCP stdio.

    python test/e2e.py [--firefox PATH]

Needs Node (npx web-ext). Uses an isolated BRIDGE_HOME so your real token is untouched.
"""
import argparse
import functools
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ap = argparse.ArgumentParser()
ap.add_argument("--firefox", default=r"C:\Program Files\Mozilla Firefox\firefox.exe" if os.name == "nt" else "firefox")
opts = ap.parse_args()
sys.stdout.reconfigure(encoding="utf-8")

work = Path(tempfile.mkdtemp(prefix="cbb-e2e-"))
home = work / "home"
env = {**os.environ, "BRIDGE_HOME": str(home)}

# 1. test page server
class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


handler = functools.partial(_Quiet, directory=str(ROOT / "test"))
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
page = f"http://127.0.0.1:{httpd.server_address[1]}/page.html"

# 2. bridge
bridge = subprocess.Popen([sys.executable, str(ROOT / "server" / "bridge.py")], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, env=env, text=True, encoding="utf-8")
token = (home / "token").read_text().strip() if (home / "token").exists() else None
for _ in range(50):
    if (home / "token").exists():
        token = (home / "token").read_text().strip()
        break
    time.sleep(0.1)

# 3. extension copy with pairing.json, launched in a temp profile
ext = work / "ext"
shutil.copytree(ROOT / "extension", ext)
(ext / "pairing.json").write_text(json.dumps({"token": token}))
npx = "npx.cmd" if os.name == "nt" else "npx"
fx = subprocess.Popen([npx, "--yes", "web-ext", "run", "--source-dir", str(ext), "--firefox", opts.firefox,
                       "--start-url", page, "--no-reload"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

ids = iter(range(1, 10**6))


def rpc(method, params=None):
    mid = next(ids)
    bridge.stdin.write(json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}) + "\n")
    bridge.stdin.flush()
    while True:
        msg = json.loads(bridge.stdout.readline())
        if msg.get("id") == mid:
            return msg


def call(name, **args):
    r = rpc("tools/call", {"name": name, "arguments": args})["result"]
    out = []
    for c in r["content"]:
        out.append(c["text"] if c["type"] == "text" else f"<{c['type']} {c['mimeType']} {len(c['data'])} b64 chars>")
    return r["isError"], "\n".join(out)


failures = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"\n     {detail[:600]}"))
    if not cond:
        failures.append(label)


try:
    init = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "0"}})
    check("initialize", init["result"]["serverInfo"]["name"] == "claude-browser-bridge")
    tools = rpc("tools/list")["result"]["tools"]
    check("tools/list", len(tools) >= 20, str(len(tools)))

    # wait for Firefox + extension to connect and the page to load
    tab_id = None
    for _ in range(90):
        err, out = call("tabs_list")
        if not err:
            tabs = json.loads(out)["tabs"]
            hit = [t for t in tabs if t["url"].startswith(page) and t["status"] == "complete"]
            if hit:
                tab_id = hit[0]["id"]
                break
        time.sleep(1)
    check("extension connected and page open", tab_id is not None, out)
    if tab_id is None:
        raise SystemExit(1)

    err, snap = call("snapshot", tab_id=tab_id)
    check("snapshot lists elements", not err and 'button "Count 0"' in snap and 'textbox "Search"' in snap, snap)
    print(snap)

    err, out = call("click", tab_id=tab_id, text="Count 0")
    check("click by text", not err and "Count 1" in out, out)

    err, out = call("logs_start", tab_id=tab_id)
    check("logs_start", not err, out)
    call("click", tab_id=tab_id, selector="#inc")
    time.sleep(0.5)
    err, out = call("logs_read", tab_id=tab_id)
    check("logs capture console + fetch", not err and "clicked 2" in out and "ping=2" in out, out)

    ref = next(l.split("]")[0][1:] for l in snap.splitlines() if 'textbox "Search"' in l)
    err, out = call("type", tab_id=tab_id, ref=ref, text="hello world")
    check("type by ref", not err and 'value=\\"hello world' in out, out)

    err, out = call("type", tab_id=tab_id, selector="#pw", text="secret")
    check("password field refused", err and "password" in out.lower(), out)

    err, out = call("select_option", tab_id=tab_id, text="Color", option="Green")
    check("select_option by label", not err and '"g"' in out, out)

    err, out = call("press_key", tab_id=tab_id, ref=ref, key="Enter")
    check("press Enter submits", not err and "submitted" in out, out)
    err, out = call("get_text", tab_id=tab_id, selector="#out")
    check("form result", out.strip() == "submitted:hello world:g", out)

    err, out = call("evaluate", tab_id=tab_id, code="document.title + ' / ' + (await Promise.resolve(41 + 1))")
    check("evaluate expression with await", not err and "Bridge Test Page / 42" in out, out)
    err, out = call("evaluate", tab_id=tab_id, code="const a = [1,2,3]; return a.map(x => x * 2);")
    check("evaluate function body", not err and "6" in out, out)

    call("click", tab_id=tab_id, text="Spawn later")
    err, out = call("wait_for", tab_id=tab_id, selector="#late", timeout_ms=5000)
    check("wait_for appears", not err and "Late arrival" in out, out)
    err, out = call("wait_for", tab_id=tab_id, selector="#nope", timeout_ms=600)
    check("wait_for times out cleanly", err and "timed out" in out, out)

    err, out = call("find", tab_id=tab_id, query="Second page")
    check("find", not err and "link" in out and "second=1" in out, out)

    err, out = call("screenshot", tab_id=tab_id, max_width=800)
    check("screenshot returns image + saves", not err and "<image image/jpeg" in out and "saved_to" in out, out)

    err, out = call("click", tab_id=tab_id, text="Second page")
    check("click link navigates", not err and "second=1" in out, out)
    err, out = call("navigate", tab_id=tab_id, action="back")
    check("navigate back", not err and json.loads(out)["url"] == page, out)

    err, out = call("reload_matching", url_contains="127.0.0.1:" + str(httpd.server_address[1]))
    check("reload_matching", not err and str(tab_id) in out, out)

    err, out = call("navigate", tab_id=tab_id, url="about:blank")
    err2, out2 = call("snapshot", tab_id=tab_id)
    check("internal pages refused", err2 and "internal" in out2, out2)

    err, out = call("bridge_status")
    check("bridge_status", '"browser_connected": true' in out, out)

    audit = (home / "audit.log").read_text()
    check("audit log redacts typed text", "hello world" not in audit and "secret" not in audit and '"tool": "type"' in audit, audit[-500:])
finally:
    bridge.stdin.close()
    bridge.terminate()
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(fx.pid)], capture_output=True)
    else:
        fx.terminate()
    httpd.shutdown()

print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED: ' + ', '.join(failures)}")
sys.exit(1 if failures else 0)
