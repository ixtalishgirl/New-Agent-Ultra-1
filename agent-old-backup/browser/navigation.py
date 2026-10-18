"""Navigation planning — URL normalization, sitemap discovery, pagination,
breadcrumb extraction, and a polite BFS crawl frontier.

Stdlib only (urllib.parse, urllib.robotparser, collections.deque).
All fetched content is UNTRUSTED data.
"""
from __future__ import annotations

import re
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from collections import deque
from dataclasses import dataclass, field
from html.parser import HTMLParser


# ---------------------------------------------------------------------------
# URL canonicalization / normalization
# ---------------------------------------------------------------------------

_DEFAULT_PORTS = {"http": 80, "https": 443, "ftp": 21}

_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "gclid", "gbraid", "wbraid", "fbclid", "msclkid", "mc_cid",
    "mc_eid", "_ga", "_gid", "yclid", "dclid", "igshid", "ref", "ref_src",
})


def url_canonicalize(url: str, strip_tracking: bool = True,
                     keep_fragment: bool = False) -> str:
    """Normalize a URL for dedup and comparison.

    - lowercase scheme and host
    - drop default ports (:80, :443)
    - collapse dot-segments, remove trailing slash (except root)
    - sort query parameters; optionally strip tracking params
    - optionally drop fragments
    - unquote-then-requote the path consistently
    """
    url = (url or "").strip()
    if not url:
        return ""
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower() or "http"
    host = parts.hostname or ""
    host = host.lower()
    # IDNA for international domains.
    try:
        host = host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        pass
    port = parts.port
    if port and _DEFAULT_PORTS.get(scheme) == port:
        port = None
    netloc = host + (f":{port}" if port else "")
    if parts.username:
        userinfo = parts.username
        if parts.password:
            userinfo += f":{parts.password}"
        netloc = f"{userinfo}@{netloc}"

    path = urllib.parse.unquote(parts.path or "/")
    # Collapse dot segments.
    segs: list[str] = []
    for seg in path.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if segs:
                segs.pop()
            continue
        segs.append(seg)
    path = "/" + "/".join(segs)
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    path = urllib.parse.quote(path, safe="/:@!$&'()*+,;=%")

    query_pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    if strip_tracking:
        query_pairs = [(k, v) for k, v in query_pairs
                       if k.lower() not in _TRACKING_PARAMS]
    query_pairs.sort(key=lambda kv: (kv[0], kv[1]))
    query = urllib.parse.urlencode(query_pairs)

    fragment = parts.fragment if keep_fragment else ""
    out = urllib.parse.urlunsplit((scheme, netloc, path, query, fragment))
    return out


def url_fingerprint(url: str) -> str:
    """Canonical URL minus query/fragment — coarse page identity."""
    c = url_canonicalize(url, strip_tracking=True, keep_fragment=False)
    return c.split("?", 1)[0]


def same_site(url_a: str, url_b: str) -> bool:
    """True when both URLs share the registrable host (or exact host)."""
    ha = (urllib.parse.urlsplit(url_a).hostname or "").lower()
    hb = (urllib.parse.urlsplit(url_b).hostname or "").lower()
    return bool(ha) and ha == hb


def parent_url(url: str) -> str:
    """URL of the parent path segment."""
    parts = urllib.parse.urlsplit(url)
    path = parts.path.rstrip("/")
    parent = path.rsplit("/", 1)[0] or "/"
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parent, "", ""))


# ---------------------------------------------------------------------------
# Breadcrumb extraction
# ---------------------------------------------------------------------------

class _BreadcrumbParser(HTMLParser):
    """Extract breadcrumbs from nav[aria-label=breadcrumb], .breadcrumb
    containers, and schema.org BreadcrumbList JSON-LD."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.crumbs: list[dict] = []
        self._in_crumb_nav = 0
        self._in_link = False
        self._link_href = ""
        self._link_text: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        cls = (a.get("class", "") or "").lower()
        aria = (a.get("aria-label", "") or "").lower()
        itemtype = (a.get("itemtype", "") or "").lower()
        if tag in ("nav", "ol", "ul", "div"):
            self._depth += 1
            if ("breadcrumb" in cls or "breadcrumb" in aria
                    or "breadcrumblist" in itemtype):
                self._in_crumb_nav += 1
        if self._in_crumb_nav and tag == "a":
            self._in_link = True
            self._link_href = a.get("href", "") or ""
            self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("nav", "ol", "ul", "div"):
            if self._in_crumb_nav:
                self._in_crumb_nav = max(0, self._in_crumb_nav - 1)
            self._depth = max(0, self._depth - 1)
        if tag == "a" and self._in_link:
            text = re.sub(r"\s+", " ", "".join(self._link_text)).strip()
            if text:
                self.crumbs.append({"text": text, "url": self._link_href})
            self._in_link = False

    def handle_data(self, data: str) -> None:
        if self._in_link:
            self._link_text.append(data)


def breadcrumb_extract(html: str, base_url: str = "") -> list[dict]:
    """Return breadcrumb trail [{text, url}] from HTML.

    Sources tried in order: nav/ol breadcrumb containers, then
    schema.org BreadcrumbList JSON-LD. URLs resolved against base_url.
    """
    parser = _BreadcrumbParser()
    parser.feed(html or "")
    parser.close()
    crumbs = parser.crumbs

    if not crumbs:
        # JSON-LD fallback.
        for m in re.finditer(
                r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
                html or "", re.DOTALL | re.IGNORECASE):
            try:
                import json as _json
                data = _json.loads(m.group(1))
            except (ValueError, ImportError):
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                if "breadcrumblist" not in str(item.get("@type", "")).lower():
                    continue
                elements = item.get("itemListElement", []) or []
                for el in sorted(elements,
                                 key=lambda e: e.get("position", 0)
                                 if isinstance(e, dict) else 0):
                    if not isinstance(el, dict):
                        continue
                    inner = el.get("item", {})
                    if isinstance(inner, dict):
                        crumbs.append({"text": inner.get("name", ""),
                                       "url": inner.get("@id", "")})
                    elif isinstance(inner, str):
                        crumbs.append({"text": el.get("name", ""),
                                       "url": inner})
    out = []
    seen = set()
    for c in crumbs:
        text = c.get("text", "").strip()
        url = urllib.parse.urljoin(base_url, c.get("url", "") or "")
        if text and (text, url) not in seen:
            seen.add((text, url))
            out.append({"text": text, "url": url})
    return out


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

_PAGE_PARAM_RES = [
    re.compile(r"([?&])(page|p|pg|pagina)=(\d+)", re.I),
    re.compile(r"/page/(\d+)/?", re.I),
    re.compile(r"/p(\d+)/?$", re.I),
    re.compile(r"[?&]offset=(\d+)", re.I),
]

_NEXT_LINK_RES = [
    re.compile(r"\bnext\b", re.I),
    re.compile(r"\bolder\b", re.I),
    re.compile(r"»|›|→"),
]


def detect_pagination(html: str, current_url: str) -> dict:
    """Detect pagination controls on a page.

    Returns {"style": "query"|"path"|"offset"|"link"|"none",
             "current_page": int|None, "next_url": str|None,
             "total_pages": int|None}.
    """
    result: dict = {"style": "none", "current_page": None,
                    "next_url": None, "total_pages": None}

    # 1) Query/path style from the current URL.
    m = _PAGE_PARAM_RES[0].search(current_url)
    if m:
        result.update(style="query", current_page=int(m.group(3)))
        return result
    m = _PAGE_PARAM_RES[1].search(current_url)
    if m:
        result.update(style="path", current_page=int(m.group(1)))
        return result
    m = _PAGE_PARAM_RES[3].search(current_url)
    if m:
        result.update(style="offset", current_page=int(m.group(1)))
        return result

    # 2) rel="next" link.
    m = re.search(r'<a[^>]+rel="next"[^>]+href="([^"]+)"',
                  html or "", re.IGNORECASE)
    if m:
        result.update(style="link",
                      next_url=urllib.parse.urljoin(current_url, m.group(1)))
        return result

    # 3) Anchor whose text looks like "next".
    for am in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                          html or "", re.DOTALL | re.IGNORECASE):
        text = re.sub(r"<[^>]+>", "", am.group(2))
        if any(rx.search(text) for rx in _NEXT_LINK_RES):
            result.update(style="link",
                          next_url=urllib.parse.urljoin(current_url, am.group(1)))
            return result

    # 4) Numbered page links — infer total.
    pages = set()
    for am in re.finditer(r'<a[^>]+href="([^"]*(?:page=\d+|/page/\d+)[^"]*)"[^>]*>'
                          r"\s*(\d+)\s*</a>", html or "", re.IGNORECASE):
        try:
            pages.add(int(am.group(2)))
        except ValueError:
            pass
    if pages:
        result.update(style="link", total_pages=max(pages))
    return result


def generate_page_urls(base_url: str, style: str,
                       start: int = 1, end: int = 10,
                       page_size: int | None = None) -> list[str]:
    """Generate paginated URLs for query/path/offset styles."""
    urls: list[str] = []
    for n in range(start, end + 1):
        if style == "query":
            urls.append(_set_query_param(base_url, "page", str(n)))
        elif style == "path":
            base = re.sub(r"/page/\d+/?$", "", base_url.rstrip("/"))
            urls.append(f"{base}/page/{n}/")
        elif style == "offset" and page_size:
            urls.append(_set_query_param(base_url, "offset",
                                         str((n - 1) * page_size)))
        elif style == "offset":
            urls.append(_set_query_param(base_url, "offset", str(n)))
    return urls


def _set_query_param(url: str, key: str, value: str) -> str:
    parts = urllib.parse.urlsplit(url)
    pairs = [(k, v) for k, v in
             urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() != key.lower()]
    pairs.append((key, value))
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path,
         urllib.parse.urlencode(pairs), parts.fragment))


# ---------------------------------------------------------------------------
# Sitemap discovery (robots.txt + common paths)
# ---------------------------------------------------------------------------

_SITEMAP_PATHS = [
    "/sitemap.xml", "/sitemap_index.xml", "/sitemap/sitemap.xml",
    "/sitemaps/sitemap.xml", "/wp-sitemap.xml", "/sitemap-index.xml",
]

_ROBOTS_PATH = "/robots.txt"


def _fetch_text(url: str, timeout: int = 15) -> str | None:
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; AgentBot/1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if "xml" not in ctype and "text" not in ctype:
                return None
            return resp.read(2_000_000).decode("utf-8", errors="ignore")
    except Exception:
        return None


def _sitemap_urls_from_xml(xml_text: str) -> tuple[list[str], list[str]]:
    """Return (page_urls, child_sitemap_urls) from a sitemap XML doc."""
    locs = re.findall(r"<loc>\s*([^<>\s]+)\s*</loc>", xml_text or "")
    if "<sitemapindex" in (xml_text or "")[:2000].lower():
        return [], locs
    return locs, []


def discover_sitemaps(base_url: str, timeout: int = 15,
                      max_urls: int = 5000) -> dict:
    """Discover sitemaps via robots.txt Sitemap: lines + common paths.

    Returns {"sitemaps": [...], "urls": [...], "source": "robots"|"guess"}.
    Follows one level of sitemap indexes.
    """
    parts = urllib.parse.urlsplit(base_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    sitemap_urls: list[str] = []
    source = "guess"

    robots = _fetch_text(origin + _ROBOTS_PATH, timeout)
    if robots:
        for line in robots.splitlines():
            m = re.match(r"(?i)sitemap:\s*(\S+)", line.strip())
            if m:
                sitemap_urls.append(m.group(1).strip())
                source = "robots"

    if not sitemap_urls:
        for path in _SITEMAP_PATHS:
            candidate = origin + path
            if _fetch_text(candidate, timeout):
                sitemap_urls.append(candidate)
                break

    all_pages: list[str] = []
    for sm in sitemap_urls[:5]:
        xml_text = _fetch_text(sm, timeout)
        if not xml_text:
            continue
        pages, children = _sitemap_urls_from_xml(xml_text)
        all_pages.extend(pages)
        for child in children[:10]:
            child_xml = _fetch_text(child, timeout)
            if child_xml:
                sub_pages, _ = _sitemap_urls_from_xml(child_xml)
                all_pages.extend(sub_pages)
        if len(all_pages) >= max_urls:
            break

    # Dedup preserving order.
    seen: set[str] = set()
    uniq: list[str] = []
    for u in all_pages:
        cu = url_canonicalize(u)
        if cu not in seen:
            seen.add(cu)
            uniq.append(cu)
    return {"sitemaps": sitemap_urls, "urls": uniq[:max_urls],
            "source": source}


def robots_allows(user_agent: str, url: str,
                  robots_txt: str | None = None) -> bool:
    """Check robots.txt permission for a URL.

    When robots_txt is None it is fetched from the site's /robots.txt.
    Fetch failures default to allowed (fail-open, like most crawlers).
    """
    rp = urllib.robotparser.RobotFileParser()
    if robots_txt is None:
        parts = urllib.parse.urlsplit(url)
        rp.set_url(f"{parts.scheme}://{parts.netloc}{_ROBOTS_PATH}")
        try:
            rp.read()
        except Exception:
            return True
    else:
        rp.parse(robots_txt.splitlines())
    try:
        return bool(rp.can_fetch(user_agent, url))
    except Exception:
        return True


def crawl_delay_for(user_agent: str, robots_txt: str) -> float | None:
    """Extract Crawl-delay for our user agent from robots.txt text."""
    current_ua: str | None = None
    wildcard_delay: float | None = None
    for line in robots_txt.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            current_ua = value
        elif key == "crawl-delay":
            try:
                delay = float(value)
            except ValueError:
                continue
            if current_ua in (user_agent, "*") and wildcard_delay is None:
                if current_ua == "*":
                    wildcard_delay = delay
                else:
                    return delay
    return wildcard_delay


# ---------------------------------------------------------------------------
# Crawl frontier — polite BFS URL queue
# ---------------------------------------------------------------------------

@dataclass
class CrawlConfig:
    max_depth: int = 2
    max_pages: int = 100
    politeness_delay: float = 1.0
    same_host_only: bool = True
    respect_robots: bool = True
    user_agent: str = "Mozilla/5.0 (compatible; AgentBot/1.0)"
    allow_query_urls: bool = True
    exclude_patterns: list[str] = field(default_factory=list)


@dataclass
class CrawlItem:
    url: str
    depth: int
    parent: str = ""
    discovered_at: float = field(default_factory=time.time)


class CrawlFrontier:
    """BFS crawl queue with depth limit, dedup, and politeness delay.

    The caller drives the loop: pop() -> fetch page -> push() new links.
    """

    def __init__(self, seeds: list[str], config: CrawlConfig | None = None):
        self.config = config or CrawlConfig()
        self.queue: deque[CrawlItem] = deque()
        self.seen: set[str] = set()      # canonical URLs already queued
        self.visited: set[str] = set()   # fingerprints already popped
        self._last_fetch: dict[str, float] = {}  # host -> timestamp
        self._robots_cache: dict[str, str | None] = {}
        self._exclude_res = [re.compile(p, re.I)
                             for p in self.config.exclude_patterns]
        for seed in seeds:
            self.push(seed, depth=0)

    # -- queue ops ---------------------------------------------------------
    def push(self, url: str, depth: int = 0, parent: str = "") -> bool:
        """Queue a URL. Returns False when rejected (dup/depth/filter)."""
        if depth > self.config.max_depth:
            return False
        canon = url_canonicalize(url)
        if not canon or canon in self.seen:
            return False
        if not self._allowed_by_filters(canon):
            return False
        self.seen.add(canon)
        self.queue.append(CrawlItem(url=canon, depth=depth, parent=parent))
        return True

    def push_many(self, urls: list[str], depth: int,
                  parent: str = "") -> int:
        """Queue many URLs; returns the number accepted."""
        return sum(1 for u in urls if self.push(u, depth=depth, parent=parent))

    def pop(self) -> CrawlItem | None:
        """Take the next URL, honoring the per-host politeness delay.

        Sleeps if needed so hosts are not hammered; returns None when
        the queue is empty or max_pages was reached.
        """
        if not self.queue or len(self.visited) >= self.config.max_pages:
            return None
        item = self.queue[0]
        host = urllib.parse.urlsplit(item.url).hostname or ""
        now = time.time()
        wait = (self._last_fetch.get(host, 0.0)
                + self.config.politeness_delay - now)
        if wait > 0:
            time.sleep(wait)
        self.queue.popleft()
        fingerprint = url_fingerprint(item.url)
        if fingerprint in self.visited:
            return self.pop()  # already crawled under another URL form
        self.visited.add(fingerprint)
        self._last_fetch[host] = time.time()
        return item

    def __len__(self) -> int:
        return len(self.queue)

    @property
    def done(self) -> bool:
        """True when the queue is drained or max_pages reached."""
        return (not self.queue
                or len(self.visited) >= self.config.max_pages)

    def stats(self) -> dict:
        return {"queued": len(self.queue), "seen": len(self.seen),
                "visited": len(self.visited),
                "max_pages": self.config.max_pages}

    # -- filters -----------------------------------------------------------
    def _allowed_by_filters(self, canon_url: str) -> bool:
        cfg = self.config
        parts = urllib.parse.urlsplit(canon_url)
        if parts.scheme not in ("http", "https"):
            return False
        if not cfg.allow_query_urls and parts.query:
            return False
        for rx in self._exclude_res:
            if rx.search(canon_url):
                return False
        if cfg.same_host_only and self.seen:
            # Compare against the first seed's host.
            first = next(iter(self.seen))
            if not same_site(first, canon_url):
                return False
        if cfg.respect_robots and not self._robots_ok(canon_url):
            return False
        return True

    def _robots_ok(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots_cache:
            self._robots_cache[origin] = _fetch_text(origin + _ROBOTS_PATH)
        robots_txt = self._robots_cache[origin]
        if not robots_txt:
            return True
        return robots_allows(self.config.user_agent, url, robots_txt)


# ---------------------------------------------------------------------------
# Navigation planning
# ---------------------------------------------------------------------------

@dataclass
class NavStep:
    action: str  # "goto" | "click" | "submit" | "scroll"
    target: str
    detail: str = ""


class NavigationPlanner:
    """Plan how to get from a start page to a target page/element.

    Strategies, in order:
    1. direct URL — target is a URL we can navigate to;
    2. sitemap route — target path exists in the sitemap URL list;
    3. search route — suggest a site: search;
    4. crawl route — BFS from the start page until the target is found.
    """

    def __init__(self, frontier_config: CrawlConfig | None = None):
        self.frontier_config = frontier_config or CrawlConfig()

    def plan_navigation(self, start_url: str, target: str,
                        sitemap_urls: list[str] | None = None,
                        known_links: dict[str, list[str]] | None = None
                        ) -> list[NavStep]:
        """Return an ordered list of NavSteps from start_url to target."""
        target = target.strip()
        steps: list[NavStep] = []

        # Strategy 1: target looks like a URL.
        if re.match(r"^https?://", target, re.I) or target.startswith("/"):
            absolute = (target if re.match(r"^https?://", target, re.I)
                        else urllib.parse.urljoin(start_url, target))
            steps.append(NavStep(action="goto", target=absolute,
                                 detail="direct navigation to target URL"))
            return steps

        # Strategy 2: target path appears in the sitemap.
        if sitemap_urls:
            needle = target.lower().strip("/")
            for sm_url in sitemap_urls:
                path = urllib.parse.urlsplit(sm_url).path.lower().strip("/")
                if needle and (needle in path or path.endswith(needle)):
                    steps.append(NavStep(action="goto", target=sm_url,
                                         detail="target found in sitemap"))
                    return steps

        # Strategy 3: known link graph — BFS over provided adjacency.
        if known_links:
            route = self._bfs_route(start_url, target, known_links)
            if route:
                for url in route[1:]:
                    steps.append(NavStep(action="goto", target=url,
                                         detail="link-graph hop"))
                return steps

        # Strategy 4: fall back to a site search suggestion.
        host = urllib.parse.urlsplit(start_url).netloc
        steps.append(NavStep(
            action="search",
            target=f"site:{host} {target}" if host else target,
            detail="target not directly reachable; search the site"))
        return steps

    def _bfs_route(self, start: str, target: str,
                   graph: dict[str, list[str]]) -> list[str] | None:
        """BFS over a caller-provided link graph; match by URL or slug."""
        start_c = url_canonicalize(start)
        needle = target.lower().strip("/")
        queue: deque[list[str]] = deque([[start_c]])
        seen = {start_c}
        while queue:
            path = queue.popleft()
            node = path[-1]
            node_slug = urllib.parse.urlsplit(node).path.lower().strip("/")
            if needle and (needle in node.lower() or needle in node_slug
                           or node_slug.endswith(needle)):
                return path
            for nxt in graph.get(node, []) or graph.get(path[-1], []):
                c = url_canonicalize(nxt)
                if c not in seen:
                    seen.add(c)
                    queue.append(path + [c])
        return None

    def plan_to_element(self, page_url: str, element_hint: str) -> list[NavStep]:
        """Plan steps to reach an element described in words.

        Returns goto + scroll/click steps the executor can refine with
        the live page (e.g. via Playwright selectors).
        """
        return [
            NavStep(action="goto", target=page_url,
                    detail="load the page containing the element"),
            NavStep(action="scroll", target=element_hint,
                    detail=f"scroll until '{element_hint}' is visible"),
            NavStep(action="click", target=element_hint,
                    detail=f"click '{element_hint}' if it is actionable"),
        ]

    def paginate_plan(self, first_page_url: str, total_pages: int,
                      style: str = "query") -> list[NavStep]:
        """Steps to walk every page of a paginated listing."""
        steps = [NavStep(action="goto", target=first_page_url,
                         detail="page 1 of listing")]
        for url in generate_page_urls(first_page_url, style,
                                      start=2, end=total_pages):
            steps.append(NavStep(action="goto", target=url,
                                 detail=f"next listing page"))
        return steps


# ---------------------------------------------------------------------------
# Link-graph helpers
# ---------------------------------------------------------------------------

def extract_page_links(html: str, base_url: str) -> list[str]:
    """All unique absolute http(s) links on a page, canonicalized."""
    found: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r'<a[^>]+href="([^"]+)"', html or "",
                         re.IGNORECASE):
        raw = m.group(1).strip()
        if not raw or raw.startswith(("#", "mailto:", "javascript:",
                                      "tel:")):
            continue
        abs_url = urllib.parse.urljoin(base_url, raw)
        if urllib.parse.urlsplit(abs_url).scheme not in ("http", "https"):
            continue
        canon = url_canonicalize(abs_url)
        if canon not in seen:
            seen.add(canon)
            found.append(canon)
    return found


def filter_links(links: list[str], include: list[str] | None = None,
                 exclude: list[str] | None = None) -> list[str]:
    """Keep links matching include patterns and not matching exclude ones."""
    inc = [re.compile(p, re.I) for p in (include or [])]
    exc = [re.compile(p, re.I) for p in (exclude or [])]
    out = []
    for link in links:
        if inc and not any(rx.search(link) for rx in inc):
            continue
        if any(rx.search(link) for rx in exc):
            continue
        out.append(link)
    return out


def learn_url_templates(urls: list[str]) -> list[dict]:
    """Learn URL templates from a set of observed URLs.

    Replaces numeric path segments with {id}, date segments with {date},
    and slug-ish segments with {slug}. Returns
    [{template, example, count}] sorted by count.
    """
    templates: dict[str, dict] = {}
    for url in urls:
        parts = urllib.parse.urlsplit(url)
        segs = []
        for seg in parts.path.strip("/").split("/"):
            if re.fullmatch(r"\d{4}/\d{2}/\d{2}", seg) or re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}", seg):
                segs.append("{date}")
            elif re.fullmatch(r"\d+", seg):
                segs.append("{id}")
            elif re.fullmatch(r"[0-9a-f]{8,64}", seg):
                segs.append("{hash}")
            elif re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)+", seg, re.I):
                segs.append("{slug}")
            else:
                segs.append(seg)
        template = f"{parts.scheme}://{parts.netloc}/" + "/".join(segs)
        entry = templates.setdefault(
            template, {"template": template, "example": url, "count": 0})
        entry["count"] += 1
    return sorted(templates.values(), key=lambda e: e["count"], reverse=True)


def build_site_tree(urls: list[str]) -> dict:
    """Build a nested path tree from a list of URLs.

    Returns {"<host>": {"<segment>": {...}, "_urls": [...]}}.
    """
    tree: dict = {}
    for url in urls:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        node = tree.setdefault(host, {})
        for seg in parts.path.strip("/").split("/"):
            if not seg:
                continue
            node = node.setdefault(seg, {})
        node.setdefault("_urls", []).append(url)
    return tree


def flatten_site_tree(tree: dict, prefix: str = "") -> list[str]:
    """Flatten a site tree back into path strings (for inspection)."""
    out: list[str] = []
    for key, node in tree.items():
        if key == "_urls":
            continue
        path = f"{prefix}/{key}"
        out.append(path)
        if isinstance(node, dict):
            out.extend(flatten_site_tree(node, path))
    return out


def path_depth(url: str) -> int:
    """Number of path segments in a URL."""
    path = urllib.parse.urlsplit(url).path.strip("/")
    return 0 if not path else len(path.split("/"))


def sort_by_depth(urls: list[str], shallow_first: bool = True) -> list[str]:
    """Sort URLs by path depth (shallow pages usually more important)."""
    return sorted(urls, key=lambda u: (path_depth(u), u),
                  reverse=not shallow_first)


@dataclass
class RedirectHop:
    url: str
    status: int


def trace_redirects(url: str, timeout: int = 15,
                    max_hops: int = 10) -> list[RedirectHop]:
    """Follow redirects manually, recording each hop.

    Uses a no-redirect opener so every hop is visible.
    """
    import http.client

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers,
                             newurl):  # noqa: ANN001, ANN202
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    hops: list[RedirectHop] = []
    current = url
    for _ in range(max_hops):
        req = urllib.request.Request(
            current, headers={"User-Agent": "Mozilla/5.0"},
            method="HEAD")
        try:
            with opener.open(req, timeout=timeout) as resp:
                hops.append(RedirectHop(url=current, status=resp.status))
                break  # not a redirect
        except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
            hops.append(RedirectHop(url=current, status=e.code))
            location = e.headers.get("Location", "")
            if e.code in (301, 302, 303, 307, 308) and location:
                current = urllib.parse.urljoin(current, location)
                continue
            break
        except Exception:  # noqa: BLE001
            hops.append(RedirectHop(url=current, status=0))
            break
    return hops


def final_url(url: str, timeout: int = 15) -> str:
    """Resolve a URL through redirects to its final destination."""
    hops = trace_redirects(url, timeout=timeout)
    return hops[-1].url if hops else url


def make_crawl_plan(seed_url: str, sitemap_urls: list[str] | None = None,
                    max_pages: int = 100) -> dict:
    """Produce a crawl plan dict: seeds + sitemap URLs + config hints.

    The executor turns this into a CrawlFrontier + fetch loop.
    """
    seeds = [url_canonicalize(seed_url)]
    extra: list[str] = []
    if sitemap_urls:
        # Prefer shallow, likely-important sitemap URLs as extra seeds.
        extra = sort_by_depth(
            [u for u in sitemap_urls if same_site(seed_url, u)])[:20]
    plan = {
        "seeds": seeds,
        "sitemap_seeds": extra,
        "max_pages": max_pages,
        "politeness_delay": 1.0,
        "same_host_only": True,
        "exclude_patterns": [
            r"\.(pdf|zip|exe|dmg|mp4|mp3)(\?|$)",
            r"/(login|logout|signup|register|cart|checkout)",
            r"[?&](sort|order|filter)=",
        ],
        "notes": (
            "Fetch seeds first, then sitemap seeds. Push discovered links "
            "into the frontier at depth+1. Stop when done or max_pages."
        ),
    }
    return plan


def estimate_crawl_time(num_pages: int, politeness_delay: float = 1.0,
                        avg_fetch_s: float = 2.0) -> dict:
    """Rough time estimate for a crawl (for planning, not a promise)."""
    per_page = politeness_delay + avg_fetch_s
    total = num_pages * per_page
    return {
        "pages": num_pages,
        "seconds_per_page": round(per_page, 2),
        "total_seconds": round(total, 1),
        "total_minutes": round(total / 60, 1),
    }


def group_links_by_section(html: str, base_url: str) -> dict[str, list[str]]:
    """Group page links by their enclosing landmark (nav/main/footer/other)."""
    groups: dict[str, list[str]] = {"nav": [], "main": [], "footer": [],
                                   "other": []}

    class _SectionLinkParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.stack: list[str] = []
            self.found: list[tuple[str, str]] = []

        def handle_starttag(self, tag: str,
                            attrs: list[tuple[str, str | None]]) -> None:
            self.stack.append(tag)
            if tag == "a":
                a = dict(attrs)
                href = a.get("href", "") or ""
                section = "other"
                for t in reversed(self.stack):
                    if t in ("nav", "main", "footer"):
                        section = t
                        break
                self.found.append((section, href))

        def handle_endtag(self, tag: str) -> None:
            if self.stack and self.stack[-1] == tag:
                self.stack.pop()
            elif tag in self.stack:
                self.stack.remove(tag)

    parser = _SectionLinkParser()
    parser.feed(html or "")
    parser.close()
    seen: set[tuple[str, str]] = set()
    for section, raw in parser.found:
        if not raw or raw.startswith(("#", "mailto:", "javascript:")):
            continue
        abs_url = urllib.parse.urljoin(base_url, raw)
        if urllib.parse.urlsplit(abs_url).scheme not in ("http", "https"):
            continue
        canon = url_canonicalize(abs_url)
        if (section, canon) not in seen:
            seen.add((section, canon))
            groups[section].append(canon)
    return groups
