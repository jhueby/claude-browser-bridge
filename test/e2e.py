"""End-to-end test: real Firefox (throwaway profile via web-ext) + bridge.py over MCP stdio.

    python test/e2e.py [--firefox PATH] [--allowlist]

--allowlist seeds allowed=["127.0.0.1"] and adds checks that other hosts are refused/hidden.
--native seeds no token and starts paused, then checks (via the popup) that the installed
  native host (python native/install.py) paired the extension automatically.

Needs Node (npx web-ext). Uses an isolated BRIDGE_HOME so your real token is untouched.
"""
import argparse
import functools
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ap = argparse.ArgumentParser()
ap.add_argument("--firefox", default=r"C:\Program Files\Mozilla Firefox\firefox.exe" if os.name == "nt" else "firefox")
ap.add_argument("--allowlist", action="store_true")
ap.add_argument("--native", action="store_true",
                help="no seeded token: the extension must pair itself via the installed native host")
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
httpd.handle_error = lambda *a: None  # Firefox resets connections when it's killed at the end
threading.Thread(target=httpd.serve_forever, daemon=True).start()
page = f"http://127.0.0.1:{httpd.server_address[1]}/page.html"

# 2. bridge
def start_bridge():
    return subprocess.Popen([sys.executable, str(ROOT / "server" / "bridge.py")], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, env=env, text=True, encoding="utf-8")


bridge = start_bridge()
token = (home / "token").read_text().strip() if (home / "token").exists() else None
for _ in range(50):
    if (home / "token").exists():
        token = (home / "token").read_text().strip()
        break
    time.sleep(0.1)

# 3. extension copy with pairing.json, launched in a temp profile
ext = work / "ext"
shutil.copytree(ROOT / "extension", ext)
if opts.native:
    # Firefox doesn't pass our BRIDGE_HOME to the native host, so the host serves the user's REAL
    # token. Start paused so this throwaway browser can never take commands from real sessions.
    seed = {"settings": {"paused": True}}
else:
    seed = {"token": token, "settings": {"allowEvaluate": True, **({"allowed": ["127.0.0.1"]} if opts.allowlist else {})}}
(ext / "pairing.json").write_text(json.dumps(seed))
npx = "npx.cmd" if os.name == "nt" else "npx"
MARIONETTE = 28282
# A Firefox left over from an earlier (killed) run would answer on this port and we'd test the wrong browser.
with socket.socket() as _s:
    if _s.connect_ex(("127.0.0.1", MARIONETTE)) == 0:
        sys.exit(f"port {MARIONETTE} is already in use — a test Firefox from an earlier run is probably still open; close it first")
# Prefs go through a config file: npx.cmd runs via cmd.exe, which mangles quoting on the command line.
cfg = work / "web-ext-config.cjs"
cfg.write_text("module.exports = " + json.dumps({"run": {"pref": [
    f"marionette.port={MARIONETTE}",
]}}) + ";\n", encoding="utf-8")
fx = subprocess.Popen([npx, "--yes", "web-ext", "run", "--config", str(cfg), "--source-dir", str(ext),
                       "--firefox", opts.firefox, "--start-url", page, "--no-reload", "--arg=--marionette", "--arg=--remote-allow-system-access"],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Marionette:
    """Just enough of Firefox's Marionette protocol (len:json framing) to click popup buttons."""

    def __init__(self, port, tries=60):
        for _ in range(tries):
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
                break
            except OSError:
                time.sleep(1)
        else:
            raise RuntimeError("marionette not reachable")
        self.n = 0
        self._read()  # server hello
        self.cmd("WebDriver:NewSession", {"capabilities": {}})

    def _read(self):
        buf = b""
        while not buf.endswith(b":"):
            buf += self.sock.recv(1)
        need = int(buf[:-1])
        data = b""
        while len(data) < need:
            data += self.sock.recv(need - len(data))
        return json.loads(data)

    def cmd(self, name, params=None):
        self.n += 1
        data = json.dumps([0, self.n, name, params or {}]).encode()
        self.sock.sendall(str(len(data)).encode() + b":" + data)
        while True:
            msg = self._read()
            if msg[0] == 1 and msg[1] == self.n:
                if msg[2]:
                    raise RuntimeError(f"{name}: {msg[2]}")
                return msg[3]

    def open_extension_page(self, addon_id, path, timeout=10):
        # Content-context navigation to moz-extension:// is forbidden, so open a tab from
        # chrome context (needs --remote-allow-system-access) and switch to it.
        self.cmd("Marionette:SetContext", {"value": "chrome"})
        host = self.cmd("WebDriver:ExecuteScript", {"args": [addon_id], "script":
                        "return WebExtensionPolicy.getByID(arguments[0]).mozExtensionHostname;"})["value"]
        url = f"moz-extension://{host}/{path}"
        self.cmd("WebDriver:ExecuteScript", {"args": [url], "script":
                 "const w = Services.wm.getMostRecentWindow('navigator:browser');"
                 "w.gBrowser.selectedTab = w.gBrowser.addTab(arguments[0],"
                 " {triggeringPrincipal: Services.scriptSecurityManager.getSystemPrincipal()});"})
        self.cmd("Marionette:SetContext", {"value": "content"})
        deadline = time.time() + timeout
        while time.time() < deadline:
            for h in self.cmd("WebDriver:GetWindowHandles"):
                self.cmd("WebDriver:SwitchToWindow", {"handle": h})
                if self.cmd("WebDriver:GetCurrentURL")["value"].startswith(url):
                    return
            time.sleep(0.5)
        raise RuntimeError(f"extension page {url} never opened")

    def text(self, css):
        el = self.cmd("WebDriver:FindElement", {"using": "css selector", "value": css})["value"]
        return self.cmd("WebDriver:GetElementText", {"id": list(el.values())[0]})["value"]

    def click(self, xpath, timeout=8):
        deadline = time.time() + timeout
        while True:
            try:
                el = self.cmd("WebDriver:FindElement", {"using": "xpath", "value": xpath})["value"]
                self.cmd("WebDriver:ElementClick", {"id": list(el.values())[0]})
                return
            except RuntimeError:
                if time.time() > deadline:
                    raise
                time.sleep(0.3)

ids = iter(range(1, 10**6))
mn = None  # Marionette connection, if a test opened one; used to quit Firefox cleanly at the end


def rpc(method, params=None, proc=None):
    proc = proc or bridge
    mid = next(ids)
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}) + "\n")
    proc.stdin.flush()
    while True:
        msg = json.loads(proc.stdout.readline())
        if msg.get("id") == mid:
            return msg


def call(name, _proc=None, **args):
    r = rpc("tools/call", {"name": name, "arguments": args}, _proc)["result"]
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

    if opts.native:
        mn = Marionette(MARIONETTE)
        mn.open_extension_page("claude-browser-bridge@jhueby", "popup.html")
        state = ""
        for _ in range(20):
            state = mn.text("#tokenState")
            if "native" in state:
                break
            time.sleep(0.5)
        check("native host paired the extension automatically", "native host" in state, state)
        real = (Path.home() / ".claude-browser-bridge" / "token").read_text().strip()
        page_text = mn.cmd("WebDriver:GetPageSource")["value"]
        check("popup never displays the token", real not in page_text, "token visible in popup")
        check("evaluate is off by default on a fresh install", mn.cmd("WebDriver:ExecuteScript", {
            "script": "return document.getElementById('allowEvaluate').checked", "args": []})["value"] is False, "checked")
        check("stays paused (no polling with the real token)", mn.cmd("WebDriver:ExecuteScript", {
            "script": "return document.getElementById('paused').checked", "args": []})["value"] is True, "not paused")
        raise SystemExit(0 if not failures else 1)

    # wait for Firefox + extension to connect and the page to load
    tab_id = None
    for _ in range(12):  # each failed call already waits ~8s for the extension
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

    # ---- iframes (same-origin, cross-origin, nested) and closed shadow DOM ----
    import re
    err, snap = call("snapshot", tab_id=tab_id)
    print(snap)

    def ref_for(label, text=snap):
        m = re.search(r"\[((?:f\d+:)?e\d+)\][^\n]*" + re.escape(label), text)
        return m.group(1) if m else None

    def frame_of(label, text=snap):
        r = ref_for(label, text)
        return r.split(":")[0] if r and ":" in r else None

    check("snapshot shows shadow DOM button", 'button "Shadow button"' in snap, snap)
    if opts.allowlist:
        check("allowlist: same-origin frame expanded", 'button "Frame button same"' in snap, snap)
        check("allowlist: cross-origin (off-list) frame hidden", "hidden by site rules" in snap
              and "Frame button cross" not in snap and "Frame button leaf" not in snap, snap)
        hidden = re.search(r"\[frame (f\d+)\] hidden by site rules", snap)
        err, out = call("click", tab_id=tab_id, text="Frame button cross")
        check("allowlist: text search skips off-list frames", err and "no visible element" in out, out)
        err, out = call("get_text", tab_id=tab_id, frame=hidden.group(1) if hidden else "f999999")
        check("allowlist: explicit off-list frame refused", err and "allowlist" in out, out)
    else:
        check("snapshot expands same-origin iframe", 'button "Frame button same"' in snap, snap)
        check("snapshot expands cross-origin iframe", 'button "Frame button cross"' in snap, snap)
        check("snapshot expands nested iframe", 'button "Frame button leaf"' in snap, snap)
        check("frame refs are prefixed", bool(re.search(r"\[f\d+:e\d+\] button \"Frame button cross\"", snap)), snap)

        cref = ref_for('button "Frame button cross"')
        err, out = call("click", tab_id=tab_id, ref=cref)
        check("click by frame ref (cross-origin)", not err and "Frame button cross" in out, out)
        err, out = call("get_text", tab_id=tab_id, frame=frame_of('button "Frame button cross"'), selector="#fout")
        check("get_text in cross-origin frame", not err and out.strip().endswith("cross clicked 1") and "[in frame f" in out, out)

        err, out = call("click", tab_id=tab_id, text="Frame button leaf")
        check("click by text finds nested frame", not err and '"frame": "f' in out, out)
        err, out = call("get_text", tab_id=tab_id, frame=frame_of('button "Frame button leaf"'), selector="#fout")
        check("nested frame received click", out.strip().endswith("leaf clicked 1"), out)

        fref = ref_for('textbox "Field same"')
        err, out = call("type", tab_id=tab_id, ref=fref, text="inside a frame")
        check("type into frame field", not err and "inside a frame" in out, out)

        err, out = call("find", tab_id=tab_id, query="Frame button")
        check("find searches every frame", not err and out.count("Frame button") >= 3 and "[frame f" in out, out)

        err, out = call("evaluate", tab_id=tab_id, frame=frame_of('button "Frame button cross"'), code="document.title")
        check("evaluate in a frame", not err and "Frame cross" in out, out)

        # coordinate click that lands inside the same-origin iframe
        err, shot = call("screenshot", tab_id=tab_id, max_width=4000, save=False)
        img_w = json.loads(shot.split("\n", 1)[1])["width"]
        err, out = call("evaluate", tab_id=tab_id, code="(() => { const f = document.getElementById('same'); f.scrollIntoView({block:'center'}); const r = f.getBoundingClientRect(); return {l: r.left + f.clientLeft, t: r.top + f.clientTop, w: innerWidth}; })()")
        fr = json.loads(out)["value"]
        err, out = call("evaluate", tab_id=tab_id, frame=frame_of('button "Frame button same"'),
                        code="(() => { const r = document.getElementById('fb').getBoundingClientRect(); return {x: r.left + r.width/2, y: r.top + r.height/2}; })()")
        bt = json.loads(out)["value"]
        err, shot = call("screenshot", tab_id=tab_id, max_width=4000, save=False)  # re-shoot after scroll
        k = img_w / fr["w"]
        err, out = call("click", tab_id=tab_id, x=(fr["l"] + bt["x"]) * k, y=(fr["t"] + bt["y"]) * k)
        check("coordinate click descends into iframe", not err and "Frame button same" in out and '"frame": "f' in out, out)

        err, out = call("click", tab_id=tab_id, text="Shadow button")
        check("click inside closed shadow root", not err and "Shadow button" in out, out)
        err, out = call("find", tab_id=tab_id, query="shadow clicked")
        check("closed shadow root content readable", not err and "shadow clicked" in out, out)

    err, out = call("click", tab_id=tab_id, text="Second page")
    check("click link navigates", not err and "second=1" in out, out)
    err, out = call("navigate", tab_id=tab_id, action="back")
    check("navigate back", not err and json.loads(out)["url"] == page, out)

    err, out = call("reload_matching", url_contains="127.0.0.1:" + str(httpd.server_address[1]))
    check("reload_matching", not err and str(tab_id) in out, out)

    err, out = call("navigate", tab_id=tab_id, url="about:blank")
    if opts.allowlist:
        check("allowlist: navigating to about:blank refused", err and "allowlist" in out, out)
    else:
        err2, out2 = call("snapshot", tab_id=tab_id)
        check("internal pages refused", err2 and "internal" in out2, out2)
    call("navigate", tab_id=tab_id, url=page)

    if opts.allowlist:
        other = page.replace("127.0.0.1", "localhost")
        err, out = call("navigate", tab_id=tab_id, url=other)
        check("allowlist: navigate to other host refused", err and "allowlist" in out, out)
        err, out = call("tab_open", url=other)
        check("allowlist: tab_open other host refused", err and "allowlist" in out, out)
        err, out = call("click", tab_id=tab_id, text="Other host")  # page link -> localhost
        time.sleep(1.5)
        err, out = call("snapshot", tab_id=tab_id)
        check("allowlist: tab that wandered off-list is refused", err and "allowlist" in out, out)
        err, out = call("tabs_list")
        check("allowlist: off-list tab hidden from tabs_list", not err and "localhost" not in out and "hidden_by_site_rules" in out, out)
        err, out = call("navigate", tab_id=tab_id, action="back")
        check("allowlist: can't even go back from an off-list tab", err and "allowlist" in out, out)

    err, out = call("bridge_status")
    check("bridge_status", '"browser_connected": true' in out, out)

    # ---- per-session controls, clicked in the real popup with two live sessions ----
    port1 = json.loads(call("bridge_status")[1])["port"]
    bridge2 = start_bridge()
    rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "e2e2", "version": "0"}}, bridge2)
    port2 = json.loads(call("bridge_status", bridge2)[1])["port"]
    err, out = call("tabs_list", bridge2)
    check("second session connects", not err, out)

    mn = Marionette(MARIONETTE)
    mn.open_extension_page("claude-browser-bridge@jhueby", "popup.html")
    row = lambda port: f"//li[contains(@class,'session')][.//div[contains(., 'port {port} ')]]"
    time.sleep(1.5)
    shown = mn.text("#sessions")
    check("popup lists both sessions with port/pid/count", f"port {port1}" in shown and f"port {port2}" in shown
          and f"pid {bridge2.pid}" in shown and "cmds" in shown, shown)

    # (in --allowlist mode the main test tab has wandered off-list by now, so use a fresh allowed tab)
    etab = json.loads(call("tab_open", url=page, active=False)[1])["id"] if opts.allowlist else tab_id
    mn.click("//input[@id='allowEvaluate']")  # untick
    time.sleep(0.5)
    err, out = call("evaluate", tab_id=etab, code="1 + 1")
    check("popup: unticking Allow evaluate blocks evaluate", err and "turned off" in out, out)
    mn.click("//input[@id='allowEvaluate']")  # tick again
    time.sleep(0.5)
    err, out = call("evaluate", tab_id=etab, code="1 + 1")
    check("popup: ticking Allow evaluate re-enables it", not err and "2" in out, out)

    mn.click(row(port1) + "//button[.='Disconnect']")
    time.sleep(0.5)
    err, out = call("tabs_list")
    check("popup Disconnect: session 1 refused with a clear message", err and "disconnected this Claude Code session" in out, out)
    err, out = call("tabs_list", bridge2)
    check("popup Disconnect: session 2 unaffected", not err, out)
    time.sleep(1.2)
    check("popup shows DISCONNECTED", "DISCONNECTED" in mn.text("#sessions"), mn.text("#sessions"))

    mn.click(row(port1) + "//button[.='Reconnect']")
    time.sleep(0.5)
    err, out = call("tabs_list")
    check("popup Reconnect: session 1 works again", not err, out)

    mn.click(row(port2) + "//button[.='Kill']")
    time.sleep(0.3)
    err, out = call("tabs_list", bridge2)
    check("Kill needs a second click", not err, out)
    mn.click(row(port2) + "//button[.='Click again to kill']")
    try:
        bridge2.wait(timeout=5)
        killed = True
    except subprocess.TimeoutExpired:
        killed = False
        bridge2.kill()
    check("popup Kill: session 2 bridge process exited", killed, "still running")
    err, out = call("tabs_list")
    check("session 1 survives killing session 2", not err, out)

    audit = (home / "audit.log").read_text()
    check("audit log redacts typed text", "hello world" not in audit and "secret" not in audit and '"tool": "type"' in audit, audit[-500:])
finally:
    try:
        (mn or Marionette(MARIONETTE, tries=3)).cmd("Marionette:Quit", {"flags": ["eForceQuit"]})
    except Exception:
        pass
    bridge.stdin.close()
    bridge.terminate()
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(fx.pid)], capture_output=True)
    else:
        fx.terminate()
    httpd.shutdown()

print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED: ' + ', '.join(failures)}")
sys.exit(1 if failures else 0)
