"""Live browser session — real-time interactive browser like the assistant's.

A persistent Playwright Chromium session with:
- MJPEG live stream (/api/browser/live) — real-time, like watching a screen
- Input events (click, type, scroll, keys) forwarded to the page
- Navigation to any URL
- Tor mode (routes through Tor SOCKS for .onion sites)

Memory-conscious: single browser, single page, JPEG quality tuned for 512MB.
"""

import io
import threading
import time

_lock = threading.Lock()
_session = None


class LiveSession:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.page = None
        self.tor_enabled = False
        self._last_frame = None
        self._frame_time = 0

    def _launch(self):
        from playwright.sync_api import sync_playwright
        if self.playwright is None:
            self.playwright = sync_playwright().start()
        proxy = None
        if self.tor_enabled:
            try:
                from agent.browser.tor_manager import ensure_tor
            except ImportError:
                from browser.tor_manager import ensure_tor  # type: ignore
            ensure_tor()
            proxy = {"server": "socks5://127.0.0.1:9050"}
        self.browser = self.playwright.chromium.launch(
            headless=True,
            proxy=proxy,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--single-process",
                "--no-zygote",
            ],
        )
        ctx = self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="de-DE",
            timezone_id="Europe/Berlin",
        )
        self.page = ctx.new_page()
        # Start blank — agent navigates when needed. (DuckDuckGo as the
        # initial page timed out on Render's network.)
        self.page.goto("about:blank", timeout=10000)

    def ensure(self):
        with _lock:
            if self.page is None or self.page.is_closed():
                self.close_locked()
                self._launch()
            return self.page

    def close_locked(self):
        try:
            if self.browser:
                self.browser.close()
        except Exception:
            pass
        self.browser = None
        self.page = None

    def close(self):
        with _lock:
            self.close_locked()

    def set_tor(self, enabled: bool):
        with _lock:
            if enabled != self.tor_enabled:
                self.tor_enabled = enabled
                self.close_locked()
                self._launch()
            return self.tor_enabled

    def navigate(self, url: str):
        page = self.ensure()
        if not url.startswith(("http://", "https://")):
            # Plain text → DuckDuckGo search (Germany)
            from urllib.parse import quote_plus
            url = f"https://duckduckgo.com/?q={quote_plus(url)}&kl=de-de"
        # .onion → force Tor on
        if ".onion" in url and not self.tor_enabled:
            self.set_tor(True)
        page = self.ensure()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return page.url

    def frame_jpeg(self, quality: int = 55) -> bytes | None:
        """Capture current frame as JPEG bytes. Cached 150ms."""
        now = time.time()
        if self._last_frame and (now - self._frame_time) < 0.15:
            return self._last_frame
        try:
            page = self.ensure()
            shot = page.screenshot(type="jpeg", quality=quality)
            self._last_frame = shot
            self._frame_time = now
            return shot
        except Exception:
            return self._last_frame

    def click(self, x: float, y: float):
        """x, y are 0..1 relative coords."""
        page = self.ensure()
        w, h = 1280, 800
        page.mouse.click(x * w, y * h)

    def scroll(self, dx: float, dy: float):
        page = self.ensure()
        page.mouse.wheel(dx * 400, dy * 400)

    def key(self, key: str):
        page = self.ensure()
        if len(key) == 1:
            page.keyboard.type(key)
        else:
            page.keyboard.press(key)

    def type_text(self, text: str):
        page = self.ensure()
        page.keyboard.type(text)


def get_session() -> LiveSession:
    global _session
    if _session is None:
        _session = LiveSession()
    return _session
