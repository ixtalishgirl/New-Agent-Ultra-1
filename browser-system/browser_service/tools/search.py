"""Tool: search — free web search without any API key.

Uses DuckDuckGo's public HTML endpoints (html.duckduckgo.com / lite.duckduckgo.com)
via httpx + BeautifulSoup. No key, no account. Honors BROWSER_HTTP(S)_PROXY.
"""
from __future__ import annotations

from typing import Any, Dict, List
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

from browser_manager import BrowserError
from registry import ToolContext, ToolRegistry
from ._common import client_kwargs as _common_client_kwargs

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string", "description": "Optional: attach results to a task's history."},
        "query": {"type": "string", "description": "Search query."},
        "engine": {"type": "string", "enum": ["duckduckgo", "duckduckgo-lite"],
                   "default": "duckduckgo"},
        "max_results": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10},
    },
    "required": ["query"],
}

_ENGINES = {
    "duckduckgo": "https://html.duckduckgo.com/html/?q={q}",
    "duckduckgo-lite": "https://lite.duckduckgo.com/lite/?q={q}",
}


def _proxies(ctx: ToolContext) -> Dict[str, str] | None:
    s = ctx.settings
    proxy = s.https_proxy or s.http_proxy
    if not proxy:
        return None
    return {"http://": proxy, "https://": proxy}


def _parse_ddg(html: str, max_results: int) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    out: List[Dict[str, str]] = []
    for res in soup.select(".result"):
        a = res.select_one("a.result__a")
        if not a:
            continue
        snippet_el = res.select_one(".result__snippet")
        out.append({
            "title": a.get_text(strip=True),
            "url": a.get("href", ""),
            "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
        })
        if len(out) >= max_results:
            break
    return out


def _parse_ddg_lite(html: str, max_results: int) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    out: List[Dict[str, str]] = []
    for a in soup.select("a.result-link"):
        parent = a.find_parent("td") or a.find_parent("tr")
        snippet = ""
        if parent:
            sib = parent.find_next_sibling("td")
            if sib:
                snippet = sib.get_text(" ", strip=True)
        out.append({
            "title": a.get_text(strip=True),
            "url": a.get("href", ""),
            "snippet": snippet,
        })
        if len(out) >= max_results:
            break
    return out


async def _handle(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    query = args["query"].strip()
    if not query:
        raise BrowserError("empty search query", code="invalid_argument")
    engine = args.get("engine", "duckduckgo")
    max_results = int(args.get("max_results", 10))

    url = _ENGINES[engine].format(q=quote_plus(query))
    await ctx.emit(args.get("task_id") or "search",
                   {"event": "search_start", "engine": engine, "query": query})
    try:
        async with httpx.AsyncClient(
            **_common_client_kwargs(_proxies(ctx)),
            timeout=30, follow_redirects=True, trust_env=False,
            headers={"User-Agent": ctx.settings.user_agent},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except httpx.HTTPError as e:
        raise BrowserError(f"search request failed: {e}", code="search_failed") from e

    results = (_parse_ddg if engine == "duckduckgo" else _parse_ddg_lite)(
        resp.text, max_results)
    await ctx.emit(args.get("task_id") or "search",
                   {"event": "search_done", "count": len(results)})

    if ctx.history and args.get("task_id"):
        ctx.history.record(args["task_id"], url, f"search: {query}",
                           f"{len(results)} results via {engine}")

    return {"engine": engine, "query": query, "count": len(results), "results": results}


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="search",
        description="Free web search (DuckDuckGo HTML endpoints, no API key). "
                    "Returns title/url/snippet results. Honors proxy env vars.",
        schema=SCHEMA,
        handler=_handle,
        tags=["search", "no-browser-needed"],
    )
