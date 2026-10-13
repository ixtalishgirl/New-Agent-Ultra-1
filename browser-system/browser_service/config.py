"""Central configuration for the browser service.

All settings come from environment variables so the same image can run
on ahsan's PC, a VPS, or the Germany VPS without code changes.
See .env.example for the full list.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    # --- service ---
    host: str = field(default_factory=lambda: os.environ.get("BROWSER_SERVICE_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("BROWSER_SERVICE_PORT", 8080))
    api_key: str | None = field(default_factory=lambda: os.environ.get("BROWSER_SERVICE_API_KEY") or None)

    # --- browser behaviour ---
    headless: bool = field(default_factory=lambda: _env_bool("BROWSER_HEADLESS", True))
    slow_mo_ms: int = field(default_factory=lambda: _env_int("BROWSER_SLOW_MO_MS", 0))
    default_timeout_ms: int = field(default_factory=lambda: _env_int("BROWSER_TIMEOUT_MS", 30000))
    navigation_timeout_ms: int = field(default_factory=lambda: _env_int("BROWSER_NAV_TIMEOUT_MS", 45000))
    max_tabs_per_task: int = field(default_factory=lambda: _env_int("BROWSER_MAX_TABS", 5))
    max_tasks: int = field(default_factory=lambda: _env_int("BROWSER_MAX_TASKS", 20))
    task_idle_ttl_sec: int = field(default_factory=lambda: _env_int("BROWSER_TASK_IDLE_TTL_SEC", 1800))

    # --- proxy (Germany routing etc.) ---
    # Honor plain HTTP(S)_PROXY too as a convenience fallback.
    http_proxy: str | None = field(
        default_factory=lambda: os.environ.get("BROWSER_HTTP_PROXY") or os.environ.get("HTTP_PROXY")
    )
    https_proxy: str | None = field(
        default_factory=lambda: os.environ.get("BROWSER_HTTPS_PROXY") or os.environ.get("HTTPS_PROXY")
    )
    proxy_username: str | None = field(default_factory=lambda: os.environ.get("BROWSER_PROXY_USERNAME") or None)
    proxy_password: str | None = field(default_factory=lambda: os.environ.get("BROWSER_PROXY_PASSWORD") or None)

    # --- downloads ---
    download_dir: str = field(default_factory=lambda: os.environ.get("BROWSER_DOWNLOAD_DIR", "./downloads"))
    max_download_mb: int = field(default_factory=lambda: _env_int("BROWSER_MAX_DOWNLOAD_MB", 100))
    allowed_download_exts: tuple = (
        ".pdf", ".zip", ".csv", ".json", ".txt", ".md", ".png", ".jpg", ".jpeg",
        ".gif", ".webp", ".svg", ".mp3", ".mp4", ".docx", ".xlsx", ".pptx", ".xml",
    )

    # --- politeness / safety ---
    respect_robots: bool = field(default_factory=lambda: _env_bool("BROWSER_RESPECT_ROBOTS", True))
    min_request_interval_ms: int = field(default_factory=lambda: _env_int("BROWSER_MIN_REQUEST_INTERVAL_MS", 1500))
    user_agent: str = field(
        default_factory=lambda: os.environ.get(
            "BROWSER_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 ahsanAgentBrowser/1.0",
        )
    )
    # Comma-separated host substrings that must never be visited (e.g. internal infra).
    blocked_hosts: tuple = field(
        default_factory=lambda: tuple(
            h.strip().lower()
            for h in os.environ.get("BROWSER_BLOCKED_HOSTS", "").split(",")
            if h.strip()
        )
    )

    # --- screenshots ---
    screenshot_max_kb: int = field(default_factory=lambda: _env_int("BROWSER_SCREENSHOT_MAX_KB", 2048))

    # --- research history (SQLite, optional) ---
    enable_history: bool = field(default_factory=lambda: _env_bool("BROWSER_ENABLE_HISTORY", False))
    history_db_path: str = field(default_factory=lambda: os.environ.get("BROWSER_HISTORY_DB", "./history.db"))

    def playwright_proxy(self) -> dict | None:
        """Build the Playwright proxy dict from env, or None for direct."""
        server = self.https_proxy or self.http_proxy
        if not server:
            return None
        proxy: dict = {"server": server}
        if self.proxy_username:
            proxy["username"] = self.proxy_username
        if self.proxy_password:
            proxy["password"] = self.proxy_password
        return proxy


settings = Settings()
