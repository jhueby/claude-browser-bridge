"""Checks the loopback endpoint refuses everything except a paired extension.

    python test/test_security.py
"""
import http.client
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
home = Path(tempfile.mkdtemp(prefix="cbb-sec-"))
env = {**os.environ, "BRIDGE_HOME": str(home), "BRIDGE_PORT_START": "8977"}
proc = subprocess.Popen([sys.executable, str(ROOT / "server" / "bridge.py")], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
for _ in range(50):
    if (home / "token").exists():
        break
    time.sleep(0.1)
time.sleep(0.5)
token = (home / "token").read_text().strip()


def req(method, path, headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", 8977, timeout=30)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    return r.status


failures = []


def check(label, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + f"{label}: {got}" + ("" if ok else f" (want {want})"))
    if not ok:
        failures.append(label)


try:
    good = {"X-Bridge-Token": token}
    check("no token", req("GET", "/hello"), 401)
    check("wrong token", req("GET", "/hello", {"X-Bridge-Token": "x" * 43}), 401)
    check("web origin with valid token", req("GET", "/hello", {**good, "Origin": "https://evil.example"}), 403)
    check("null origin", req("GET", "/hello", {**good, "Origin": "null"}), 403)
    check("DNS-rebinding host", req("GET", "/hello", {**good, "Host": "evil.example:8977"}), 403)
    check("CORS preflight", req("OPTIONS", "/poll", {"Origin": "https://evil.example",
                                                     "Access-Control-Request-Method": "GET"}), 403)
    check("simple POST from a page (no token)", req("POST", "/result", {"Content-Type": "text/plain"},
                                                     json.dumps({"id": "1", "ok": True})), 401)
    check("extension origin + token", req("GET", "/hello", {**good, "Origin": "moz-extension://abc"}), 200)
    check("no origin + token", req("GET", "/hello", good), 200)
finally:
    proc.terminate()

print("ALL PASSED" if not failures else f"{len(failures)} FAILED")
sys.exit(1 if failures else 0)
