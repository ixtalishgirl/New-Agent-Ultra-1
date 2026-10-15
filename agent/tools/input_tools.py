"""Mouse & Keyboard tool module — real OS-level input control.

Primary backend: ``pyautogui`` (probed at call time, never imported at
module top-level, so a missing install never breaks import).
Fallback: Playwright mouse/keyboard via subprocess against the persistent
browser daemon (same CDP port file used by browser_tools); this fallback
only drives a browser page and is documented as such in each result.
If neither backend is available the tool returns ok=False with an install
hint ("pip install pyautogui"). Nothing here implements or weakens any of
the 5 hard denials (container_escape, boot_recovery, av_edr_disable,
billing_fraud, exfiltration).
"""
from __future__ import annotations

import json
import locale
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import pyperclip  # type: ignore
except Exception:
    pyperclip = None  # type: ignore

_INSTALL_HINT = ("no input backend available: pip install pyautogui "
                 "(and optionally pyperclip); "
                 "Playwright CDP daemon fallback also unavailable")


@dataclass
class InputResult:
    """Structured result of an input operation."""
    ok: bool
    output: str = ""
    parsed: dict = field(default_factory=dict)
    error: str = ""


# ---------------------------------------------------------------------------
# Backend probing (call time only)
# ---------------------------------------------------------------------------

def _pyautogui():
    try:
        import pyautogui  # type: ignore
        pyautogui.FAILSAFE = False
        return pyautogui
    except Exception:
        return None


def _daemon_port() -> Optional[int]:
    """Read the browser-daemon CDP port file (shared with browser_tools)."""
    try:
        p = Path("/tmp/browser_daemon/cdp.port")
        if not p.exists():
            return None
        port = int(p.read_text().strip())
        import socket as _s
        with _s.create_connection(("127.0.0.1", port), timeout=2):
            pass
        return port
    except Exception:
        return None


def _pw_available() -> bool:
    try:
        proc = subprocess.run(
            [sys.executable, "-c",
             "import importlib.util; print(bool(importlib.util.find_spec('playwright')))"],
            capture_output=True, text=True, timeout=15)
        return proc.stdout.strip() == "True"
    except Exception:
        return False


_PW_INPUT_SCRIPT = r'''
import json, sys
from playwright.sync_api import sync_playwright

def main():
    args = json.loads(sys.argv[1])
    op = args["op"]
    out = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(
                f"http://127.0.0.1:{args['port']}")
            ctx = browser.contexts[0]
            pages = ctx.pages or [ctx.new_page()]
            page = pages[0]
            mouse, kb = page.mouse, page.keyboard
            if op == "move":
                mouse.move(args["x"], args["y"]); out = {"moved": True}
            elif op == "click":
                mouse.click(args["x"], args["y"],
                            button=args.get("button", "left"))
                out = {"clicked": True}
            elif op == "dblclick":
                mouse.dblclick(args["x"], args["y"]); out = {"clicked": True}
            elif op == "drag":
                mouse.move(args["x1"], args["y1"]); mouse.down()
                steps = max(2, int(args.get("duration", 0.5) * 20))
                mouse.move(args["x2"], args["y2"], steps=steps)
                mouse.up(); out = {"dragged": True}
            elif op == "scroll":
                mouse.move(args.get("x", 0), args.get("y", 0))
                mouse.wheel(0, -args.get("clicks", 3) * 100)
                out = {"scrolled": True}
            elif op == "press":
                kb.press(args["key"]); out = {"pressed": True}
            elif op == "hotkey":
                kb.press("+".join(args["keys"])); out = {"pressed": True}
            elif op == "type":
                kb.type(args["text"]); out = {"typed": len(args["text"])}
            elif op == "down":
                kb.down(args["key"]); out = {"down": True}
            elif op == "up":
                kb.up(args["key"]); out = {"up": True}
            elif op == "tap":
                page.touchscreen.tap(args["x"], args["y"])
                out = {"tapped": True}
            else:
                out = {"error": f"unknown op {op}"}
            browser.close()
    except Exception as e:
        out = {"error": str(e)[:600]}
    print(json.dumps(out))

main()
'''


def _pw_input(op: str, params: dict, timeout: int = 60) -> Optional[dict]:
    """Playwright CDP fallback for input ops; None if unavailable."""
    port = _daemon_port()
    if port is None or not _pw_available():
        return None
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "pw_input.py"
        path.write_text(_PW_INPUT_SCRIPT)
        try:
            proc = subprocess.run(
                [sys.executable, str(path),
                 json.dumps({"op": op, "port": port, **params})],
                capture_output=True, text=True, timeout=timeout, cwd=tmpdir)
        except subprocess.TimeoutExpired:
            return {"error": "playwright input timed out"}
        if proc.returncode != 0:
            return {"error": (proc.stderr or "playwright failed")[:400]}
        try:
            return json.loads(proc.stdout.strip().split("\n")[-1])
        except Exception:
            return {"error": "bad playwright output"}


def _ok(action: str, parsed: dict) -> InputResult:
    return InputResult(ok=True, output=action, parsed=parsed)


# ---------------------------------------------------------------------------
# Mouse tools (9)
# ---------------------------------------------------------------------------

def mouse_move(args: dict) -> InputResult:
    try:
        x, y = int(args["x"]), int(args["y"])
        pg = _pyautogui()
        if pg is not None:
            pg.moveTo(x, y)
            return _ok(f"mouse moved to ({x}, {y})",
                       {"x": x, "y": y, "backend": "pyautogui"})
        res = _pw_input("move", {"x": x, "y": y})
        if res and not res.get("error"):
            return _ok(f"mouse moved to ({x}, {y}) [page-level fallback]",
                       {"x": x, "y": y, "backend": "playwright-cdp",
                        "note": "page-level only"})
        return InputResult(ok=False, error=_INSTALL_HINT)
    except Exception as e:
        return InputResult(ok=False, error=str(e)[:600])


def mouse_click(args: dict) -> InputResult:
    try:
        x, y = int(args["x"]), int(args["y"])
        button = str(args.get("button", "left")).lower()
        if button not in ("left", "right", "middle"):
            return InputResult(ok=False, error="button must be left|right|middle")
        pg = _pyautogui()
        if pg is not None:
            pg.click(x, y, button=button)
            return _ok(f"clicked {button} at ({x}, {y})",
                       {"x": x, "y": y, "button": button,
                        "backend": "pyautogui"})
        res = _pw_input("click", {"x": x, "y": y, "button": button})
        if res and not res.get("error"):
            return _ok(f"clicked {button} at ({x}, {y}) [page-level fallback]",
                       {"x": x, "y": y, "button": button,
                        "backend": "playwright-cdp",
                        "note": "page-level only"})
        return InputResult(ok=False, error=_INSTALL_HINT)
    except Exception as e:
        return InputResult(ok=False, error=str(e)[:600])


def mouse_double_click(args: dict) -> InputResult:
    try:
        x, y = int(args["x"]), int(args["y"])
        pg = _pyautogui()
        if pg is not None:
            pg.doubleClick(x, y)
            return _ok(f"double-clicked at ({x}, {y})",
                       {"x": x, "y": y, "backend": "pyautogui"})
        res = _pw_input("dblclick", {"x": x, "y": y})
        if res and not res.get("error"):
            return _ok(f"double-clicked at ({x}, {y}) [page-level fallback]",
                       {"x": x, "y": y, "backend": "playwright-cdp",
                        "note": "page-level only"})
        return InputResult(ok=False, error=_INSTALL_HINT)
    except Exception as e:
        return InputResult(ok=False, error=str(e)[:600])


def mouse_right_click(args: dict) -> InputResult:
    try:
        x, y = int(args["x"]), int(args["y"])
        pg = _pyautogui()
        if pg is not None:
            pg.rightClick(x, y)
            return _ok(f"right-clicked at ({x}, {y})",
                       {"x": x, "y": y, "backend": "pyautogui"})
        res = _pw_input("click", {"x": x, "y": y, "button": "right"})
        if res and not res.get("error"):
            return _ok(f"right-clicked at ({x}, {y}) [page-level fallback]",
                       {"x": x, "y": y, "backend": "playwright-cdp",
                        "note": "page-level only"})
        return InputResult(ok=False, error=_INSTALL_HINT)
    except Exception as e:
        return InputResult(ok=False, error=str(e)[:600])


def mouse_drag(args: dict) -> InputResult:
    try:
        x1, y1 = int(args["x1"]), int(args["y1"])
        x2, y2 = int(args["x2"]), int(args["y2"])
        dur = float(args.get("duration", 0.5))
        pg = _pyautogui()
        if pg is not None:
            pg.moveTo(x1, y1)
            pg.dragTo(x2, y2, duration=dur)
            return _ok(f"dragged ({x1},{y1})->({x2},{y2})",
                       {"x1": x1, "y1": y1, "x2": x2, "y2": y2,
                        "backend": "pyautogui"})
        res = _pw_input("drag", {"x1": x1, "y1": y1, "x2": x2, "y2": y2,
                                 "duration": dur})
        if res and not res.get("error"):
            return _ok(f"dragged ({x1},{y1})->({x2},{y2}) [page-level fallback]",
                       {"backend": "playwright-cdp", "note": "page-level only"})
        return InputResult(ok=False, error=_INSTALL_HINT)
    except Exception as e:
        return InputResult(ok=False, error=str(e)[:600])


def mouse_scroll(args: dict) -> InputResult:
    try:
        clicks = int(args.get("clicks", 3))
        x = args.get("x"); y = args.get("y")
        pg = _pyautogui()
        if pg is not None:
            if x is not None and y is not None:
                pg.moveTo(int(x), int(y))
            pg.scroll(clicks)
            return _ok(f"scrolled {clicks} click(s)",
                       {"clicks": clicks, "backend": "pyautogui"})
        res = _pw_input("scroll", {"clicks": clicks,
                                   "x": int(x or 0), "y": int(y or 0)})
        if res and not res.get("error"):
            return _ok(f"scrolled {clicks} click(s) [page-level fallback]",
                       {"clicks": clicks, "backend": "playwright-cdp",
                        "note": "page-level only"})
        return InputResult(ok=False, error=_INSTALL_HINT)
    except Exception as e:
        return InputResult(ok=False, error=str(e)[:600])


def mouse_hover(args: dict) -> InputResult:
    try:
        x, y = int(args["x"]), int(args["y"])
        pg = _pyautogui()
        if pg is not None:
            pg.moveTo(x, y)
            return _ok(f"hovered at ({x}, {y})",
                       {"x": x, "y": y, "backend": "pyautogui"})
        res = _pw_input("move", {"x": x, "y": y})
        if res and not res.get("error"):
            return _ok(f"hovered at ({x}, {y}) [page-level fallback]",
                       {"x": x, "y": y, "backend": "playwright-cdp",
                        "note": "page-level only"})
        return InputResult(ok=False, error=_INSTALL_HINT)
    except Exception as e:
        return InputResult(ok=False, error=str(e)[:600])


def mouse_hold_release(args: dict) -> InputResult:
    try:
        x, y = int(args["x"]), int(args["y"])
        hold = float(args.get("hold_sec", 1.0))
        if hold < 0 or hold > 60:
            return InputResult(ok=False, error="hold_sec must be in [0, 60]")
        pg = _pyautogui()
        if pg is not None:
            pg.moveTo(x, y)
            pg.mouseDown()
            time.sleep(hold)
            pg.mouseUp()
            return _ok(f"held left button {hold}s at ({x}, {y})",
                       {"x": x, "y": y, "hold_sec": hold,
                        "backend": "pyautogui"})
        return InputResult(ok=False, error=
            "mouse_hold_release needs pyautogui (page fallback cannot hold "
            "the OS button): pip install pyautogui")
    except Exception as e:
        return InputResult(ok=False, error=str(e)[:600])


_TOUCH_SCRIPT = r'''
import json, sys
from playwright.sync_api import sync_playwright

def main():
    args = json.loads(sys.argv[1])
    gesture = args["gesture"]
    out = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(
                f"http://127.0.0.1:{args['port']}")
            ctx = browser.contexts[0]
            page = (ctx.pages or [ctx.new_page()])[0]
            cdp = ctx.new_cdp_session(page)
            if gesture == "tap":
                page.touchscreen.tap(args["x"], args["y"])
                out = {"gesture": "tap"}
            elif gesture == "swipe":
                x1, y1 = args["x1"], args["y1"]
                x2, y2 = args["x2"], args["y2"]
                cdp.send("Input.dispatchTouchEvent",
                         {"type": "touchStart",
                          "touchPoints": [{"x": x1, "y": y1, "id": 1}]})
                for i in range(1, 11):
                    cdp.send("Input.dispatchTouchEvent",
                             {"type": "touchMove",
                              "touchPoints": [{"x": x1 + (x2 - x1) * i / 10,
                                               "y": y1 + (y2 - y1) * i / 10,
                                               "id": 1}]})
                cdp.send("Input.dispatchTouchEvent",
                         {"type": "touchEnd", "touchPoints": []})
                out = {"gesture": "swipe"}
            else:
                out = {"error": f"gesture '{gesture}' unsupported on desktop "
                                "Chromium (tap and swipe only)"}
            browser.close()
    except Exception as e:
        out = {"error": str(e)[:600]}
    print(json.dumps(out))

main()
'''


def touch_gesture(args: dict) -> InputResult:
    """tap|swipe via CDP touch events; pinch is unsupported on desktop."""
    try:
        gesture = str(args.get("gesture", "tap")).lower()
        if gesture not in ("tap", "swipe", "pinch"):
            return InputResult(ok=False,
                               error="gesture must be tap|swipe|pinch")
        if gesture == "pinch":
            return InputResult(ok=False, error=
                "pinch requires a multi-touch source; unsupported on desktop "
                "Chromium")
        port = _daemon_port()
        if port is None or not _pw_available():
            return InputResult(ok=False, error=
                "touch_gesture needs the browser daemon running "
                "(start any browser_* tool first) with playwright installed")
        params = {"gesture": gesture, "port": port,
                  "x": int(args.get("x", 0)), "y": int(args.get("y", 0))}
        if gesture == "swipe":
            for k in ("x1", "y1", "x2", "y2"):
                if k not in args:
                    return InputResult(ok=False, error=
                        f"swipe needs {k}")
                params[k] = int(args[k])
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "pw_touch.py"
            path.write_text(_TOUCH_SCRIPT)
            proc = subprocess.run(
                [sys.executable, str(path), json.dumps(params)],
                capture_output=True, text=True, timeout=60, cwd=tmpdir)
            if proc.returncode != 0:
                return InputResult(ok=False,
                                   error=(proc.stderr or "failed")[:400])
            res = json.loads(proc.stdout.strip().split("\n")[-1])
        if res.get("error"):
            return InputResult(ok=False, error=res["error"])
        return _ok(f"touch {gesture} performed",
                   {"gesture": gesture, "backend": "playwright-cdp"})
    except Exception as e:
        return InputResult(ok=False, error=str(e)[:600])


# ---------------------------------------------------------------------------
# Keyboard tools (13)
# ---------------------------------------------------------------------------

def key_press(args: dict) -> InputResult:
    try:
        key = str(args["key"])
        pg = _pyautogui()
        if pg is not None:
            pg.press(key)
            return _ok(f"pressed '{key}'", {"key": key, "backend": "pyautogui"})
        res = _pw_input("press", {"key": key})
        if res and not res.get("error"):
            return _ok(f"pressed '{key}' [page-level fallback]",
                       {"key": key, "backend": "playwright-cdp",
                        "note": "page-level only"})
        return InputResult(ok=False, error=_INSTALL_HINT)
    except Exception as e:
        return InputResult(ok=False, error=str(e)[:600])


def key_hotkey(args: dict) -> InputResult:
    try:
        keys = args.get("keys", [])
        if not keys or not isinstance(keys, list):
            return InputResult(ok=False, error="keys must be a non-empty list")
        keys = [str(k) for k in keys]
        pg = _pyautogui()
        if pg is not None:
            pg.hotkey(*keys)
            return _ok(f"hotkey {'+'.join(keys)}",
                       {"keys": keys, "backend": "pyautogui"})
        res = _pw_input("hotkey", {"keys": keys})
        if res and not res.get("error"):
            return _ok(f"hotkey {'+'.join(keys)} [page-level fallback]",
                       {"keys": keys, "backend": "playwright-cdp",
                        "note": "page-level only"})
        return InputResult(ok=False, error=_INSTALL_HINT)
    except Exception as e:
        return InputResult(ok=False, error=str(e)[:600])


def key_type(args: dict) -> InputResult:
    try:
        text = str(args.get("text", ""))
        interval = float(args.get("interval", 0.0))
        pg = _pyautogui()
        if pg is not None:
            pg.typewrite(text, interval=interval)
            return _ok(f"typed {len(text)} char(s)",
                       {"chars": len(text), "backend": "pyautogui"})
        # Playwright keyboard.type is unicode-safe — preferred fallback
        res = _pw_input("type", {"text": text})
        if res and not res.get("error"):
            return _ok(f"typed {len(text)} char(s) [page-level fallback]",
                       {"chars": len(text), "backend": "playwright-cdp",
                        "note": "page-level only"})
        return InputResult(ok=False, error=_INSTALL_HINT)
    except Exception as e:
        return InputResult(ok=False, error=str(e)[:600])


_SPECIAL_KEYS = {"enter", "tab", "esc", "escape", "space", "backspace",
                 "delete", "up", "down", "left", "right", "home", "end",
                 "pageup", "pagedown", "insert", "f1", "f2", "f3", "f4",
                 "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12",
                 "shift", "ctrl", "alt", "capslock", "numlock", "printscreen"}


def key_special(args: dict) -> InputResult:
    try:
        key = str(args.get("key", "")).lower()
        if key not in _SPECIAL_KEYS:
            return InputResult(ok=False, error=
                f"unknown special key '{key}'; allowed: {sorted(_SPECIAL_KEYS)}")
        return key_press({"key": key})
    except Exception as e:
        return InputResult(ok=False, error=str(e)[:600])


def key_combo(args: dict) -> InputResult:
    """Combo like 'ctrl+shift+t' -> hotkey(['ctrl','shift','t'])."""
    try:
        combo = str(args.get("combo", ""))
        parts = [p.strip() for p in combo.split("+") if p.strip()]
        if not parts:
            return InputResult(ok=False, error="combo must look like 'ctrl+shift+t'")
        return key_hotkey({"keys": parts})
    except Exception as e:
        return InputResult(ok=False, error=str(e)[:600])


def text_select_all(args: dict) -> InputResult:
    try:
        return key_hotkey({"keys": ["ctrl", "a"]})
    except Exception as e:
        return InputResult(ok=False, error=str(e)[:600])


def _clipboard_set(text: str) -> tuple[bool, str]:
    if pyperclip is not None:
        try:
            pyperclip.copy(text)
            return True, "pyperclip"
        except Exception:
            pass
    if shutil.which("xclip"):
        try:
            subprocess.run(["xclip", "-selection", "clipboard"], input=text,
                           capture_output=True, text=True, timeout=10,
                           check=True)
            return True, "xclip"
        except Exception:
            pass
    if shutil.which("xsel"):
        try:
            subprocess.run(["xsel", "--clipboard", "--input"], input=text,
                           capture_output=True, text=True, timeout=10,
                           check=True)
            return True, "xsel"
        except Exception:
            pass
    return False, ""


def _clipboard_get() -> tuple[Optional[str], str]:
    if pyperclip is not None:
        try:
            return pyperclip.paste(), "pyperclip"
        except Exception:
            pass
    if shutil.which("xclip"):
        try:
            proc = subprocess.run(["xclip", "-selection", "clipboard",
                                   "-o"], capture_output=True, text=True,
                                  timeout=10)
            if proc.returncode == 0:
                return proc.stdout, "xclip"
        except Exception:
            pass
    return None, ""


def clipboard_copy(args: dict) -> InputResult:
    try:
        text = str(args.get("text", ""))
        ok, backend = _clipboard_set(text)
        if not ok:
            return InputResult(ok=False, error=
                "no clipboard backend (install pyperclip, or xclip/xsel)")
        return _ok(f"copied {len(text)} char(s) to clipboard",
                   {"chars": len(text), "backend": backend})
    except Exception as e:
        return InputResult(ok=False, error=str(e)[:600])


def clipboard_paste(args: dict) -> InputResult:
    """Paste clipboard content (reads it back and types ctrl+v)."""
    try:
        text, backend = _clipboard_get()
        if text is None:
            return InputResult(ok=False, error=
                "clipboard unreadable (install pyperclip, or xclip/xsel)")
        pg = _pyautogui()
        if pg is not None:
            pg.hotkey("ctrl", "v")
            return _ok(f"pasted {len(text)} char(s) from clipboard",
                       {"chars": len(text), "preview": text[:100],
                        "backend": "pyautogui"})
        res = _pw_input("hotkey", {"keys": ["Control", "v"]})
        if res and not res.get("error"):
            return _ok(f"pasted {len(text)} char(s) [page-level fallback]",
                       {"chars": len(text), "preview": text[:100],
                        "backend": "playwright-cdp",
                        "note": "page-level only"})
        return _ok(f"clipboard holds {len(text)} char(s); no paste backend",
                   {"chars": len(text), "preview": text[:100],
                    "backend": backend, "pasted": False})
    except Exception as e:
        return InputResult(ok=False, error=str(e)[:600])


def input_method_switch(args: dict) -> InputResult:
    """Best-effort keyboard layout switch via setxkbmap."""
    try:
        layout = str(args.get("layout", ""))
        if not layout:
            return InputResult(ok=False,
                               error="layout is required, e.g. 'us', 'de'")
        exe = shutil.which("setxkbmap")
        if not exe:
            return InputResult(ok=False, error=
                "setxkbmap not available on this host")
        proc = subprocess.run([exe, layout], capture_output=True, text=True,
                              timeout=10)
        if proc.returncode != 0:
            return InputResult(ok=False,
                               error=proc.stderr.strip()[:400] or
                               "setxkbmap failed")
        return _ok(f"keyboard layout set to {layout}",
                   {"layout": layout, "backend": "setxkbmap"})
    except Exception as e:
        return InputResult(ok=False, error=str(e)[:600])


def key_hold(args: dict) -> InputResult:
    try:
        key = str(args["key"])
        seconds = float(args.get("seconds", 1.0))
        if seconds < 0 or seconds > 60:
            return InputResult(ok=False, error="seconds must be in [0, 60]")
        pg = _pyautogui()
        if pg is not None:
            pg.keyDown(key)
            time.sleep(seconds)
            pg.keyUp(key)
            return _ok(f"held '{key}' for {seconds}s",
                       {"key": key, "seconds": seconds,
                        "backend": "pyautogui"})
        res = _pw_input("down", {"key": key})
        if res and not res.get("error"):
            time.sleep(seconds)
            _pw_input("up", {"key": key})
            return _ok(f"held '{key}' for {seconds}s [page-level fallback]",
                       {"key": key, "seconds": seconds,
                        "backend": "playwright-cdp",
                        "note": "page-level only"})
        return InputResult(ok=False, error=_INSTALL_HINT)
    except Exception as e:
        return InputResult(ok=False, error=str(e)[:600])


def unicode_type(args: dict) -> InputResult:
    """Unicode-safe typing; prefers the Playwright keyboard.type path."""
    try:
        text = str(args.get("text", ""))
        # Playwright keyboard.type handles full unicode; try it first
        res = _pw_input("type", {"text": text})
        if res and not res.get("error"):
            return _ok(f"typed {len(text)} unicode char(s) via Playwright",
                       {"chars": len(text), "backend": "playwright-cdp"})
        pg = _pyautogui()
        if pg is not None:
            pg.typewrite(text)
            return _ok(f"typed {len(text)} unicode char(s)",
                       {"chars": len(text), "backend": "pyautogui"})
        return InputResult(ok=False, error=_INSTALL_HINT)
    except Exception as e:
        return InputResult(ok=False, error=str(e)[:600])


def layout_detect(args: dict) -> InputResult:
    """Detect current keyboard layout/locale (best-effort)."""
    try:
        info: dict = {}
        exe = shutil.which("setxkbmap")
        if exe:
            try:
                proc = subprocess.run([exe, "-query"], capture_output=True,
                                      text=True, timeout=10)
                for line in proc.stdout.splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1)
                        info[k.strip()] = v.strip()
            except Exception:
                pass
        info["locale"] = locale.getdefaultlocale()[0] or ""
        info["LANG"] = os.environ.get("LANG", "")
        info["XKB_DEFAULT_LAYOUT"] = os.environ.get("XKB_DEFAULT_LAYOUT", "")
        layout = info.get("layout") or info.get("XKB_DEFAULT_LAYOUT") or ""
        return _ok(f"layout={layout or 'unknown'}",
                   {"layout": layout, "details": info})
    except Exception as e:
        return InputResult(ok=False, error=str(e)[:600])


def key_macro(args: dict) -> InputResult:
    """Play back a sequence of [{key, delay_ms}]."""
    try:
        keys = args.get("keys", [])
        if not isinstance(keys, list) or not keys:
            return InputResult(ok=False,
                               error="keys must be a non-empty list of {key, delay_ms}")
        pg = _pyautogui()
        played = 0
        if pg is not None:
            for item in keys:
                k = str(item.get("key", ""))
                delay = float(item.get("delay_ms", 100)) / 1000.0
                pg.press(k)
                played += 1
                if delay > 0:
                    time.sleep(delay)
            return _ok(f"macro played {played} key(s)",
                       {"played": played, "backend": "pyautogui"})
        res = _pw_input("press", {"key": str(keys[0].get("key", ""))})
        if res and not res.get("error"):
            for item in keys:
                _pw_input("press", {"key": str(item.get("key", ""))})
                d = float(item.get("delay_ms", 100)) / 1000.0
                if d > 0:
                    time.sleep(d)
                played += 1
            return _ok(f"macro played {played} key(s) [page-level fallback]",
                       {"played": played, "backend": "playwright-cdp",
                        "note": "page-level only"})
        return InputResult(ok=False, error=_INSTALL_HINT)
    except Exception as e:
        return InputResult(ok=False, error=str(e)[:600])


# ---------------------------------------------------------------------------
# Module contract
# ---------------------------------------------------------------------------

def _S(arg_type: str, required: bool, desc: str) -> dict:
    return {"type": arg_type, "required": required, "description": desc}


_XY = lambda: {"x": _S("integer", True, "Screen X"),
               "y": _S("integer", True, "Screen Y")}

TOOL_DEFS: list[tuple] = [
    # -- mouse (9) --
    ("mouse_move", "Move the mouse cursor to (x, y)",
     dict(_XY()), "computer", 30),
    ("mouse_click", "Click at (x, y)",
     {**_XY(), "button": _S("string", False, "left|right|middle (default left)")},
     "computer", 30),
    ("mouse_double_click", "Double-click at (x, y)",
     dict(_XY()), "computer", 30),
    ("mouse_right_click", "Right-click at (x, y)",
     dict(_XY()), "computer", 30),
    ("mouse_drag", "Drag from (x1,y1) to (x2,y2)",
     {"x1": _S("integer", True, "Start X"), "y1": _S("integer", True, "Start Y"),
      "x2": _S("integer", True, "End X"), "y2": _S("integer", True, "End Y"),
      "duration": _S("number", False, "Seconds (default 0.5)")},
     "computer", 30),
    ("mouse_scroll", "Scroll the wheel N clicks (positive = up)",
     {"clicks": _S("integer", False, "Click count (default 3)"),
      "x": _S("integer", False, "Optional X to scroll at"),
      "y": _S("integer", False, "Optional Y to scroll at")},
     "computer", 30),
    ("mouse_hover", "Move the cursor to (x, y) without clicking",
     dict(_XY()), "computer", 30),
    ("mouse_hold_release", "Press and hold left button at (x,y) for hold_sec",
     {**_XY(), "hold_sec": _S("number", False, "Hold seconds [0,60] (default 1)")},
     "computer", 90),
    ("touch_gesture", "Touch gesture via CDP: tap|swipe (pinch unsupported on desktop)",
     {"gesture": _S("string", True, "tap|swipe|pinch"),
      "x": _S("integer", False, "Tap X"), "y": _S("integer", False, "Tap Y"),
      "x1": _S("integer", False, "Swipe start X"),
      "y1": _S("integer", False, "Swipe start Y"),
      "x2": _S("integer", False, "Swipe end X"),
      "y2": _S("integer", False, "Swipe end Y")},
     "computer", 60),
    # -- keyboard (13) --
    ("key_press", "Press a single key",
     {"key": _S("string", True, "Key name, e.g. 'a', 'enter', 'f5'")},
     "computer", 30),
    ("key_hotkey", "Press keys together",
     {"keys": _S("array", True, "Key list, e.g. ['ctrl','c']")},
     "computer", 30),
    ("key_type", "Type text (unicode-safe)",
     {"text": _S("string", True, "Text to type"),
      "interval": _S("number", False, "Delay between keystrokes (default 0)")},
     "computer", 60),
    ("key_special", "Press a special key",
     {"key": _S("string", True, "enter|tab|esc|space|backspace|delete|arrows|home|end|pageup|pagedown|insert|f1..f12|shift|ctrl|alt|...")},
     "computer", 30),
    ("key_combo", "Press a combo string like 'ctrl+shift+t'",
     {"combo": _S("string", True, "Plus-separated combo")},
     "computer", 30),
    ("text_select_all", "Select all (ctrl+a)",
     {}, "computer", 30),
    ("clipboard_copy", "Copy text to the OS clipboard",
     {"text": _S("string", True, "Text to copy")},
     "computer", 30),
    ("clipboard_paste", "Paste clipboard content via ctrl+v",
     {}, "computer", 30),
    ("input_method_switch", "Switch keyboard layout via setxkbmap (best-effort)",
     {"layout": _S("string", True, "Layout code, e.g. 'us', 'de'")},
     "computer", 30),
    ("key_hold", "Hold a key down for N seconds",
     {"key": _S("string", True, "Key name"),
      "seconds": _S("number", False, "Hold seconds [0,60] (default 1)")},
     "computer", 90),
    ("unicode_type", "Unicode-safe typing (prefers Playwright keyboard.type)",
     {"text": _S("string", True, "Text to type")},
     "computer", 60),
    ("layout_detect", "Detect current keyboard layout/locale (best-effort)",
     {}, "computer", 30),
    ("key_macro", "Play back a key sequence",
     {"keys": _S("array", True, "List of {key, delay_ms}")},
     "computer", 120),
]

HANDLERS: dict[str, Any] = {
    "mouse_move": mouse_move,
    "mouse_click": mouse_click,
    "mouse_double_click": mouse_double_click,
    "mouse_right_click": mouse_right_click,
    "mouse_drag": mouse_drag,
    "mouse_scroll": mouse_scroll,
    "mouse_hover": mouse_hover,
    "mouse_hold_release": mouse_hold_release,
    "touch_gesture": touch_gesture,
    "key_press": key_press,
    "key_hotkey": key_hotkey,
    "key_type": key_type,
    "key_special": key_special,
    "key_combo": key_combo,
    "text_select_all": text_select_all,
    "clipboard_copy": clipboard_copy,
    "clipboard_paste": clipboard_paste,
    "input_method_switch": input_method_switch,
    "key_hold": key_hold,
    "unicode_type": unicode_type,
    "layout_detect": layout_detect,
    "key_macro": key_macro,
}

__all__ = ["InputResult", "TOOL_DEFS", "HANDLERS"]
