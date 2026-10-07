#!/usr/bin/env python3
"""Sign the extension as an UNLISTED (private) add-on with your own AMO account, so Firefox
installs it permanently. Nothing is published on addons.mozilla.org.

    python scripts/sign.py [--bump] [--install] [--listed]

  --bump     bump the patch version first (AMO rejects re-signing a version it has seen)
  --install  open the signed .xpi in Firefox afterwards (you click "Add" once)
  --listed   submit to the PUBLIC addons.mozilla.org listing instead (uses store/amo-metadata.json;
             goes to Mozilla's human review, so no .xpi comes back right away)

API keys (create once at https://addons.mozilla.org/developers/addon/api/key/) come from
AMO_JWT_ISSUER / AMO_JWT_SECRET, or from ~/.claude-browser-bridge/amo.json:
    {"issuer": "user:12345:67", "secret": "..."}
They are handed to web-ext via environment variables, never on the command line.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "extension" / "manifest.json"
DIST = ROOT / "dist"
KEYFILE = Path.home() / ".claude-browser-bridge" / "amo.json"
FIREFOX = Path(r"C:\Program Files\Mozilla Firefox\firefox.exe")


def keys():
    issuer, secret = os.environ.get("AMO_JWT_ISSUER"), os.environ.get("AMO_JWT_SECRET")
    if not (issuer and secret) and KEYFILE.exists():
        d = json.loads(KEYFILE.read_text(encoding="utf-8"))
        issuer, secret = d.get("issuer"), d.get("secret")
    if not (issuer and secret):
        sys.exit(f"No AMO API keys. Create them at https://addons.mozilla.org/developers/addon/api/key/ "
                 f"and put them in {KEYFILE} as {{\"issuer\": \"...\", \"secret\": \"...\"}} "
                 "or set AMO_JWT_ISSUER / AMO_JWT_SECRET.")
    return issuer, secret


def bump() -> str:
    text = MANIFEST.read_text(encoding="utf-8")
    m = re.search(r'"version":\s*"(\d+)\.(\d+)\.(\d+)"', text)
    new = f"{m[1]}.{m[2]}.{int(m[3]) + 1}"
    MANIFEST.write_text(text.replace(m[0], f'"version": "{new}"'), encoding="utf-8", newline="\n")
    print(f"version -> {new}")
    return new


def main() -> None:
    issuer, secret = keys()
    if "--bump" in sys.argv:
        bump()
    version = json.loads(MANIFEST.read_text(encoding="utf-8"))["version"]
    DIST.mkdir(exist_ok=True)
    env = {**os.environ, "WEB_EXT_API_KEY": issuer, "WEB_EXT_API_SECRET": secret}
    npx = "npx.cmd" if os.name == "nt" else "npx"
    listed = "--listed" in sys.argv
    cmd = [npx, "--yes", "web-ext", "sign", "--channel", "listed" if listed else "unlisted",
           "--source-dir", str(ROOT / "extension"), "--artifacts-dir", str(DIST),
           "--ignore-files", "pairing.json", ".amo-upload-uuid"]
    if listed:
        cmd += ["--amo-metadata", str(ROOT / "store" / "amo-metadata.json"), "--approval-timeout", "0"]
        print(f"submitting v{version} to the public listing — Mozilla reviews it by hand (hours to days)…")
    else:
        print(f"signing v{version} (unlisted) — AMO's automated review usually takes a minute or two…")
    r = subprocess.run(cmd, env=env)
    if r.returncode:
        sys.exit("web-ext sign failed (if AMO says the version exists, rerun with --bump)")
    if listed:
        print("Submitted. Track review at https://addons.mozilla.org/developers/addons — once approved, "
              "Firefox users can install it from the public listing.")
        return
    xpis = sorted(DIST.glob(f"*{version}*.xpi"), key=lambda p: p.stat().st_mtime)
    if not xpis:
        sys.exit(f"signed, but no .xpi for v{version} found in {DIST}")
    xpi = xpis[-1]
    print(f"\nsigned: {xpi}")
    if "--install" in sys.argv and FIREFOX.exists():
        subprocess.Popen([str(FIREFOX), str(xpi)])
        print("Firefox is asking to add it — click Add. Remove the temporary copy from about:debugging if loaded.")
    else:
        print("Install: drag that file into Firefox (or File > Open File), then click Add.")


if __name__ == "__main__":
    main()
