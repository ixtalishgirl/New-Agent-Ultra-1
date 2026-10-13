"""Playwright lifecycle + per-task isolated browser contexts.

Design:
- One shared Playwright + Chromium browser instance (saves RAM vs one per task).
- Each *task* (one research job from the Node app) gets its own isolated
  BrowserContext: separate cookies, storage, and tabs. Tabs are individual
  Page objects inside that context.
- Idle tasks are evicted after ``task_idle_ttl_sec`` to free memory.
- Proxy from env (BROWSER_HTTP(S)_PROXY) applies to every context.
- NEVER attempts to bypass CAPTCHAs / bot checks — see tools/detect_blocks.py.

All public methods raise BrowserError (or subclasses) with a machine-readable
``code`` so the REST layer can return structured errors.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from config import Settings

# Playwright is OPTIONAL at import time: the service must boot even when the
# browser stack isn't installed (fetch-only mode: search/extract/recon work;
# browser tools fail honestly with code='browser_unavailable').
try:
    from playwright.async_api import (
        Browser,
        BrowserContext,
        Page,
        Playwright,
        TimeoutError as PlaywrightTimeoutError,
        async_playwright,
    )
    _PLAYWRIGHT_IMPORT_ERROR: Optional[str] = None
except Exception as e:  # pragma: no cover - environment dependent
    Browser = BrowserContext = Page = Playwright = PlaywrightTimeoutError = None  # type: ignore
    async_playwright = None  # type: ignore
    _PLAYWRIGHT_IMPORT_ERROR = str(e)[:300]


# ---------- errors ----------

class BrowserError(Exception):
    code = "browser_error"

    def __init__(self, message: str, code: str | None = None, details: dict | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details or {}


class TaskNotFoundError(BrowserError):
    code = "task_not_found"


class TabNotFoundError(BrowserError):
    code = "tab_not_found"


class NavigationError(BrowserError):
    code = "navigation_failed"


class BlockedHostError(BrowserError):
    code = "blocked_host"


class BlockedPageError(BrowserError):
    code = "page_blocked"
    # Raised when the operator policy refuses an action on a blocked page.
    # Detection itself lives in tools/detect_blocks.py.


class ToolTimeoutError(BrowserError):
    code = "tool_timeout"


# ---------- task record ----------

class Task:
    def __init__(self, task_id: str, context: BrowserContext):
        self.id = task_id
        self.context = context
        self.pages: Dict[str, Page] = {}
        self.active_tab: Optional[str] = None
        self.created_at = time.time()
        self.last_used = time.time()
        self.last_extract_links: List[Dict[str, str]] = []  # for open_link(index=...)
        self._rate_lock = asyncio.Lock()
        self._last_request_ts = 0.0

    def touch(self) -> None:
        self.last_used = time.time()

    async def polite_wait(self, min_interval_ms: int) -> None:
        """Rate-limit navigation-class actions per task (HUMAN BEHAVIOR rule)."""
        async with self._rate_lock:
            now = time.monotonic() * 1000
            wait = min_interval_ms - (now - self._last_request_ts)
            if wait > 0:
                await asyncio.sleep(wait / 1000)
            self._last_request_ts = time.monotonic() * 1000


# ---------- manager ----------

class BrowserManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._tasks: Dict[str, Task] = {}
        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._janitor_task: Optional[asyncio.Task] = None
        # Lazy-launch state (free-tier friendly: no Chromium RAM until first use).
        self.browser_error: Optional[str] = None

    async def ensure_started(self) -> None:
        """Idempotent lazy browser launch. First browser-needing call pays the
        cost; failures are recorded (browser_error) and raised as BrowserError
        with code='browser_unavailable' instead of crashing the service."""
        if self._browser is not None:
            return
        if async_playwright is None:
            self.browser_error = (
                f"playwright not installed ({_PLAYWRIGHT_IMPORT_ERROR}). "
                "Fetch-based tools (search, extract, recon) still work.")
            raise BrowserError(
                "browser unavailable: playwright package not installed. "
                "Fetch-based tools (search, extract, recon) still work.",
                code="browser_unavailable")
        async with self._start_lock:
            if self._browser is not None:
                return
            try:
                await self.start()
                self.browser_error = None
            except Exception as e:
                self.browser_error = str(e)[:500]
                raise BrowserError(
                    f"browser unavailable: {self.browser_error}. "
                    "Fetch-based tools (search, extract, recon) still work.",
                    code="browser_unavailable") from e

    # ----- lifecycle -----

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        launch_kwargs: Dict[str, Any] = {
            "headless": self.settings.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",  # needed in most Docker/VPS environments
                "--disable-dev-shm-usage",  # /dev/shm is tiny in Docker
                "--disable-gpu",  # free-tier: no GPU, saves RAM
                "--disable-software-rasterizer",
            ],
        }
        if self.settings.slow_mo_ms:
            launch_kwargs["slow_mo"] = self.settings.slow_mo_ms
        proxy = self.settings.playwright_proxy()
        if proxy:
            launch_kwargs["proxy"] = proxy
        self._browser = await self._pw.chromium.launch(**launch_kwargs)
        self._janitor_task = asyncio.create_task(self._janitor_loop())

    async def stop(self) -> None:
        if self._janitor_task:
            self._janitor_task.cancel()
        for task in list(self._tasks.values()):
            try:
                await task.context.close()
            except Exception:
                pass
        self._tasks.clear()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    async def _janitor_loop(self) -> None:
        """Evict idle tasks so a long-running service doesn't leak RAM."""
        try:
            while True:
                await asyncio.sleep(120)
                now = time.time()
                stale = [
                    tid for tid, t in self._tasks.items()
                    if now - t.last_used > self.settings.task_idle_ttl_sec
                ]
                for tid in stale:
                    try:
                        await self.close_task(tid)
                    except Exception:
                        pass
        except asyncio.CancelledError:
            pass

    # ----- tasks -----

    def _check_host(self, url: str) -> None:
        host = (urlparse(url).hostname or "").lower()
        for blocked in self.settings.blocked_hosts:
            if blocked and blocked in host:
                raise BlockedHostError(f"host is blocked by operator policy: {host}")

    async def get_or_create_task(self, task_id: Optional[str] = None) -> Task:
        async with self._lock:
            if task_id and task_id in self._tasks:
                task = self._tasks[task_id]
                task.touch()
                return task
            if len(self._tasks) >= self.settings.max_tasks:
                raise BrowserError(
                    f"too many concurrent tasks (max {self.settings.max_tasks}); "
                    "close idle tasks first", code="too_many_tasks")
            new_id = task_id or f"task-{uuid.uuid4().hex[:12]}"
            if new_id in self._tasks:
                raise BrowserError(f"task already exists: {new_id}", code="task_exists")
            await self.ensure_started()  # lazy: first browser use launches Chromium
            assert self._browser is not None, "browser not started"
            context = await self._browser.new_context(
                user_agent=self.settings.user_agent,
                viewport={"width": 1366, "height": 900},
                accept_downloads=True,
            )
            context.set_default_timeout(self.settings.default_timeout_ms)
            context.set_default_navigation_timeout(self.settings.navigation_timeout_ms)
            task = Task(new_id, context)
            self._tasks[new_id] = task
            return task

    def get_task(self, task_id: str) -> Task:
        try:
            task = self._tasks[task_id]
        except KeyError:
            raise TaskNotFoundError(f"unknown task_id: {task_id}") from None
        task.touch()
        return task

    async def close_task(self, task_id: str) -> None:
        async with self._lock:
            task = self._tasks.pop(task_id, None)
        if task is None:
            raise TaskNotFoundError(f"unknown task_id: {task_id}")
        await task.context.close()

    def task_ids(self) -> List[str]:
        return sorted(self._tasks.keys())

    # ----- tabs -----

    async def new_tab(self, task: Task, url: Optional[str] = None) -> Dict[str, str]:
        if len(task.pages) >= self.settings.max_tabs_per_task:
            raise BrowserError(
                f"too many tabs in task (max {self.settings.max_tabs_per_task})",
                code="too_many_tabs")
        page = await task.context.new_page()
        tab_id = f"tab-{uuid.uuid4().hex[:8]}"
        task.pages[tab_id] = page
        task.active_tab = tab_id
        result = {"tab_id": tab_id}
        if url:
            self._check_host(url)
            await task.polite_wait(self.settings.min_request_interval_ms)
            try:
                resp = await page.goto(url, wait_until="domcontentloaded")
                result["status"] = resp.status if resp else None
            except PlaywrightTimeoutError as e:
                raise NavigationError(f"navigation timed out: {url}") from e
            except Exception as e:
                raise NavigationError(f"navigation failed: {e}") from e
        task.touch()
        return result

    def active_page(self, task: Task) -> Page:
        if not task.active_tab or task.active_tab not in task.pages:
            raise TabNotFoundError("task has no active tab — call new_tab first")
        return task.pages[task.active_tab]

    def get_page(self, task: Task, tab_id: str) -> Page:
        try:
            return task.pages[tab_id]
        except KeyError:
            raise TabNotFoundError(f"unknown tab_id: {tab_id}") from None

    def switch_tab(self, task: Task, tab_id: str) -> Page:
        page = self.get_page(task, tab_id)
        task.active_tab = tab_id
        task.touch()
        return page

    async def close_tab(self, task: Task, tab_id: str) -> List[str]:
        page = self.get_page(task, tab_id)
        await page.close()
        del task.pages[tab_id]
        if task.active_tab == tab_id:
            task.active_tab = next(iter(task.pages), None)
        task.touch()
        return sorted(task.pages.keys())
