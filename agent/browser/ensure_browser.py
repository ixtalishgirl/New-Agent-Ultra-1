"""Ensure a Playwright Chromium binary exists, downloading it at runtime if needed.

Build-time installs (e.g. Render's buildCommand) can silently fail or land in a
different HOME than the runtime process uses. This helper checks for the actual
browser binaries and, when missing, runs ``python -m playwright install``
itself — so browser tools self-heal instead of failing forever.

Tries the small headless-shell build first, then falls back to full Chromium.
Each missing-browser install is attempted at most once per process to avoid
repeated multi-minute hangs.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_tried: dict[str, bool] = {}


def browsers_path() -> Path:
    """Where Playwright keeps downloaded browsers."""
    p = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if p:
        return Path(p).expanduser()
    return Path.home() / ".cache" / "ms-playwright"


def has_browser() -> bool:
    """True when a usable Chromium / headless-shell binary is on disk."""
    base = browsers_path()
    if not base.is_dir():
        return False
    try:
        children = list(base.iterdir())
    except OSError:
        return False
    for child in children:
        name = child.name
        if name.startswith("chromium_headless_shell-"):
            # Playwright >=1.49: chrome-headless-shell-linux64/chrome-headless-shell
            # Older: chrome-linux/headless_shell
            if ((child / "chrome-linux" / "headless_shell").exists()
                    or (child / "chrome-headless-shell-linux64"
                        / "chrome-headless-shell").exists()):
                return True
        elif name.startswith("chromium-"):
            if ((child / "chrome-linux" / "chrome").exists()
                    or (child / "chrome-linux64" / "chrome").exists()):
                return True
    return False


def ensure_browser_installed(timeout: int = 420, force: bool = False) -> tuple[bool, str]:
    """Download Chromium when missing (or when force=True).

    Installs the FULL chromium build (not just headless-shell): the daemon
    launches it with --headless=new, which is far more reliable on Render
    than the headless-shell binary (which has gone stale/corrupt causing
    ETXTBSY spawn failures).

    Returns (ok, message). Never raises.
    """
    if not force and has_browser():
        return True, "browser already installed"
    if force:
        # Wipe potentially corrupt install so we get a clean download.
        import shutil
        try:
            shutil.rmtree(browsers_path(), ignore_errors=True)
        except Exception:
            pass
        # Reset the tried-guard so a fresh attempt is allowed.
        _tried.pop(str(browsers_path()), None)
    key = str(browsers_path())
    if _tried.get(key):
        return False, "browser install already attempted in this process and failed"
    _tried[key] = True
    last_err = ""
    # Full chromium FIRST (daemon uses --headless=new with it), then
    # headless-shell as a fallback.
    for extra in (["chromium"], ["--only-shell", "chromium"]):
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "playwright", "install", *extra],
                capture_output=True, text=True, timeout=timeout,
            )
            if has_browser():
                return True, "browser installed at runtime"
            last_err = (proc.stderr or proc.stdout or "no output")[-400:]
        except Exception as e:  # timeout, missing module, ...
            last_err = str(e)[-400:]
    return False, f"browser install failed: {last_err}"
