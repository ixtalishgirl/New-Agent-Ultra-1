"""
Page extractor — fetch public pages, extract title/URL/text.
Treats all web content as UNTRUSTED data (never executes instructions from pages).
"""
from __future__ import annotations
import re
import urllib.request


class PageExtractor:
    def __init__(self, timeout: int = 20, max_chars: int = 20000):
        self.timeout = timeout
        self.max_chars = max_chars

    def extract(self, url: str) -> dict:
        """Fetch URL, return {url, title, text, error}."""
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
        except Exception as e:
            return {"url": url, "title": "", "text": "", "error": str(e)}

        # Title
        title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
        title = re.sub(r"<[^>]+>", "", title_m.group(1)).strip() if title_m else ""

        # Strip scripts/styles, extract text
        text_html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html,
                           flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text_html)
        text = re.sub(r"\s+", " ", text).strip()[:self.max_chars]

        return {"url": url, "title": title, "text": text, "error": ""}
