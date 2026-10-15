"""Screen & Vision tool module — real screen capture, UI inspection and image analysis.

Backends are probed at call time (never at import):
  1. ``mss`` (real display capture) if installed,
  2. native CLI tools (``scrot``/``import``/``xwd``) when a DISPLAY exists,
  3. Playwright (subprocess-isolated, headless Chromium) as a fallback for
     page-level capture, region capture and accessibility snapshots.

On a headless server without a display, full-screen OS capture cannot show a
real desktop; the Playwright path renders a real page screenshot instead and
says so in the output. No tool here implements or weakens any of the 5 hard
denials (container_escape, boot_recovery, av_edr_disable, billing_fraud,
exfiltration): captures stay on the local host, nothing is uploaded anywhere.
"""
from __future__ import annotations

import base64
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Optional

try:
    from PIL import Image, ImageDraw, ImageChops  # type: ignore
except Exception:
    Image = None  # type: ignore
    ImageDraw = None  # type: ignore
    ImageChops = None  # type: ignore

try:
    import mss  # type: ignore
except Exception:
    mss = None  # type: ignore

_STREAMS: dict[int, dict] = {}
_STREAMS_LOCK = threading.Lock()


@dataclass
class VisionResult:
    """Structured result of a vision operation."""
    ok: bool
    output: str = ""
    parsed: dict = field(default_factory=dict)
    error: str = ""


# ---------------------------------------------------------------------------
# Internal helpers (not tools)
# ---------------------------------------------------------------------------

_PW_CHECK = "import importlib.util; print(bool(importlib.util.find_spec('playwright')))"


def _playwright_available() -> bool:
    try:
        proc = subprocess.run([sys.executable, "-c", _PW_CHECK],
                              capture_output=True, text=True, timeout=15)
        return proc.stdout.strip() == "True"
    except Exception:
        return False


def _run_playwright_script(script: str, args: dict, timeout: int = 60) -> dict:
    """Run a Playwright script in a subprocess; return parsed JSON."""
    if not _playwright_available():
        return {"error": "playwright not installed (pip install playwright && "
                         "playwright install chromium)"}
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "pw_vision.py"
        path.write_text(script)
        try:
            proc = subprocess.run(
                [sys.executable, str(path), json.dumps(args)],
                capture_output=True, text=True, timeout=timeout, cwd=tmpdir)
        except subprocess.TimeoutExpired:
            return {"error": f"playwright script timed out after {timeout}s"}
        if proc.returncode != 0:
            return {"error": (proc.stderr or "playwright failed")[:600]}
        try:
            return json.loads(proc.stdout.strip().split("\n")[-1])
        except Exception:
            return {"error": "bad playwright output",
                    "raw": proc.stdout[:400]}


def _shot_path(prefix: str = "screen") -> str:
    ts = time.strftime("%Y%m%d-%H%M%S")
    return f"/tmp/{prefix}_{ts}_{os.getpid()}.png"


def _data_uri(path: str) -> str:
    with open(path, "rb") as f:
        raw = f.read()
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def _capture_mss(path: str, region: Optional[dict] = None) -> bool:
    if mss is None:
        return False
    try:
        with mss.mss() as sct:
            mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            if region:
                mon = {"left": int(region["x"]), "top": int(region["y"]),
                       "width": int(region["width"]),
                       "height": int(region["height"])}
            sct.shot(mon=mon, output=path)
        return Path(path).exists()
    except Exception:
        return False


def _capture_cli(path: str) -> tuple[bool, str]:
    if not os.environ.get("DISPLAY"):
        return False, "no DISPLAY"
    for exe in ("scrot", "import"):
        if shutil.which(exe):
            try:
                cmd = [exe, path] if exe == "scrot" else [exe, "-window", "root", path]
                subprocess.run(cmd, capture_output=True, timeout=20,
                               check=True)
                if Path(path).exists():
                    return True, exe
            except Exception:
                continue
    return False, "no capture CLI available"


_PW_SHOT_SCRIPT = r'''
import json, sys
from playwright.sync_api import sync_playwright

def main():
    args = json.loads(sys.argv[1])
    mode = args.get("mode", "page")
    url = args.get("url", "about:blank")
    path = args["path"]
    out = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            ctx = browser.new_context(viewport={"width": args.get("width", 1280),
                                                "height": args.get("height", 800)})
            page = ctx.new_page()
            if mode == "page":
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
                kw = {"path": path, "full_page": bool(args.get("full_page", False))}
                if args.get("clip"):
                    kw["clip"] = args["clip"]
                page.screenshot(**kw)
            elif mode == "a11y":
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
                out["snapshot"] = page.accessibility.snapshot()
            browser.close()
        out["path"] = path
    except Exception as e:
        out = {"error": str(e)[:800]}
    print(json.dumps(out))

main()
'''


def _capture_playwright_page(path: str, url: str = "about:blank",
                             clip: Optional[dict] = None) -> tuple[bool, str]:
    res = _run_playwright_script(_PW_SHOT_SCRIPT,
                                {"mode": "page", "url": url, "path": path,
                                 "clip": clip}, timeout=90)
    if res.get("error"):
        return False, res["error"]
    return Path(path).exists(), "playwright"


def _playwright_a11y(url: str) -> dict:
    return _run_playwright_script(_PW_SHOT_SCRIPT,
                                  {"mode": "a11y", "url": url}, timeout=90)


def _flatten_snapshot(node: Any, out: list) -> None:
    if not isinstance(node, dict):
        return
    children = node.get("children") or []
    entry = {"role": node.get("role", ""), "name": node.get("name", "") or ""}
    if node.get("role"):
        out.append(entry)
    for child in children:
        _flatten_snapshot(child, out)


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def screen_shot(args: dict) -> VisionResult:
    """Capture the full screen to a PNG; return path + base64 data URI."""
    try:
        path = args.get("output") or _shot_path("screen")
        backend = ""
        if _capture_mss(path):
            backend = "mss"
        else:
            ok, which = _capture_cli(path)
            if ok:
                backend = which
            else:
                ok, which = _capture_playwright_page(path)
                if ok:
                    backend = "playwright-headless-page"
                else:
                    return VisionResult(ok=False, error=
                        f"no screen-capture backend available: mss missing, "
                        f"CLI failed ({which})")
        uri = _data_uri(path)
        size = Path(path).stat().st_size
        note = ("headless page render (no real display on this host)"
                if backend == "playwright-headless-page"
                else "real display capture")
        return VisionResult(
            ok=True,
            output=f"captured {size} bytes via {backend} ({note}) -> {path}\n"
                   f"data_uri[{len(uri)} chars]: {uri[:120]}...",
            parsed={"path": path, "backend": backend, "bytes": size,
                    "data_uri": uri})
    except Exception as e:
        return VisionResult(ok=False, error=str(e)[:600])


def screen_region_capture(args: dict) -> VisionResult:
    """Capture a screen region {x, y, width, height} to PNG."""
    try:
        x = int(args.get("x", 0)); y = int(args.get("y", 0))
        w = int(args.get("width", 0)); h = int(args.get("height", 0))
        if w <= 0 or h <= 0:
            return VisionResult(ok=False, error="width and height must be > 0")
        region = {"x": x, "y": y, "width": w, "height": h}
        path = args.get("output") or _shot_path("region")
        backend = ""
        if _capture_mss(path, region):
            backend = "mss"
        else:
            ok, which = _capture_playwright_page(
                path, clip={"x": x, "y": y, "width": w, "height": h})
            if ok:
                backend = "playwright-headless-page"
            else:
                return VisionResult(ok=False, error=
                    f"region capture failed: mss missing, playwright: {which}")
        uri = _data_uri(path)
        return VisionResult(
            ok=True,
            output=f"captured region {x},{y} {w}x{h} via {backend} -> {path}",
            parsed={"path": path, "backend": backend, "region": region,
                    "data_uri": uri})
    except Exception as e:
        return VisionResult(ok=False, error=str(e)[:600])


def ui_element_detect(args: dict) -> VisionResult:
    """List interactive UI elements from a page accessibility tree."""
    try:
        url = args.get("url")
        if not url:
            return VisionResult(ok=False, error="url is required")
        res = _playwright_a11y(url)
        if res.get("error"):
            return VisionResult(ok=False, error=res["error"])
        flat: list[dict] = []
        _flatten_snapshot(res.get("snapshot"), flat)
        max_n = int(args.get("max_results", 200))
        flat = flat[:max_n]
        lines = [f"{len(flat)} accessible element(s) on {url}:"]
        for el in flat[:50]:
            lines.append(f"  [{el['role']}] {el['name'][:80]}")
        if len(flat) > 50:
            lines.append(f"  ... ({len(flat) - 50} more)")
        return VisionResult(ok=True, output="\n".join(lines),
                            parsed={"url": url, "count": len(flat),
                                    "elements": flat})
    except Exception as e:
        return VisionResult(ok=False, error=str(e)[:600])


def ui_state_capture(args: dict) -> VisionResult:
    """Return the raw accessibility snapshot JSON of a page."""
    try:
        url = args.get("url")
        if not url:
            return VisionResult(ok=False, error="url is required")
        res = _playwright_a11y(url)
        if res.get("error"):
            return VisionResult(ok=False, error=res["error"])
        snap = res.get("snapshot")
        out_path = args.get("output")
        if out_path:
            Path(out_path).write_text(json.dumps(snap, indent=2))
        return VisionResult(
            ok=True,
            output=f"accessibility snapshot captured for {url}"
                   + (f" -> {out_path}" if out_path else ""),
            parsed={"url": url, "snapshot": snap,
                    "output": out_path or ""})
    except Exception as e:
        return VisionResult(ok=False, error=str(e)[:600])


_GROUND_SCRIPT = r'''
import json, sys
from playwright.sync_api import sync_playwright

def main():
    args = json.loads(sys.argv[1])
    desc = args["description"]
    url = args.get("url", "about:blank")
    out = {"found": False}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page()
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            box = None
            method = ""
            for locator, m in (
                (page.get_by_text(desc, exact=False), "get_by_text"),
                (page.get_by_label(desc, exact=False), "get_by_label"),
                (page.get_by_role("button", name=desc), "get_by_role"),
                (page.get_by_placeholder(desc), "get_by_placeholder"),
            ):
                try:
                    if locator.count() > 0:
                        box = locator.first.bounding_box()
                        method = m
                        if box:
                            break
                except Exception:
                    continue
            if box:
                out = {"found": True, "method": method,
                       "x": box["x"] + box["width"] / 2,
                       "y": box["y"] + box["height"] / 2,
                       "box": box}
            browser.close()
    except Exception as e:
        out = {"error": str(e)[:800]}
    print(json.dumps(out))

main()
'''


def visual_grounding(args: dict) -> VisionResult:
    """Find an element by description; return its center coordinates."""
    try:
        desc = args.get("description")
        if not desc:
            return VisionResult(ok=False, error="description is required")
        res = _run_playwright_script(
            _GROUND_SCRIPT,
            {"description": desc, "url": args.get("url", "about:blank")},
            timeout=90)
        if res.get("error"):
            return VisionResult(ok=False, error=res["error"])
        if res.get("found"):
            return VisionResult(
                ok=True,
                output=f"found '{desc}' at ({res['x']:.0f}, {res['y']:.0f}) "
                       f"via {res['method']}",
                parsed={"found": True, "x": res["x"], "y": res["y"],
                        "box": res["box"], "method": res["method"]})
        return VisionResult(ok=True,
                            output=f"no element matching '{desc}' found",
                            parsed={"found": False, "x": None, "y": None})
    except Exception as e:
        return VisionResult(ok=False, error=str(e)[:600])


_RECORD_SCRIPT = r'''
import json, sys, time
from playwright.sync_api import sync_playwright

def main():
    args = json.loads(sys.argv[1])
    url = args.get("url", "about:blank")
    out_dir = args["out_dir"]
    secs = float(args.get("duration_sec", 5))
    out = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            ctx = browser.new_context(
                record_video_dir=out_dir,
                record_video_size={"width": 1280, "height": 720})
            page = ctx.new_page()
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            time.sleep(secs)
            page.close()
            ctx.close()
            browser.close()
        import os
        vids = [os.path.join(out_dir, f) for f in os.listdir(out_dir)
                if f.endswith(".webm")]
        out = {"video": vids[0] if vids else None}
    except Exception as e:
        out = {"error": str(e)[:800]}
    print(json.dumps(out))

main()
'''


def screen_record(args: dict) -> VisionResult:
    """Record the screen/page for duration_sec; return the video path."""
    try:
        secs = float(args.get("duration_sec", 5))
        if secs <= 0 or secs > 600:
            return VisionResult(ok=False,
                                error="duration_sec must be in (0, 600]")
        out_path = args.get("output")
        # Path 1: real display via ffmpeg x11grab
        if os.environ.get("DISPLAY") and shutil.which("ffmpeg"):
            path = out_path or _shot_path("rec").replace(".png", ".mp4")
            size = args.get("size", "1280x720")
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-f", "x11grab", "-s", size,
                     "-i", os.environ["DISPLAY"], "-t", str(secs),
                     "-pix_fmt", "yuv420p", path],
                    capture_output=True, timeout=secs + 30, check=True)
                if Path(path).exists():
                    return VisionResult(ok=True,
                        output=f"recorded {secs:.0f}s of display -> {path}",
                        parsed={"path": path, "backend": "ffmpeg-x11grab",
                                "duration_sec": secs})
            except Exception as e:
                pass  # fall through to playwright page recording
        # Path 2: Playwright video recording of a page (headless-safe)
        out_dir = tempfile.mkdtemp(prefix="rec_")
        res = _run_playwright_script(
            _RECORD_SCRIPT,
            {"url": args.get("url", "about:blank"), "out_dir": out_dir,
             "duration_sec": secs},
            timeout=int(secs) + 90)
        if res.get("error"):
            return VisionResult(ok=False, error=res["error"])
        video = res.get("video")
        if not video:
            return VisionResult(ok=False,
                                error="playwright produced no video file")
        final = out_path or _shot_path("rec").replace(".png", ".webm")
        shutil.move(video, final)
        shutil.rmtree(out_dir, ignore_errors=True)
        return VisionResult(ok=True,
            output=f"recorded {secs:.0f}s of page -> {final}",
            parsed={"path": final, "backend": "playwright-video",
                    "duration_sec": secs})
    except Exception as e:
        return VisionResult(ok=False, error=str(e)[:600])


def _stream_frame_bytes() -> Optional[bytes]:
    path = _shot_path("stream_frame")
    try:
        if _capture_mss(path):
            pass
        else:
            ok, _ = _capture_playwright_page(path)
            if not ok:
                return None
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass


def live_screen_stream(args: dict) -> VisionResult:
    """Start an MJPEG HTTP server streaming screenshots; stop with stream_stop."""
    try:
        port = int(args.get("port", 0))
        fps = min(max(float(args.get("fps", 1.0)), 0.2), 5.0)
        if port:
            with _STREAMS_LOCK:
                if port in _STREAMS:
                    return VisionResult(ok=False, error=
                        f"stream already running on port {port} "
                        f"(use stream_stop first)")
        # sanity: at least one capture backend must work
        probe = _stream_frame_bytes()
        if probe is None:
            return VisionResult(ok=False, error=
                "no capture backend available (need mss or playwright)")
        stop_event = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/stream.mjpeg":
                    self.send_response(404); self.end_headers(); return
                self.send_response(200)
                self.send_header("Content-Type",
                                 "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                try:
                    while not stop_event.is_set():
                        frame = _stream_frame_bytes()
                        if frame:
                            self.wfile.write(b"--frame\r\n")
                            self.wfile.write(b"Content-Type: image/png\r\n")
                            self.wfile.write(
                                f"Content-Length: {len(frame)}\r\n\r\n"
                                .encode())
                            self.wfile.write(frame + b"\r\n")
                        stop_event.wait(1.0 / fps)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", port), Handler)
        real_port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        with _STREAMS_LOCK:
            _STREAMS[real_port] = {"server": server, "thread": thread,
                                   "stop": stop_event, "fps": fps}
        url = f"http://127.0.0.1:{real_port}/stream.mjpeg"
        return VisionResult(ok=True,
            output=f"MJPEG stream live at {url} ({fps} fps). "
                   f"Stop with stream_stop(port={real_port}).",
            parsed={"port": real_port, "url": url, "fps": fps})
    except Exception as e:
        return VisionResult(ok=False, error=str(e)[:600])


def stream_stop(args: dict) -> VisionResult:
    """Stop a live_screen_stream server keyed by port."""
    try:
        port = int(args.get("port", 0))
        with _STREAMS_LOCK:
            entry = _STREAMS.pop(port, None)
        if not entry:
            return VisionResult(ok=False, error=
                f"no stream running on port {port}")
        entry["stop"].set()
        try:
            entry["server"].shutdown()
            entry["server"].server_close()
        except Exception:
            pass
        return VisionResult(ok=True,
            output=f"stream on port {port} stopped",
            parsed={"port": port, "stopped": True})
    except Exception as e:
        return VisionResult(ok=False, error=str(e)[:600])


def screen_annotate(args: dict) -> VisionResult:
    """Draw boxes+labels onto an image; write annotated copy (non-destructive)."""
    try:
        if Image is None or ImageDraw is None:
            return VisionResult(ok=False, error=
                "PIL not installed (pip install pillow) — annotation needs it")
        src = args.get("image_path")
        if not src or not Path(src).exists():
            return VisionResult(ok=False, error="image_path not found")
        boxes = args.get("boxes", [])
        img = Image.open(src).convert("RGB")
        draw = ImageDraw.Draw(img)
        for b in boxes:
            x, y, w, h = (int(b.get("x", 0)), int(b.get("y", 0)),
                          int(b.get("w", 0)), int(b.get("h", 0)))
            label = str(b.get("label", ""))
            draw.rectangle([x, y, x + w, y + h], outline=(255, 0, 0), width=2)
            if label:
                draw.text((x + 3, y + 3), label, fill=(255, 0, 0))
        out = args.get("output") or str(
            Path(src).with_name(Path(src).stem + "_annotated.png"))
        img.save(out)
        return VisionResult(ok=True,
            output=f"annotated {len(boxes)} box(es) -> {out} (source untouched)",
            parsed={"path": out, "boxes": len(boxes), "source": src})
    except Exception as e:
        return VisionResult(ok=False, error=str(e)[:600])


def _png_scanline_bytes(path: str) -> Optional[tuple[int, int, bytes]]:
    """Pure-stdlib PNG decode to raw bytes (no PIL). Returns (w, h, data)."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    pos, w = 8, 0
    h, bitd, ctype = 0, 0, 0
    idat = b""
    while pos + 8 <= len(data):
        ln = int.from_bytes(data[pos:pos + 4], "big")
        typ = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + ln]
        if typ == b"IHDR":
            w = int.from_bytes(chunk[0:4], "big")
            h = int.from_bytes(chunk[4:8], "big")
            bitd, ctype = chunk[8], chunk[9]
        elif typ == b"IDAT":
            idat += chunk
        elif typ == b"IEND":
            break
        pos += 12 + ln
    if not w or not h or bitd != 8 or ctype not in (2, 6):
        return None
    try:
        raw = zlib.decompress(idat)
    except Exception:
        return None
    ch = 3 if ctype == 2 else 4
    stride = w * ch
    out = bytearray()
    prev = bytearray(stride)
    i = 0
    for _ in range(h):
        if i >= len(raw):
            return None
        f = raw[i]; i += 1
        line = bytearray(raw[i:i + stride]); i += stride
        if len(line) < stride:
            return None
        if f == 1:
            for j in range(ch, stride):
                line[j] = (line[j] + line[j - ch]) & 0xFF
        elif f == 2:
            for j in range(stride):
                line[j] = (line[j] + prev[j]) & 0xFF
        elif f == 3:
            for j in range(stride):
                a = line[j - ch] if j >= ch else 0
                line[j] = (line[j] + ((a + prev[j]) >> 1)) & 0xFF
        elif f == 4:
            for j in range(stride):
                a = line[j - ch] if j >= ch else 0
                b = prev[j]
                c = prev[j - ch] if j >= ch else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[j] = (line[j] + pr) & 0xFF
        elif f != 0:
            return None
        out += line
        prev = line
    return w, h, bytes(out)


def visual_verify(args: dict) -> VisionResult:
    """Pixel-diff two images; report diff ratio and whether identical."""
    try:
        a = args.get("image_a"); b = args.get("image_b")
        if not a or not Path(a).exists():
            return VisionResult(ok=False, error="image_a not found")
        if not b or not Path(b).exists():
            return VisionResult(ok=False, error="image_b not found")
        diff_ratio: Optional[float] = None
        identical = False
        backend = ""
        if Image is not None and ImageChops is not None:
            ia = Image.open(a).convert("RGB")
            ib = Image.open(b).convert("RGB")
            if ia.size == ib.size:
                diff = ImageChops.difference(ia, ib)
                bbox = diff.getbbox()
                if bbox is None:
                    identical, diff_ratio = True, 0.0
                else:
                    total = ia.size[0] * ia.size[1]
                    import math
                    hist = diff.convert("L").histogram()
                    changed = sum(hist[1:])
                    diff_ratio = changed / (total or 1)
            backend = "PIL"
        else:
            pa, pb = _png_scanline_bytes(a), _png_scanline_bytes(b)
            if pa is None or pb is None:
                return VisionResult(ok=False, error=
                    "PIL missing and images are not 8-bit RGB/RGBA PNGs — "
                    "install pillow for general comparison")
            if pa[0] == pb[0] and pa[1] == pb[1] and pa[2] == pb[2]:
                identical, diff_ratio = True, 0.0
            elif pa[0] == pb[0] and pa[1] == pb[1]:
                da, db = pa[2], pb[2]
                n = min(len(da), len(db))
                changed = sum(1 for i in range(n) if da[i] != db[i])
                diff_ratio = changed / (n or 1)
            else:
                diff_ratio = 1.0
            backend = "stdlib-png"
        return VisionResult(
            ok=True,
            output=f"identical={identical} diff_ratio={diff_ratio:.4f} "
                   f"(backend={backend})",
            parsed={"identical": identical, "diff_ratio": diff_ratio,
                    "backend": backend})
    except Exception as e:
        return VisionResult(ok=False, error=str(e)[:600])


# ---------------------------------------------------------------------------

_PW_TEXT_SCRIPT = r"""
import json, sys
from playwright.sync_api import sync_playwright

def main():
    args = json.loads(sys.argv[1])
    out = {}
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, args=["--no-sandbox"])
            pg = b.new_page()
            pg.goto(args["url"], timeout=30000, wait_until="domcontentloaded")
            out["text"] = pg.evaluate(
                "() => document.body ? document.body.innerText : ''")
            b.close()
    except Exception as e:
        out = {"error": str(e)[:800]}
    print(json.dumps(out))

main()
"""


def ocr_read(args: dict) -> VisionResult:
    """OCR text from an image (pytesseract); optional url enables Playwright page-text fallback."""
    try:
        image_path = args.get("image_path")
        url = args.get("url")
        if not image_path:
            image_path = _shot_path("ocr")
            if _capture_mss(image_path):
                cap_backend = "mss"
            else:
                ok, which = _capture_cli(image_path)
                cap_backend = which if ok else ""
            if not cap_backend:
                if url and _playwright_available():
                    res = _run_playwright_script(_PW_TEXT_SCRIPT, {"url": url}, timeout=90)
                    if res.get("error"):
                        return VisionResult(ok=False, error=
                            f"no capture backend; page-text fallback failed: {res['error']}")
                    text = res.get("text", "")
                    return VisionResult(ok=True,
                        output=(text.strip()[:4000] or "(no text)"),
                        parsed={"text": text.strip(), "backend": "playwright-page-text",
                                "url": url})
                return VisionResult(ok=False, error=
                    "no screen-capture backend (no display, no mss/CLI); "
                    "pass image_path or url")
        if not Path(image_path).exists():
            return VisionResult(ok=False, error=f"image not found: {image_path}")
        try:
            import pytesseract  # type: ignore
            if Image is None:
                raise ImportError("PIL missing")
            img = Image.open(image_path)
            text = pytesseract.image_to_string(img)
            conf = None
            try:
                data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
                confs = [int(c) for c in data.get("conf", [])
                         if str(c).strip().lstrip("-").isdigit() and int(c) >= 0]
                conf = round(sum(confs) / len(confs), 1) if confs else None
            except Exception:
                pass
            note = f"\nmean_confidence={conf}" if conf is not None else ""
            return VisionResult(ok=True,
                output=(text.strip()[:4000] or "(no text detected)") + note,
                parsed={"text": text.strip(), "backend": "pytesseract",
                        "mean_confidence": conf, "image": image_path})
        except Exception as te:
            if url and _playwright_available():
                res = _run_playwright_script(_PW_TEXT_SCRIPT, {"url": url}, timeout=90)
                if res.get("error"):
                    return VisionResult(ok=False, error=
                        f"pytesseract unavailable ({te}); page-text fallback failed: {res['error']}")
                text = res.get("text", "")
                return VisionResult(ok=True,
                    output=(text.strip()[:4000] or "(no text)"),
                    parsed={"text": text.strip(), "backend": "playwright-page-text",
                            "url": url, "note": f"pytesseract unavailable: {te}"})
            return VisionResult(ok=False, error=
                f"pytesseract unavailable ({te}). Install tesseract-ocr + pytesseract, "
                f"or pass url for page-text fallback.")
    except Exception as e:
        return VisionResult(ok=False, error=str(e)[:600])


def multi_monitor_info(args: dict) -> VisionResult:
    """List monitors via screeninfo, else xrandr, else mss; honest note when headless."""
    try:
        monitors: list = []
        backend = ""
        try:
            from screeninfo import get_monitors  # type: ignore
            for m in get_monitors():
                monitors.append({"name": str(getattr(m, "name", "")),
                                 "x": m.x, "y": m.y, "width": m.width,
                                 "height": m.height,
                                 "is_primary": bool(getattr(m, "is_primary", False))})
            if monitors:
                backend = "screeninfo"
        except Exception:
            pass
        if not monitors and shutil.which("xrandr") and os.environ.get("DISPLAY"):
            try:
                proc = subprocess.run(["xrandr", "--query"], capture_output=True,
                                      text=True, timeout=15)
                import re as _re
                for line in proc.stdout.splitlines():
                    m = _re.match(r"^(\S+) connected (?:primary )?(\d+)x(\d+)\+(\d+)\+(\d+)", line)
                    if m:
                        monitors.append({"name": m.group(1), "x": int(m.group(4)),
                                         "y": int(m.group(5)), "width": int(m.group(2)),
                                         "height": int(m.group(3)),
                                         "is_primary": "primary" in line})
                if monitors:
                    backend = "xrandr"
            except Exception:
                pass
        if not monitors and mss is not None:
            try:
                with mss.mss() as sct:
                    mons = sct.monitors[1:] if len(sct.monitors) > 1 else sct.monitors
                    for i, mon in enumerate(mons):
                        monitors.append({"name": f"monitor-{i}", "x": mon["left"],
                                         "y": mon["top"], "width": mon["width"],
                                         "height": mon["height"], "is_primary": i == 0})
                if monitors:
                    backend = "mss"
            except Exception:
                pass
        note = f"backend={backend or 'none'}; DISPLAY={os.environ.get('DISPLAY', '(unset)')}"
        if not monitors:
            return VisionResult(ok=True,
                output=f"no monitors detected ({note}); headless host has no real display",
                parsed={"monitors": [], "backend": backend, "note": note})
        desc = "; ".join(f"{m['name']} {m['width']}x{m['height']}+{m['x']}+{m['y']}"
                         for m in monitors)
        return VisionResult(ok=True,
            output=f"{len(monitors)} monitor(s) via {backend}: {desc}",
            parsed={"monitors": monitors, "backend": backend})
    except Exception as e:
        return VisionResult(ok=False, error=str(e)[:600])


def screen_change_detect(args: dict) -> VisionResult:
    """Capture two screenshots interval_sec apart; report changed-region boxes + sha256 diff."""
    try:
        import hashlib
        interval = max(0.2, min(float(args.get("interval_sec", 2.0)), 60.0))
        region = args.get("region")

        def grab(tag: str):
            p = _shot_path(f"chg_{tag}")
            if _capture_mss(p, region):
                return p, "mss"
            ok, which = _capture_cli(p)
            return (p, which) if ok else (None, which)

        p1, b1 = grab("a")
        if not p1:
            return VisionResult(ok=False, error=f"first capture failed: {b1}")
        time.sleep(interval)
        p2, b2 = grab("b")
        if not p2:
            return VisionResult(ok=False, error=f"second capture failed: {b2}")
        h1 = hashlib.sha256(Path(p1).read_bytes()).hexdigest()
        h2 = hashlib.sha256(Path(p2).read_bytes()).hexdigest()
        boxes: list = []
        changed = h1 != h2
        if changed and Image is not None:
            try:
                ia = Image.open(p1).convert("RGB")
                ib = Image.open(p2).convert("RGB")
                if ia.size == ib.size:
                    w, h = ia.size
                    cell = max(32, w // 24)
                    da, db = ia.load(), ib.load()
                    for gy in range(0, h, cell):
                        for gx in range(0, w, cell):
                            hit = False
                            for yy in range(gy, min(gy + cell, h), 4):
                                for xx in range(gx, min(gx + cell, w), 4):
                                    pa, pb = da[xx, yy], db[xx, yy]
                                    if abs(pa[0] - pb[0]) + abs(pa[1] - pb[1]) + abs(pa[2] - pb[2]) > 60:
                                        hit = True
                                        break
                                if hit:
                                    break
                            if hit:
                                boxes.append({"x": gx, "y": gy,
                                              "w": min(cell, w - gx),
                                              "h": min(cell, h - gy)})
            except Exception:
                pass
        return VisionResult(ok=True,
            output=f"changed={changed} regions={len(boxes)} interval={interval}s "
                   f"sha_a={h1[:12]} sha_b={h2[:12]}",
            parsed={"changed": changed, "boxes": boxes, "sha256_a": h1,
                    "sha256_b": h2, "interval_sec": interval,
                    "shot_a": p1, "shot_b": p2})
    except Exception as e:
        return VisionResult(ok=False, error=str(e)[:600])


# Module contract
# ---------------------------------------------------------------------------

def _S(arg_type: str, required: bool, desc: str) -> dict:
    return {"type": arg_type, "required": required, "description": desc}


TOOL_DEFS: list[tuple] = [
    ("screen_shot", "Capture the full screen to PNG (mss/CLI, else headless page render)",
     {"output": _S("string", False, "Optional output PNG path")},
     "computer", 60),
    ("screen_region_capture", "Capture a screen region {x,y,width,height} to PNG",
     {"x": _S("integer", True, "Left"), "y": _S("integer", True, "Top"),
      "width": _S("integer", True, "Width (>0)"),
      "height": _S("integer", True, "Height (>0)"),
      "output": _S("string", False, "Optional output PNG path")},
     "computer", 60),
    ("ui_element_detect", "List interactive elements from a page accessibility tree",
     {"url": _S("string", True, "Page URL"),
      "max_results": _S("integer", False, "Cap on elements (default 200)")},
     "computer", 90),
    ("ui_state_capture", "Raw accessibility snapshot JSON of a page",
     {"url": _S("string", True, "Page URL"),
      "output": _S("string", False, "Optional JSON output path")},
     "computer", 90),
    ("visual_grounding", "Locate an element by description; return center {x,y}",
     {"description": _S("string", True, "Element description text"),
      "url": _S("string", False, "Page URL (default about:blank)")},
     "computer", 90),
    ("screen_record", "Record screen (ffmpeg x11grab) or page (Playwright video)",
     {"duration_sec": _S("number", False, "Seconds, (0,600] (default 5)"),
      "output": _S("string", False, "Optional output video path"),
      "url": _S("string", False, "Page URL for page recording")},
     "computer", 700),
    ("live_screen_stream", "Start MJPEG HTTP stream of screenshots; stop with stream_stop",
     {"port": _S("integer", False, "Port (0 = auto)"),
      "fps": _S("number", False, "Frames/sec 0.2-5 (default 1)")},
     "computer", 60),
    ("stream_stop", "Stop an MJPEG stream started by live_screen_stream",
     {"port": _S("integer", True, "Stream port")},
     "computer", 30),
    ("screen_annotate", "Draw boxes+labels on an image copy (needs PIL)",
     {"image_path": _S("string", True, "Source image"),
      "boxes": _S("array", False, "List of {x,y,w,h,label}"),
      "output": _S("string", False, "Output path (default *_annotated.png)")},
     "computer", 60),
    ("visual_verify", "Pixel-diff two images; report identical + diff ratio",
     {"image_a": _S("string", True, "First image"),
      "image_b": _S("string", True, "Second image")},
     "computer", 60),
    ("ocr_read", "OCR text from image_path (pytesseract); url enables Playwright page-text fallback",
     {"image_path": _S("string", False, "PNG image to OCR"),
      "url": _S("string", False, "Page URL for page-text fallback")},
     "computer", 120),
    ("multi_monitor_info", "List detected monitors (screeninfo/xrandr/mss); honest headless note",
     {}, "computer", 30),
    ("screen_change_detect", "Two screenshots interval apart; changed-region boxes + sha256 diff",
     {"interval_sec": _S("number", False, "Seconds between shots (default 2)"),
      "region": _S("object", False, "{x,y,width,height} region to watch")},
     "computer", 120),

]

HANDLERS: dict[str, Any] = {
    "screen_shot": screen_shot,
    "screen_region_capture": screen_region_capture,
    "ui_element_detect": ui_element_detect,
    "ui_state_capture": ui_state_capture,
    "visual_grounding": visual_grounding,
    "screen_record": screen_record,
    "live_screen_stream": live_screen_stream,
    "stream_stop": stream_stop,
    "screen_annotate": screen_annotate,
    "visual_verify": visual_verify,
    "ocr_read": ocr_read,
    "multi_monitor_info": multi_monitor_info,
    "screen_change_detect": screen_change_detect,

}

__all__ = ["VisionResult", "TOOL_DEFS", "HANDLERS"]
