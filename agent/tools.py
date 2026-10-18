"""Clean tool registry — dict-based dispatch, no complex classes.

Core tools: shell, python, http_get, file ops, browser.
The 5 locked denials are enforced here (they can never execute).
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

# The 5 locked denials — these tool names / actions NEVER execute.
DENIED = {
    "container_escape",
    "boot_recovery",
    "av_edr_disable",
    "billing_fraud",
    "exfiltration",
}

WORKSPACE = Path(os.environ.get("AGENT_WORKSPACE", "/tmp/agent-workspace"))
WORKSPACE.mkdir(parents=True, exist_ok=True)


def _ok(**kw):
    return {"ok": True, **kw}


def _err(msg):
    return {"ok": False, "error": str(msg)[:500]}


def tool_shell(command: str, timeout: int = 60) -> dict:
    """Run a shell command. Returns stdout/stderr/exit code."""
    try:
        p = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=timeout,
            cwd=str(WORKSPACE),
        )
        return _ok(stdout=p.stdout[-4000:], stderr=p.stderr[-2000:],
                   exit_code=p.returncode)
    except subprocess.TimeoutExpired:
        return _err(f"timeout after {timeout}s")
    except Exception as e:
        return _err(e)


def tool_python(code: str, timeout: int = 60) -> dict:
    """Run Python code. Returns stdout/stderr/exit code."""
    try:
        p = subprocess.run(
            ["python3", "-c", code], capture_output=True, text=True,
            timeout=timeout, cwd=str(WORKSPACE),
        )
        return _ok(stdout=p.stdout[-4000:], stderr=p.stderr[-2000:],
                   exit_code=p.returncode)
    except subprocess.TimeoutExpired:
        return _err(f"timeout after {timeout}s")
    except Exception as e:
        return _err(e)


def tool_pip(package: str) -> dict:
    """pip install a package."""
    return tool_shell(f"pip install {package}", timeout=180)


def tool_http_get(url: str, timeout: int = 30) -> dict:
    """Fetch a URL. Returns status, headers, body (truncated)."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0", "ngrok-skip-browser-warning": "true"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
        text = body.decode("utf-8", "replace")
        return _ok(status=getattr(r, "status", 200), url=url,
                   bytes=len(body), body=text[:6000])
    except urllib.error.HTTPError as e:
        return _err(f"HTTP {e.code} for {url}")
    except Exception as e:
        return _err(e)


def _safe_path(path: str) -> Path:
    p = (WORKSPACE / path).resolve()
    if not str(p).startswith(str(WORKSPACE.resolve())):
        raise ValueError("path escapes workspace")
    return p


def tool_file_read(path: str) -> dict:
    try:
        return _ok(path=path, content=_safe_path(path).read_text()[:20000])
    except Exception as e:
        return _err(e)


def tool_file_write(path: str, content: str) -> dict:
    try:
        p = _safe_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return _ok(path=path, bytes=len(content))
    except Exception as e:
        return _err(e)


def tool_file_list(path: str = ".") -> dict:
    try:
        entries = sorted(os.listdir(_safe_path(path)))
        return _ok(path=path, entries=entries[:200])
    except Exception as e:
        return _err(e)


def tool_file_delete(path: str, confirm: bool = False) -> dict:
    if not confirm:
        return _err("destructive: pass confirm=true")
    try:
        _safe_path(path).unlink()
        return _ok(deleted=path)
    except Exception as e:
        return _err(e)


# ---- Browser (Playwright, lazy import so server starts fast) ----
# SPEED OPTIMIZED: headless-shell (lightest build), minimal args, single reused
# instance, images/fonts/media blocked by default, 15s page timeout.

_browser = None


def _get_browser():
    """Launch once, reuse forever. headless-shell = ~100MB vs ~170MB full."""
    global _browser
    if _browser is not None:
        return _browser
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    # Try headless-shell first (lightest); fall back to full chromium.
    browser = None
    last_err = None
    for kwargs in ({"channel": "chromium-headless-shell"}, {}):
        try:
            browser = pw.chromium.launch(headless=True, args=[
                "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                "--disable-extensions", "--disable-background-networking",
                "--disable-sync", "--mute-audio",
            ], **kwargs)
            break
        except Exception as e:
            last_err = e
    if browser is None:
        raise RuntimeError(f"browser launch failed: {last_err}")
    ctx = browser.new_context(viewport={"width": 800, "height": 600})
    page = ctx.new_page()

    # Block heavy resources by default — pages load 3-5x faster.
    def _route(route):
        if route.request.resource_type in ("image", "font", "media"):
            route.abort()
        else:
            route.continue_()
    page.route("**/*", _route)
    page.set_default_timeout(15000)
    page.set_default_navigation_timeout(15000)
    _browser = (pw, browser, page)
    return _browser


def tool_browser_navigate(url: str, timeout: int = 15) -> dict:
    """Open a URL. 15s cap — never hangs."""
    try:
        _, _, page = _get_browser()
        page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
        return _ok(url=page.url, title=page.title()[:200])
    except Exception as e:
        return _err(e)


def tool_browser_screenshot(full_page: bool = False, high_quality: bool = False) -> dict:
    """Screenshot the current page as data URI.

    Fast mode (default): 800x600 viewport PNG — for streaming/previews.
    high_quality=True: full-page PNG at device scale — for detailed inspection.
    """
    try:
        _, _, page = _get_browser()
        if high_quality:
            buf = page.screenshot(full_page=True)
        else:
            buf = page.screenshot(full_page=full_page)
        return _ok(url=page.url, high_quality=high_quality,
                   data_uri="data:image/png;base64," + base64.b64encode(buf).decode())
    except Exception as e:
        return _err(e)


def tool_browser_element_screenshot(selector: str, timeout: int = 10) -> dict:
    """Screenshot a single element (CSS selector) as data URI. God-mode precision."""
    try:
        _, _, page = _get_browser()
        el = page.locator(selector).first
        el.wait_for(state="visible", timeout=timeout * 1000)
        buf = el.screenshot()
        return _ok(url=page.url, selector=selector,
                   data_uri="data:image/png;base64," + base64.b64encode(buf).decode())
    except Exception as e:
        return _err(e)


def tool_browser_see() -> dict:
    """GOD-MODE EYES: see the page like a human does.

    Returns everything at once:
      - screenshot: data URI of what the page LOOKS like right now
      - text: visible text content
      - interactive: list of clickable/fillable elements (role, name)
    Use this when you need to UNDERSTAND a page visually, not just read HTML.
    """
    try:
        _, _, page = _get_browser()
        buf = page.screenshot()
        text = page.inner_text("body")[:6000]
        interactive = []
        try:
            for el in page.locator("a, button, input, select, textarea, [role=button]").all()[:60]:
                try:
                    role = el.evaluate("e => e.tagName.toLowerCase()")
                    name = (el.inner_text() or el.get_attribute("aria-label")
                            or el.get_attribute("placeholder") or el.get_attribute("name") or "")[:60]
                    if name.strip():
                        interactive.append(f"{role}: {name.strip()}")
                except Exception:
                    continue
        except Exception:
            pass
        return _ok(
            url=page.url, title=page.title()[:200],
            screenshot="data:image/png;base64," + base64.b64encode(buf).decode(),
            text=text,
            interactive=interactive[:60],
        )
    except Exception as e:
        return _err(e)


def tool_browser_text() -> dict:
    try:
        _, _, page = _get_browser()
        return _ok(url=page.url, text=page.inner_text("body")[:8000])
    except Exception as e:
        return _err(e)


def tool_browser_close() -> dict:
    global _browser
    try:
        if _browser:
            pw, b, _ = _browser
            b.close()
            pw.stop()
            _browser = None
        return _ok(closed=True)
    except Exception as e:
        return _err(e)


# ---- MJPEG live view: throttled frame capture (max ~5 fps, 800x600) ----

import time as _time

_last_frame_ts = 0.0
_last_frame_bytes = b""


def browser_frame_jpeg(min_interval: float = 0.2) -> bytes:
    """Capture one JPEG frame for the MJPEG stream.

    Throttled to min_interval seconds (default 0.2 = 5 fps) to cap CPU.
    Returns b"" when no browser/page is active or on error.
    """
    global _last_frame_ts, _last_frame_bytes
    now = _time.monotonic()
    if now - _last_frame_ts < min_interval:
        return _last_frame_bytes
    try:
        _, _, page = _get_browser()
        buf = page.screenshot(type="jpeg", quality=60)
        _last_frame_ts = now
        _last_frame_bytes = buf
        return buf
    except Exception:
        return b""


# ---- Self-modification: the agent can CREATE new tools and USE them ----

def tool_create(name: str, code: str, description: str = "") -> dict:
    """CREATE a brand-new tool from Python code, then use it like any other.

    code must define a function run(**kwargs) returning a dict.
    Example:
        tool_create(name="my_tool",
                    code="def run(url):\\n    return {'url': url}",
                    description="does things")
    After creation, call it immediately: <tool_call>{"name": "my_tool", ...}
    """
    try:
        if not name.replace("_", "").isalnum() or not name:
            return _err("name must be alphanumeric/underscore")
        if name in REGISTRY or name in DENIED:
            return _err(f"cannot create/replace '{name}'")
        namespace: dict = {}
        exec(compile(code, f"<tool:{name}>", "exec"), namespace)
        fn = namespace.get("run")
        if not callable(fn):
            return _err("code must define a function named run(**kwargs) returning a dict")
        REGISTRY[name] = (fn, description or f"Custom tool '{name}'.",
                          {"args": "varies — see description"})
        return _ok(created=name, description=description,
                   note=f"Tool '{name}' is live. Call it now with a <tool_call>.")
    except Exception as e:
        return _err(f"tool creation failed: {e}")


def tool_web_search(query: str, max_results: int = 8) -> dict:
    """Web search via DuckDuckGo (no API key). Returns titles/URLs/snippets."""
    try:
        import html as _html
        url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            page = r.read().decode("utf-8", "replace")
        out = []
        for m in re.finditer(
                r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
                r'class="result__snippet"[^>]*>(.*?)</a>',
                page, re.DOTALL):
            link, title, snip = m.groups()
            clean = lambda s: _html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
            out.append({"title": clean(title), "url": link, "snippet": clean(snip)[:300]})
            if len(out) >= max_results:
                break
        return _ok(query=query, results=out)
    except Exception as e:
        return _err(e)


def tool_web_scrape(url: str, timeout: int = 30) -> dict:
    """Reader-style scrape: fetch a page, return clean text + links + images."""
    try:
        import html as _html
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0", "ngrok-skip-browser-warning": "true"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ",
                      raw, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = _html.unescape(re.sub(r"\s+", " ", text)).strip()
        links = sorted(set(re.findall(r'href="(https?://[^"]+)"', raw)))[:50]
        images = sorted(set(re.findall(r'<img[^>]+src="([^"]+)"', raw)))[:30]
        title = re.search(r"<title[^>]*>(.*?)</title>", raw,
                          re.DOTALL | re.IGNORECASE)
        return _ok(url=url,
                   title=_html.unescape(title.group(1)).strip()[:200] if title else "",
                   text=text[:12000], links=links, images=images)
    except Exception as e:
        return _err(e)


def tool_view_media(url: str, timeout: int = 30) -> dict:
    """Fetch an image/video URL and report its real metadata (type, size, dims)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = r.headers.get("Content-Type", "")
            data = r.read(5 * 1024 * 1024)
        info = {"url": url, "content_type": ctype, "bytes": len(data)}
        if ctype.startswith("image/"):
            try:
                import struct
                if data[:8] == b"\x89PNG\r\n\x1a\n":
                    w, h = struct.unpack(">II", data[16:24])
                    info["dimensions"] = f"{w}x{h}"
                elif data[:2] == b"\xff\xd8":
                    info["dimensions"] = "jpeg (dims not parsed)"
            except Exception:
                pass
        return _ok(**info)
    except Exception as e:
        return _err(e)


def tool_image_search(query: str, max_results: int = 8) -> dict:
    """Image search via DuckDuckGo. Returns image URLs + source pages."""
    try:
        import html as _html
        url = ("https://duckduckgo.com/?q=" + urllib.parse.quote(query)
               + "&iax=images&ia=images")
        vqd = None
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            page = r.read().decode("utf-8", "replace")
        m = re.search(r"vqd=([\d-]+)", page)
        if m:
            vqd = m.group(1)
        out = []
        if vqd:
            api = ("https://duckduckgo.com/i.js?l=us-en&o=json&q="
                   + urllib.parse.quote(query) + "&vqd=" + vqd)
            req2 = urllib.request.Request(api, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req2, timeout=20) as r2:
                data = json.loads(r2.read().decode("utf-8"))
            for item in data.get("results", [])[:max_results]:
                out.append({"image": item.get("image"),
                            "thumbnail": item.get("thumbnail"),
                            "title": _html.unescape(item.get("title", ""))[:120],
                            "source": item.get("url")})
        return _ok(query=query, results=out)
    except Exception as e:
        return _err(e)


def tool_context_grep(pattern: str, path: str = ".", max_hits: int = 40) -> dict:
    """Grep for a pattern inside the workspace. Returns file:line matches."""
    try:
        base = _safe_path(path)
        hits = []
        rx = re.compile(pattern)
        for root, _, files in os.walk(base):
            for fn in files:
                fp = Path(root) / fn
                try:
                    if fp.stat().st_size > 2_000_000:
                        continue
                    for i, line in enumerate(fp.read_text(errors="replace").splitlines(), 1):
                        if rx.search(line):
                            rel = fp.relative_to(WORKSPACE.resolve())
                            hits.append(f"{rel}:{i}: {line.strip()[:160]}")
                            if len(hits) >= max_hits:
                                return _ok(pattern=pattern, hits=hits)
                except Exception:
                    continue
        return _ok(pattern=pattern, hits=hits)
    except Exception as e:
        return _err(e)


def tool_get_user_balance() -> dict:
    """User balance. Reads USER_BALANCE env if set; otherwise honestly reports unconfigured."""
    val = os.environ.get("USER_BALANCE")
    if val:
        return _ok(balance=val, source="USER_BALANCE env")
    return {"ok": False,
            "error": "no balance source configured (set USER_BALANCE env). Not inventing a number."}


def tool_crypto_market_data(symbol: str = "bitcoin") -> dict:
    """Crypto price/market data via CoinGecko free API (no key)."""
    try:
        coin = symbol.lower().strip()
        url = ("https://api.coingecko.com/api/v3/simple/price?ids="
               + urllib.parse.quote(coin)
               + "&vs_currencies=usd&include_24hr_change=true&include_market_cap=true")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
        if coin not in data:
            return _err(f"coin '{symbol}' not found on CoinGecko")
        d = data[coin]
        return _ok(symbol=coin, usd=d.get("usd"),
                   change_24h_pct=d.get("usd_24h_change"),
                   market_cap_usd=d.get("usd_market_cap"))
    except Exception as e:
        return _err(e)


def tool_currency_conversion(amount: float, from_cur: str, to_cur: str) -> dict:
    """Currency conversion via open.er-api.com (free, no key)."""
    try:
        url = "https://open.er-api.com/v6/latest/" + urllib.parse.quote(from_cur.upper())
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
        rates = data.get("rates", {})
        to = to_cur.upper()
        if to not in rates:
            return _err(f"currency '{to_cur}' not in rates")
        return _ok(amount=amount, from_cur=from_cur.upper(), to_cur=to,
                   rate=rates[to], converted=round(amount * rates[to], 4),
                   date=data.get("time_last_update_utc", ""))
    except Exception as e:
        return _err(e)


def tool_unit_conversion(value: float, from_unit: str, to_unit: str) -> dict:
    """Convert between common units (length, mass, temp, volume, speed, data)."""
    try:
        f, t = from_unit.lower().strip(), to_unit.lower().strip()
        length = {"mm": 0.001, "cm": 0.01, "m": 1.0, "km": 1000.0,
                  "in": 0.0254, "ft": 0.3048, "yd": 0.9144, "mi": 1609.344}
        mass = {"mg": 1e-6, "g": 0.001, "kg": 1.0, "t": 1000.0,
                "oz": 0.0283495, "lb": 0.453592}
        vol = {"ml": 0.001, "l": 1.0, "gal": 3.78541, "floz": 0.0295735,
               "cup": 0.24, "pint": 0.473176}
        speed = {"ms": 1.0, "m/s": 1.0, "kmh": 1 / 3.6, "km/h": 1 / 3.6,
                 "mph": 0.44704, "knot": 0.514444, "fts": 0.3048, "ft/s": 0.3048}
        data = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12,
                "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3}
        for table in (length, mass, vol, speed, data):
            if f in table and t in table:
                return _ok(value=value, from_unit=f, to_unit=t,
                           result=value * table[f] / table[t])
        if f in ("c", "f", "k") and t in ("c", "f", "k"):
            c = {"c": value, "f": (value - 32) * 5 / 9,
                 "k": value - 273.15}[f]
            out = {"c": c, "f": c * 9 / 5 + 32, "k": c + 273.15}[t]
            return _ok(value=value, from_unit=f, to_unit=t, result=out)
        return _err(f"unsupported conversion {from_unit} -> {to_unit}")
    except Exception as e:
        return _err(e)


def tool_calculator(expression: str) -> dict:
    """Safely evaluate a math expression."""
    try:
        import math
        allowed = {k: getattr(math, k) for k in
                   ("pi", "e", "sqrt", "sin", "cos", "tan", "log", "log10",
                    "exp", "pow", "floor", "ceil", "fabs", "degrees", "radians")}
        allowed.update({"abs": abs, "round": round, "min": min, "max": max})
        if not re.fullmatch(r"[\d\s+\-*/().,%^a-z_]+", expression.lower()):
            return _err("invalid characters in expression")
        expr = expression.replace("^", "**")
        result = eval(expr, {"__builtins__": {}}, allowed)  # noqa: S307
        return _ok(expression=expression, result=result)
    except Exception as e:
        return _err(e)


def tool_clock(timezone: str = "UTC") -> dict:
    """Current date/time. Optional IANA timezone, e.g. Asia/Karachi."""
    try:
        import datetime
        try:
            from zoneinfo import ZoneInfo
            now = datetime.datetime.now(ZoneInfo(timezone))
        except Exception:
            now = datetime.datetime.now(datetime.timezone.utc)
            timezone = "UTC"
        return _ok(timezone=timezone, iso=now.isoformat(timespec="seconds"),
                   pretty=now.strftime("%A, %Y-%m-%d %H:%M:%S"))
    except Exception as e:
        return _err(e)


def tool_weather_fetch(lat: float, lon: float) -> dict:
    """Current weather via Open-Meteo (free, no key). Needs lat/lon."""
    try:
        url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
               "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
               "weather_code,wind_speed_10m&timezone=auto")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
        cur = data.get("current", {})
        codes = {0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
                 45: "fog", 48: "rime fog", 51: "light drizzle", 61: "light rain",
                 63: "rain", 71: "light snow", 73: "snow", 80: "light showers",
                 95: "thunderstorm"}
        return _ok(lat=lat, lon=lon, timezone=data.get("timezone"),
                   temp_c=cur.get("temperature_2m"),
                   feels_like_c=cur.get("apparent_temperature"),
                   humidity_pct=cur.get("relative_humidity_2m"),
                   wind_kmh=cur.get("wind_speed_10m"),
                   condition=codes.get(cur.get("weather_code"),
                                       f"code {cur.get('weather_code')}"),
                   time=cur.get("time"))
    except Exception as e:
        return _err(e)


def tool_sports_fetch(league: str = "nba") -> dict:
    """Recent games/scores via balldontlie.io (free, no key)."""
    try:
        url = "https://www.balldontlie.io/api/games?per_page=10"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
        games = []
        for g in data.get("data", [])[:10]:
            games.append({
                "date": g.get("date", "")[:10],
                "home": g["home_team"]["abbreviation"],
                "away": g["visitor_team"]["abbreviation"],
                "home_score": g.get("home_team_score"),
                "away_score": g.get("visitor_team_score"),
                "status": g.get("status"),
            })
        return _ok(league=league, games=games)
    except Exception as e:
        return _err(e)


def tool_location_fetch() -> dict:
    """Server's approximate location via ip-api.com (free, no key)."""
    try:
        req = urllib.request.Request("http://ip-api.com/json/",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
        if data.get("status") != "success":
            return _err("location lookup failed")
        return _ok(ip=data.get("query"), city=data.get("city"),
                   region=data.get("regionName"), country=data.get("country"),
                   lat=data.get("lat"), lon=data.get("lon"),
                   timezone=data.get("timezone"), isp=data.get("isp"))
    except Exception as e:
        return _err(e)


def _media_backend():
    return os.environ.get("MEDIA_GEN_URL", "").rstrip("/")


def tool_generate_image(prompt: str) -> dict:
    """Generate an image. Needs MEDIA_GEN_URL backend; honest if missing."""
    if not _media_backend():
        return {"ok": False, "error":
                "no image-generation backend configured (set MEDIA_GEN_URL). Not fabricating an image."}
    try:
        payload = json.dumps({"prompt": prompt}).encode()
        req = urllib.request.Request(_media_backend() + "/generate",
                                     data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return _ok(**json.loads(r.read().decode("utf-8")))
    except Exception as e:
        return _err(e)


def tool_edit_image(image_url: str, instruction: str) -> dict:
    """Edit an image via MEDIA_GEN_URL backend; honest if missing."""
    if not _media_backend():
        return {"ok": False, "error":
                "no image-edit backend configured (set MEDIA_GEN_URL). Not fabricating."}
    try:
        payload = json.dumps({"image_url": image_url,
                              "instruction": instruction}).encode()
        req = urllib.request.Request(_media_backend() + "/edit", data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return _ok(**json.loads(r.read().decode("utf-8")))
    except Exception as e:
        return _err(e)


def tool_upscale_image(image_url: str, scale: int = 2) -> dict:
    """Upscale an image via MEDIA_GEN_URL backend; honest if missing."""
    if not _media_backend():
        return {"ok": False, "error":
                "no upscale backend configured (set MEDIA_GEN_URL). Not fabricating."}
    try:
        payload = json.dumps({"image_url": image_url, "scale": scale}).encode()
        req = urllib.request.Request(_media_backend() + "/upscale", data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return _ok(**json.loads(r.read().decode("utf-8")))
    except Exception as e:
        return _err(e)


def tool_generate_video_v2(prompt: str, duration_s: int = 5) -> dict:
    """Generate a video via MEDIA_GEN_URL backend; honest if missing."""
    if not _media_backend():
        return {"ok": False, "error":
                "no video-generation backend configured (set MEDIA_GEN_URL). Not fabricating."}
    try:
        payload = json.dumps({"prompt": prompt,
                              "duration_s": duration_s}).encode()
        req = urllib.request.Request(_media_backend() + "/video", data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            return _ok(**json.loads(r.read().decode("utf-8")))
    except Exception as e:
        return _err(e)


def tool_html_sandbox_render(html: str, name: str = "page") -> dict:
    """Save an HTML page to the workspace sandbox. Returns the file path."""
    try:
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:40] or "page"
        p = _safe_path(f"sandbox/{safe}.html")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(html)
        return _ok(path=str(p.relative_to(WORKSPACE.resolve())), bytes=len(html),
                   note="Serve the workspace or open the file to view.")
    except Exception as e:
        return _err(e)


def tool_html_sandbox_readme(text: str, name: str = "README") -> dict:
    """Write a README.md into the sandbox."""
    try:
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:40] or "README"
        p = _safe_path(f"sandbox/{safe}.md")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return _ok(path=str(p.relative_to(WORKSPACE.resolve())))
    except Exception as e:
        return _err(e)


def tool_mermaid_diagram(code: str, name: str = "diagram") -> dict:
    """Save a Mermaid diagram; returns the .mmd path and a live render URL (mermaid.ink)."""
    try:
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:40] or "diagram"
        p = _safe_path(f"sandbox/{safe}.mmd")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(code)
        enc = base64.urlsafe_b64encode(code.encode()).decode()
        render_url = f"https://mermaid.ink/img/{enc}"
        return _ok(path=str(p.relative_to(WORKSPACE.resolve())),
                   render_url=render_url)
    except Exception as e:
        return _err(e)


def tool_questionnaire(questions: list, title: str = "Questionnaire") -> dict:
    """Build a structured questionnaire, save it, and return it for the user to answer."""
    try:
        if not isinstance(questions, list) or not questions:
            return _err("questions must be a non-empty list")
        p = _safe_path("sandbox/questionnaire.json")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"title": title, "questions": questions},
                                indent=2))
        lines = [f"**{title}**"]
        for i, q in enumerate(questions, 1):
            if isinstance(q, dict):
                lines.append(f"{i}. {q.get('q', '')}")
                for o in q.get("options", []) or []:
                    lines.append(f"   - {o}")
            else:
                lines.append(f"{i}. {q}")
        return _ok(title=title, count=len(questions),
                   path="sandbox/questionnaire.json",
                   rendered="\n".join(lines))
    except Exception as e:
        return _err(e)

def tool_curl_cffi_fetch(url: str, impersonate: str = "chrome120", timeout: int = 30) -> dict:
    """Fetch a URL using curl_cffi (bypasses Cloudflare by mimicking browser TLS)."""
    try:
        from curl_cffi import requests as creq
        r = creq.get(url, impersonate=impersonate, timeout=timeout)
        return _ok(status=r.status_code, url=url, size=len(r.content),
                   text=r.text[:5000], headers=dict(r.headers))
    except ImportError:
        return _err("curl_cffi not installed. Run pip install curl_cffi")
    except Exception as e:
        return _err(e)

def tool_flaresolverr_fetch(url: str, solver_url: str = "http://localhost:8191") -> dict:
    """Fetch via FlareSolverr (solves Cloudflare challenges). Needs FlareSolverr running."""
    try:
        import json as js, urllib.request
        payload = js.dumps({
            "cmd": "request.get", "url": url, "maxTimeout": 60000
        }).encode()
        req = urllib.request.Request(solver_url + "/v1",
            data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=70) as resp:
            d = js.loads(resp.read().decode())
        sol = d.get("solution", {})
        return _ok(status=sol.get("status"), url=sol.get("url", url),
                   text=(sol.get("response") or "")[:5000])
    except Exception as e:
        return _err(f"FlareSolverr failed (is it running at {solver_url}?): {e}")


def tool_api_discover(url: str, timeout: int = 30) -> dict:
    """Discover hidden APIs: check sitemap.xml, robots.txt, JS bundles for endpoints/tokens."""
    try:
        import re, urllib.request
        from urllib.parse import urljoin, urlparse
        base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        found = {"sitemap": [], "robots": "", "js_endpoints": [], "apis": []}
        # robots.txt
        try:
            with urllib.request.urlopen(base + "/robots.txt", timeout=10) as r:
                found["robots"] = r.read().decode()[:2000]
        except: pass
        # sitemap.xml
        try:
            with urllib.request.urlopen(base + "/sitemap.xml", timeout=10) as r:
                txt = r.read().decode()[:5000]
                found["sitemap"] = re.findall(r"<loc>(.*?)</loc>", txt)[:20]
        except: pass
        # Fetch page, find JS bundles, extract endpoints
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                html = r.read().decode()
            js_urls = re.findall(r'src="([^"]*\.js[^"]*)"', html)[:10]
            for js in js_urls:
                js_url = urljoin(url, js)
                try:
                    with urllib.request.urlopen(js_url, timeout=10) as jr:
                        js_txt = jr.read().decode()[:50000]
                    # Find API endpoints, tokens
                    eps = re.findall(r'["\'](/[a-zA-Z0-9/_\-\.]+(?:api|v1|v2|graphql)[^"\']*)["\']', js_txt)
                    keys = re.findall(r'["\'](?:api[_-]?key|token|bearer)["\']\s*[:=]\s*["\']([^"\']+)["\']', js_txt, re.I)
                    found["js_endpoints"].extend(eps[:10])
                    found["apis"].extend(keys[:5])
                except: pass
        except Exception as e:
            return _err(e)
        return _ok(**found)
    except Exception as e:
        return _err(e)

def tool_graphql_introspect(endpoint: str, timeout: int = 30) -> dict:
    """Run GraphQL introspection to discover schema."""
    try:
        import json, urllib.request
        q = {"query": "{ __schema { queryType { fields { name } } mutationType { fields { name } } } }"}
        req = urllib.request.Request(endpoint, data=json.dumps(q).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode())
        schema = d.get("data", {}).get("__schema", {})
        queries = [f["name"] for f in (schema.get("queryType", {}) or {}).get("fields", []) or []]
        mutations = [f["name"] for f in (schema.get("mutationType", {}) or {}).get("fields", []) or []]
        return _ok(queries=queries[:50], mutations=mutations[:50])
    except Exception as e:
        return _err(e)

def tool_crawl_paginated(base_url: str, param: str = "page", max_pages: int = 10, timeout: int = 30) -> dict:
    """Crawl paginated URLs: base_url?page=1..N, collect all data (chapa chapa chana)."""
    try:
        import urllib.request
        from urllib.parse import urlencode
        all_data = []
        for p in range(1, max_pages + 1):
            sep = "&" if "?" in base_url else "?"
            url = f"{base_url}{sep}{param}={p}"
            try:
                with urllib.request.urlopen(url, timeout=timeout) as r:
                    txt = r.read().decode()
                    if not txt.strip() or len(txt) < 100:
                        break
                    all_data.append({"page": p, "size": len(txt), "text": txt[:3000]})
            except:
                break
        return _ok(pages_crawled=len(all_data), data=all_data)
    except Exception as e:
        return _err(e)


REGISTRY = {
    "tool_create": (tool_create, "CREATE a brand-new tool from Python code defining run(**kwargs)->dict, then USE it immediately.",
                    {"name": "str (required, new tool name)", "code": "str (required, must define run)", "description": "str"}),
    "shell": (tool_shell, "Run a shell command in the workspace.",
              {"command": "str (required)", "timeout": "int seconds, default 60"}),
    "python": (tool_python, "Run Python code.",
               {"code": "str (required)", "timeout": "int seconds, default 60"}),
    "pip": (tool_pip, "pip install a package.", {"package": "str (required)"}),
    "http_get": (tool_http_get, "Fetch a URL, returns status and body.",
                 {"url": "str (required)", "timeout": "int seconds, default 30"}),
    "file_read": (tool_file_read, "Read a text file from the workspace.",
                  {"path": "str (required, relative to workspace)"}),
    "file_write": (tool_file_write, "Write a text file in the workspace.",
                   {"path": "str (required)", "content": "str (required)"}),
    "file_list": (tool_file_list, "List files in a workspace directory.",
                  {"path": "str, default '.'"}),
    "file_delete": (tool_file_delete, "Delete a file. Requires confirm=true.",
                    {"path": "str (required)", "confirm": "bool (required)"}),
    "browser_navigate": (tool_browser_navigate, "Open a URL in the agent browser (15s cap).",
                         {"url": "str (required)", "timeout": "int seconds, default 15"}),
    "browser_screenshot": (tool_browser_screenshot, "Screenshot the page as data URI. Fast 800x600 by default; high_quality=true for full-page detailed PNG.",
                           {"full_page": "bool, default false", "high_quality": "bool, default false"}),
    "browser_element_screenshot": (tool_browser_element_screenshot, "Screenshot ONE element by CSS selector as data URI.",
                           {"selector": "str (required, CSS)", "timeout": "int seconds, default 10"}),
    "browser_see": (tool_browser_see, "GOD-MODE EYES: screenshot + visible text + interactive elements, all at once. Use to UNDERSTAND a page visually.", {}),
    "browser_text": (tool_browser_text, "Get the visible text of the current page.", {}),
    "browser_close": (tool_browser_close, "Close the browser.", {}),
    "web_search": (tool_web_search, "Web search (DuckDuckGo, no key). Returns titles/URLs/snippets.",
                  {"query": "str (required)", "max_results": "int, default 8"}),
    "web_scrape": (tool_web_scrape, "Reader-style scrape: clean text + links + images from a page.",
                   {"url": "str (required)", "timeout": "int, default 30"}),
    "view_media": (tool_view_media, "Fetch an image/video URL, report real metadata (type, size, dims).",
                   {"url": "str (required)", "timeout": "int, default 30"}),
    "image_search": (tool_image_search, "Image search (DuckDuckGo). Returns image URLs + sources.",
                     {"query": "str (required)", "max_results": "int, default 8"}),
    "context_grep": (tool_context_grep, "Grep a pattern inside the workspace. Returns file:line hits.",
                     {"pattern": "str (required, regex)", "path": "str, default '.'", "max_hits": "int, default 40"}),
    "get_user_balance": (tool_get_user_balance, "User balance (from USER_BALANCE env; honest if unconfigured).", {}),
    "crypto_market_data": (tool_crypto_market_data, "Crypto price/market data (CoinGecko free).",
                           {"symbol": "str, default 'bitcoin'"}),
    "currency_conversion": (tool_currency_conversion, "Currency conversion (free rates API).",
                            {"amount": "float (required)", "from_cur": "str (required)", "to_cur": "str (required)"}),
    "unit_conversion": (tool_unit_conversion, "Convert units: length, mass, temp, volume, speed, data.",
                        {"value": "float (required)", "from_unit": "str (required)", "to_unit": "str (required)"}),
    "calculator": (tool_calculator, "Safely evaluate a math expression.",
                   {"expression": "str (required)"}),
    "clock": (tool_clock, "Current date/time, optional IANA timezone.",
              {"timezone": "str, default 'UTC'"}),
    "weather_fetch": (tool_weather_fetch, "Current weather (Open-Meteo free). Needs lat/lon.",
                      {"lat": "float (required)", "lon": "float (required)"}),
    "sports_fetch": (tool_sports_fetch, "Recent games/scores (balldontlie.io free).",
                     {"league": "str, default 'nba'"}),
    "location_fetch": (tool_location_fetch, "Server's approximate location (ip-api.com free).", {}),
    "generate_image": (tool_generate_image, "Generate an image (needs MEDIA_GEN_URL backend).",
                       {"prompt": "str (required)"}),
    "edit_image": (tool_edit_image, "Edit an image (needs MEDIA_GEN_URL backend).",
                   {"image_url": "str (required)", "instruction": "str (required)"}),
    "upscale_image": (tool_upscale_image, "Upscale an image (needs MEDIA_GEN_URL backend).",
                      {"image_url": "str (required)", "scale": "int, default 2"}),
    "generate_video_v2": (tool_generate_video_v2, "Generate a video (needs MEDIA_GEN_URL backend).",
                          {"prompt": "str (required)", "duration_s": "int, default 5"}),
    "html_sandbox_render": (tool_html_sandbox_render, "Save an HTML page to the workspace sandbox.",
                            {"html": "str (required)", "name": "str, default 'page'"}),
    "html_sandbox_readme": (tool_html_sandbox_readme, "Write a README.md into the sandbox.",
                            {"text": "str (required)", "name": "str, default 'README'"}),
    "mermaid_diagram": (tool_mermaid_diagram, "Save a Mermaid diagram + live render URL.",
                        {"code": "str (required)", "name": "str, default 'diagram'"}),
    "curl_cffi_fetch": (tool_curl_cffi_fetch, "Fetch URL via curl_cffi (Cloudflare bypass, browser TLS impersonation).",
                       {"url": "str (required)", "impersonate": "str, default 'chrome120'", "timeout": "int, default 30"}),
    "flaresolverr_fetch": (tool_flaresolverr_fetch, "Fetch via FlareSolverr (solves Cloudflare JS challenges).",
                          {"url": "str (required)", "solver_url": "str, default 'http://localhost:8191'"}),
    "api_discover": (tool_api_discover, "Discover hidden APIs: sitemap, robots.txt, JS bundle endpoints/tokens.",
                    {"url": "str (required)", "timeout": "int, default 30"}),
    "graphql_introspect": (tool_graphql_introspect, "GraphQL introspection to discover schema.",
                          {"endpoint": "str (required)", "timeout": "int, default 30"}),
    "crawl_paginated": (tool_crawl_paginated, "Crawl paginated URLs thoroughly (chapa chapa chana).",
                       {"base_url": "str (required)", "param": "str, default 'page'", "max_pages": "int, default 10", "timeout": "int, default 30"}),
    "questionnaire": (tool_questionnaire, "Build a structured questionnaire for the user to answer.",
                      {"questions": "list (required)", "title": "str, default 'Questionnaire'"}),
}


def describe_tools() -> str:
    lines = []
    for name, (fn, desc, schema) in sorted(REGISTRY.items()):
        args = ", ".join(f"{k}: {v}" for k, v in schema.items()) or "no args"
        lines.append(f"- {name}: {desc} Args: {args}")
    return "\n".join(lines)


def execute_tool(name: str, args: dict) -> dict:
    """Dispatch a tool call. Enforces the 5 locked denials."""
    if name in DENIED:
        return _err(f"DENIED: {name} is one of the 5 locked denials and can never run.")
    entry = REGISTRY.get(name)
    if not entry:
        return _err(f"unknown tool: {name}")
    fn = entry[0]
    try:
        allowed = {k: v for k, v in (args or {}).items()
                   if k in fn.__code__.co_varnames}
        return fn(**allowed)
    except TypeError as e:
        return _err(f"bad args for {name}: {e}")
    except Exception as e:
        return _err(e)


def tool_names() -> list:
    return sorted(REGISTRY.keys())
