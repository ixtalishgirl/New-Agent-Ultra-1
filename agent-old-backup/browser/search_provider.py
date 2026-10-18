"""
Web search provider — interchangeable public search.
Uses DuckDuckGo HTML (no API key needed).
"""
from __future__ import annotations
import re
import urllib.parse
import urllib.request


class SearchProvider:
    """Public web search via DuckDuckGo."""

    def __init__(self, timeout: int = 15):
        self.timeout = timeout

    def search(self, query: str, max_results: int = 5) -> list[dict]:
        """Search and return [{title, url, snippet}]."""
        q = urllib.parse.quote(query)
        url = f"https://html.duckduckgo.com/html/?q={q}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
        except Exception as e:
            return [{"title": "", "url": "", "snippet": f"Search failed: {e}"}]

        results = []
        # Parse DDG html results
        for m in re.finditer(
            r'<a rel="nofollow" class="result__a" href="([^"]+)">(.*?)</a>.*?'
            r'<a class="result__snippet"[^>]*>(.*?)</a>',
            html, re.DOTALL,
        ):
            raw_url, title, snippet = m.groups()
            # DDG wraps URLs in redirect; extract actual
            actual = raw_url
            uddg = re.search(r"uddg=([^&]+)", raw_url)
            if uddg:
                actual = urllib.parse.unquote(uddg.group(1))
            results.append({
                "title": re.sub(r"<[^>]+>", "", title).strip(),
                "url": actual,
                "snippet": re.sub(r"<[^>]+>", "", snippet).strip(),
            })
            if len(results) >= max_results:
                break
        return results
