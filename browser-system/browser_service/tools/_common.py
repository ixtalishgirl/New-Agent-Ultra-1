"""Shared helpers for tool modules.

- ``check_robots(url)``: best-effort robots.txt check (urllib.robotparser).
  Respected when BROWSER_RESPECT_ROBOTS=1 (default). Network failures while
  fetching robots.txt are treated as "allowed" (fail-open for reading).
- Page content is DATA: never executed, never treated as instructions.
"""
from __future__ import annotations

import time
import urllib.robotparser
from urllib.parse import urlparse, urlunparse

import httpx

_robots_cache: dict[str, tuple[float, urllib.robotparser.RobotFileParser]] = {}
_ROBOTS_TTL = 3600


def _robots_url(url: str) -> str:
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, "/robots.txt", "", "", ""))


def _get_parser(robots_url: str) -> urllib.robotparser.RobotFileParser | None:
    now = time.time()
    cached = _robots_cache.get(robots_url)
    if cached and now - cached[0] < _ROBOTS_TTL:
        return cached[1]
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(robots_url)
    try:
        with httpx.Client(timeout=10, follow_redirects=True,
                          headers={"User-Agent": "ahsanAgentBrowser/1.0"}) as client:
            resp = client.get(robots_url)
            if resp.status_code in (401, 403):
                # Can't read robots.txt -> treat as disallowed (conservative).
                rp.disallow_all = True
            elif resp.status_code >= 400:
                # No usable robots.txt -> allow (standard behaviour).
                rp.allow_all = True
            else:
                rp.parse(resp.text.splitlines())
    except Exception:
        rp.allow_all = True  # fail-open on network errors
    _robots_cache[robots_url] = (now, rp)
    return rp


def robots_allowed(url: str, user_agent: str = "*") -> tuple[bool, str]:
    """Return (allowed, robots_url_or_reason)."""
    try:
        parser = _get_parser(_robots_url(url))
        if parser is None:
            return True, "no robots.txt"
        allowed = parser.can_fetch(user_agent, url)
        return allowed, _robots_url(url)
    except Exception as e:  # never let robots checking break a tool
        return True, f"robots check failed ({e}); allowing"


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        raise ValueError("empty url")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme: {parsed.scheme or '(none)'} "
                         "(only http/https are allowed)")
    return url
