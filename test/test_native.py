"""Native messaging host protocol check (no Firefox needed).

    python test/test_native.py
"""
import json
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
home = Path(tempfile.mkdtemp(prefix="cbb-nat-"))
env = {**os.environ, "BRIDGE_HOME": str(home)}


def frame(obj):
    data = json.dumps(obj).encode()
    return struct.pack("=I", len(data)) + data


def unframe(buf):
    out = []
    while buf:
        (n,) = struct.unpack("=I", buf[:4])
        out.append(json.loads(buf[4:4 + n]))
        buf = buf[4 + n:]
    return out


r = subprocess.run([sys.executable, str(ROOT / "native" / "host.py")], env=env, capture_output=True, timeout=10,
                   input=frame({"type": "token"}) + frame({"type": "nope"}) + frame({"type": "token"}))
msgs = unframe(r.stdout)
tok = (home / "token").read_text().strip()
checks = [
    ("creates token file and returns it", msgs[0].get("token") == tok and len(tok) >= 32),
    ("unknown message type -> error, host keeps running", "error" in msgs[1]),
    ("stable token across requests", msgs[2].get("token") == tok),
    ("exits cleanly when stdin closes", r.returncode == 0),
]
fails = 0
for label, ok in checks:
    print(("PASS " if ok else "FAIL ") + label)
    fails += not ok
print("ALL PASSED" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
