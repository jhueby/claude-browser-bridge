#!/usr/bin/env python3
"""Register (or remove) the native messaging host so the extension pairs itself.

    python native/install.py            # install for the current user
    python native/install.py --remove   # uninstall

Per-user only; no admin rights. On Windows it writes one registry key:
HKCU\\Software\\Mozilla\\NativeMessagingHosts\\claude_browser_bridge -> host manifest path.
"""

import json
import os
import sys
from pathlib import Path

NAME = "claude_browser_bridge"
EXT_ID = "claude-browser-bridge@jhueby"
HERE = Path(__file__).resolve().parent
STATE = Path.home() / ".claude-browser-bridge"
REG_KEY = rf"Software\Mozilla\NativeMessagingHosts\{NAME}"


def manifest_dir() -> Path:
    if os.name == "nt":
        return STATE / "native"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Mozilla/NativeMessagingHosts"
    return Path.home() / ".mozilla/native-messaging-hosts"


def install() -> None:
    mdir = manifest_dir()
    mdir.mkdir(parents=True, exist_ok=True)
    host = HERE / "host.py"
    if os.name == "nt":
        # Firefox needs an executable path; a .bat wrapper runs host.py with this same Python.
        launcher = mdir / "host.bat"
        launcher.write_text(f'@echo off\r\n"{sys.executable}" -u "{host}" %*\r\n', encoding="utf-8")
    else:
        launcher = mdir / "claude_browser_bridge_host"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" -u "{host}" "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)
    manifest = mdir / f"{NAME}.json"
    manifest.write_text(json.dumps({
        "name": NAME,
        "description": "Hands the Claude Browser Bridge pairing token to its Firefox extension",
        "path": str(launcher),
        "type": "stdio",
        "allowed_extensions": [EXT_ID],
    }, indent=2), encoding="utf-8")
    if os.name == "nt":
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_KEY) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, str(manifest))
        print(f"registered HKCU\\{REG_KEY}")
    print(f"host manifest: {manifest}\nlauncher:      {launcher}\nonly extension allowed: {EXT_ID}")
    print("Reload the extension (or restart Firefox); it will pair itself.")


def remove() -> None:
    mdir = manifest_dir()
    for f in (mdir / f"{NAME}.json", mdir / "host.bat", mdir / "claude_browser_bridge_host"):
        f.unlink(missing_ok=True)
    if os.name == "nt":
        import winreg
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REG_KEY)
            print(f"removed HKCU\\{REG_KEY}")
        except FileNotFoundError:
            pass
    print("native host removed; the extension falls back to a pasted token")


if __name__ == "__main__":
    remove() if "--remove" in sys.argv else install()
