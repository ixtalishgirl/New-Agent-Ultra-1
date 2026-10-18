"""Tor manager — self-healing Tor for onion browsing.

Ensures a Tor SOCKS proxy on 127.0.0.1:9050:
- Downloads the Debian tor .deb if the binary is missing (survives Render restarts).
- Extracts it under ~/tor and launches tor in the background.
- Safe to call repeatedly; no-op if Tor is already running.

Used by server startup (background thread) and the browser tools.
"""
from __future__ import annotations

import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

TOR_DIR = Path.home() / "tor"
TOR_DEB = TOR_DIR / "tor_0.4.9.11-0+deb12u1_amd64.deb"
TOR_BIN = TOR_DIR / "root" / "usr" / "bin" / "tor"
TOR_URL = "http://ftp.debian.org/debian/pool/main/t/tor/tor_0.4.9.11-0+deb12u1_amd64.deb"
SOCKS_PORT = 9050


def _port_open(port: int) -> bool:
    s = socket.socket()
    s.settimeout(1)
    try:
        s.connect(("127.0.0.1", port))
        s.close()
        return True
    except OSError:
        return False


def tor_running() -> bool:
    return _port_open(SOCKS_PORT)


def _ensure_binary() -> bool:
    if TOR_BIN.exists():
        return True
    try:
        TOR_DIR.mkdir(parents=True, exist_ok=True)
        if not TOR_DEB.exists():
            req = urllib.request.Request(TOR_URL, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=120) as r, open(TOR_DEB, "wb") as f:
                f.write(r.read())
        # Extract without dpkg (works unprivileged).
        import shutil
        tmp = TOR_DIR / "_x"
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        subprocess.run(["ar", "x", str(TOR_DEB)], cwd=str(tmp),
                       capture_output=True, timeout=60)
        data_tar = next(tmp.glob("data.tar*"), None)
        if not data_tar:
            return False
        root = TOR_DIR / "root"
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        subprocess.run(["tar", "-xf", str(data_tar), "-C", str(root)],
                       capture_output=True, timeout=60)
        shutil.rmtree(tmp, ignore_errors=True)
        return TOR_BIN.exists()
    except Exception:
        return False


def ensure_tor(timeout: int = 90) -> tuple[bool, str]:
    """Make sure Tor SOCKS proxy is up. Returns (ok, message)."""
    if tor_running():
        return True, "tor already running on 127.0.0.1:9050"
    if not _ensure_binary():
        return False, "tor binary unavailable"
    try:
        # Launch tor detached.
        subprocess.Popen(
            [str(TOR_BIN), "--SocksPort", str(SOCKS_PORT),
             "--DataDirectory", str(TOR_DIR / "data")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, cwd=str(TOR_DIR))
        deadline = time.time() + timeout
        while time.time() < deadline:
            if tor_running():
                return True, "tor started on 127.0.0.1:9050"
            time.sleep(2)
        return False, "tor did not come up in time"
    except Exception as e:
        return False, f"tor start failed: {e}"[:200]


def tor_status() -> dict:
    return {"running": tor_running(), "socks": f"127.0.0.1:{SOCKS_PORT}",
            "binary": str(TOR_BIN), "binary_present": TOR_BIN.exists()}
