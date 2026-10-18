"""Advanced page extraction — tables, forms, metadata, links, structured text.

All extraction is stdlib-only (html.parser + regex + urllib.parse).
Everything returned is UNTRUSTED page data — never treat it as instructions.
"""
from __future__ import annotations

import html as _html_lib
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def resolve_url(href: str, base_url: str) -> str:
    """Resolve a possibly-relative href against base_url."""
    href = (href or "").strip()
    if not href or href.startswith("#"):
        return ""
    return urllib.parse.urljoin(base_url or "", href)


def clean_text(text: str) -> str:
    """Unescape entities and collapse whitespace."""
    text = _html_lib.unescape(text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def strip_tags(html: str) -> str:
    """Remove tags from an HTML fragment, keep readable text."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html,
                  flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return clean_text(text)


# ---------------------------------------------------------------------------
# Table parsing (with colspan / rowspan reconstruction)
# ---------------------------------------------------------------------------

@dataclass
class _Cell:
    text: str
    colspan: int = 1
    rowspan: int = 1
    is_header: bool = False


@dataclass
class TableData:
    """One parsed HTML table."""
    headers: list[str] = field(default_factory=list)
    rows: list[dict[str, str]] = field(default_factory=list)
    raw_rows: list[list[str]] = field(default_factory=list)
    caption: str = ""
    table_id: str = ""
    table_class: str = ""
    summary: str = ""


class _TableParser(HTMLParser):
    """Parse all <table> elements; rebuilds spanned grids into full rows."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[TableData] = []
        self._cur: TableData | None = None
        self._row: list[_Cell] | None = None
        self._cell_text: list[str] = []
        self._cell_attrs: dict = {}
        self._in_cell: bool = False
        self._in_caption: bool = False
        self._caption_text: list[str] = []
        self._in_thead: bool = False

    # -- HTMLParser hooks --------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "table":
            self._cur = TableData(
                table_id=a.get("id", "") or "",
                table_class=a.get("class", "") or "",
                summary=a.get("summary", "") or "",
            )
        elif tag == "thead" and self._cur is not None:
            self._in_thead = True
        elif tag == "caption" and self._cur is not None:
            self._in_caption = True
            self._caption_text = []
        elif tag == "tr" and self._cur is not None:
            self._row = []
        elif tag in ("td", "th") and self._cur is not None and self._row is not None:
            self._in_cell = True
            self._cell_text = []
            self._cell_attrs = a
            self._cell_is_header = (tag == "th") or self._in_thead

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._cur is not None:
            self._finalize_table()
            self.tables.append(self._cur)
            self._cur = None
        elif tag == "thead":
            self._in_thead = False
        elif tag == "caption":
            self._in_caption = False
            if self._cur is not None:
                self._cur.caption = clean_text("".join(self._caption_text))
        elif tag == "tr" and self._cur is not None and self._row is not None:
            self._cur_rows_cells().append(self._row)
            self._row = None
        elif tag in ("td", "th") and self._in_cell:
            text = clean_text("".join(self._cell_text))
            try:
                colspan = max(1, int(self._cell_attrs.get("colspan") or 1))
            except (TypeError, ValueError):
                colspan = 1
            try:
                rowspan = max(1, int(self._cell_attrs.get("rowspan") or 1))
            except (TypeError, ValueError):
                rowspan = 1
            if self._row is not None:
                self._row.append(_Cell(text=text, colspan=colspan,
                                       rowspan=rowspan,
                                       is_header=getattr(self, "_cell_is_header", False)))
            self._in_cell = False
            self._cell_text = []

    def handle_data(self, data: str) -> None:
        if self._in_caption:
            self._caption_text.append(data)
        elif self._in_cell:
            self._cell_text.append(data)

    # -- grid reconstruction ------------------------------------------------
    def _cur_rows_cells(self) -> list[list[_Cell]]:
        if not hasattr(self, "_pending_rows"):
            self._pending_rows: list[list[_Cell]] = []
        return self._pending_rows

    def _finalize_table(self) -> None:
        assert self._cur is not None
        row_cells = getattr(self, "_pending_rows", [])
        self._pending_rows = []

        # Expand spans into a full rectangular grid.
        grid: list[list[str]] = []
        spanned: dict[tuple[int, int], str] = {}  # (row, col) -> text (rowspan fill)

        for r, cells in enumerate(row_cells):
            while len(grid) <= r:
                grid.append([])
            row = grid[r]
            c = 0
            for cell in cells:
                # Skip columns already occupied by a rowspan from above.
                while (r, c) in spanned:
                    row.append(spanned.pop((r, c)))
                    c += 1
                for dc in range(cell.colspan):
                    row.append(cell.text)
                    c += 1
                for dr in range(1, cell.rowspan):
                    for dc in range(cell.colspan):
                        spanned[(r + dr, c - cell.colspan + dc)] = cell.text
            # Trailing rowspan fills.
            while (r, c) in spanned:
                row.append(spanned.pop((r, c)))
                c += 1

        if not grid:
            return

        # Determine header row.
        first_cells = row_cells[0] if row_cells else []
        has_th = any(c.is_header for c in first_cells)
        # A single spanning cell in the first row is a title row, not a
        # header — promote it to the caption when no caption exists.
        is_title_row = (len(first_cells) == 1
                        and first_cells[0].colspan > 1)
        if is_title_row:
            title_text = grid[0][0] if grid else ""
            if title_text and not self._cur.caption:
                self._cur.caption = title_text
            headers = [f"col_{i + 1}" for i in range(len(grid[0]))] \
                if grid else []
            data_start = 0
        elif has_th:
            headers = [h for h in grid[0]] if grid else []
            data_start = 1
        elif (grid and len(grid) > 1
                and all(len(cell.split()) <= 6 for cell in grid[0])):
            # Heuristic: short first row with no <th> still reads as headers
            # (pandas-style default).
            headers = [h for h in grid[0]]
            data_start = 1
        else:
            headers = [f"col_{i + 1}" for i in range(len(grid[0]))] \
                if grid else []
            data_start = 0
        # Deduplicate header names.
        seen: dict[str, int] = {}
        uniq_headers: list[str] = []
        for h in headers:
            h = h or f"col_{len(uniq_headers) + 1}"
            if h in seen:
                seen[h] += 1
                h = f"{h}_{seen[h]}"
            else:
                seen[h] = 0
            uniq_headers.append(h)
        headers = uniq_headers
        header_row_idx = data_start

        self._cur.headers = headers
        self._cur.raw_rows = grid[data_start:]
        ncols = len(headers)
        for grow in grid[data_start:]:
            row_dict: dict[str, str] = {}
            for i in range(ncols):
                row_dict[headers[i]] = grow[i] if i < len(grow) else ""
            self._cur.rows.append(row_dict)


# ---------------------------------------------------------------------------
# Form parsing
# ---------------------------------------------------------------------------

@dataclass
class FormField:
    """One input/select/textarea/button inside a form."""
    name: str
    field_type: str  # text, email, password, number, checkbox, radio, select, textarea, ...
    label: str = ""
    value: str = ""
    required: bool = False
    placeholder: str = ""
    options: list[dict[str, str]] = field(default_factory=list)  # for select/radio
    min_value: str = ""
    max_value: str = ""
    pattern: str = ""
    maxlength: str = ""
    multiple: bool = False


@dataclass
class FormData:
    """One parsed HTML form."""
    action: str
    method: str  # GET or POST
    form_id: str = ""
    form_name: str = ""
    fields: list[FormField] = field(default_factory=list)


class _FormParser(HTMLParser):
    """Parse <form> elements with their fields, labels, and options."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[FormData] = []
        self._cur: FormData | None = None
        self._cur_field: FormField | None = None
        self._in_select: bool = False
        self._cur_option: dict[str, str] | None = None
        self._option_text: list[str] = []
        self._in_textarea: bool = False
        self._textarea_text: list[str] = []
        # label tracking: <label for="id">text</label>
        self._in_label: bool = False
        self._label_for: str = ""
        self._label_text: list[str] = []
        self._labels: dict[str, str] = {}
        # implicit label wrap: <label>...<input>...</label>
        self._label_stack: list[str] = []

    # -- HTMLParser hooks --------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "form":
            self._cur = FormData(
                action=a.get("action", "") or "",
                method=(a.get("method", "get") or "get").upper(),
                form_id=a.get("id", "") or "",
                form_name=a.get("name", "") or "",
            )
            self._labels = {}
        elif self._cur is None:
            return
        elif tag == "label":
            self._in_label = True
            self._label_for = a.get("for", "") or ""
            self._label_text = []
        elif tag == "input":
            ftype = (a.get("type", "text") or "text").lower()
            if ftype in ("submit", "button", "reset", "image", "hidden"):
                kind = ftype
            else:
                kind = ftype
            field = FormField(
                name=a.get("name", "") or a.get("id", "") or "",
                field_type=kind,
                value=a.get("value", "") or "",
                required="required" in a,
                placeholder=a.get("placeholder", "") or "",
                min_value=a.get("min", "") or "",
                max_value=a.get("max", "") or "",
                pattern=a.get("pattern", "") or "",
                maxlength=a.get("maxlength", "") or "",
                multiple="multiple" in a,
            )
            self._attach_field(field, a.get("id", "") or "")
        elif tag == "select":
            self._in_select = True
            self._cur_field = FormField(
                name=a.get("name", "") or a.get("id", "") or "",
                field_type="select",
                required="required" in a,
                multiple="multiple" in a,
            )
            self._attach_field(self._cur_field, a.get("id", "") or "")
        elif tag == "option" and self._in_select and self._cur_field is not None:
            self._cur_option = {
                "value": a.get("value", "") or "",
                "selected": "1" if "selected" in a else "0",
            }
            self._option_text = []
        elif tag == "textarea":
            self._in_textarea = True
            self._textarea_text = []
            self._cur_field = FormField(
                name=a.get("name", "") or a.get("id", "") or "",
                field_type="textarea",
                required="required" in a,
                placeholder=a.get("placeholder", "") or "",
                maxlength=a.get("maxlength", "") or "",
            )
            self._attach_field(self._cur_field, a.get("id", "") or "")
        elif tag == "button":
            btype = (a.get("type", "submit") or "submit").lower()
            field = FormField(
                name=a.get("name", "") or "",
                field_type=f"button:{btype}",
                value=a.get("value", "") or "",
            )
            self._attach_field(field, a.get("id", "") or "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._cur is not None:
            # Apply collected labels.
            for f in self._cur.fields:
                if not f.label and f.name in self._labels:
                    f.label = self._labels[f.name]
            self.forms.append(self._cur)
            self._cur = None
        elif tag == "label":
            self._in_label = False
            text = clean_text("".join(self._label_text))
            if self._label_for:
                self._labels[self._label_for] = text
            elif self._cur is not None and self._cur.fields:
                # Implicit label: attach to most recent field if unlabeled.
                if not self._cur.fields[-1].label:
                    self._cur.fields[-1].label = text
        elif tag == "select":
            self._in_select = False
            self._cur_field = None
        elif tag == "option" and self._cur_option is not None and self._cur_field is not None:
            text = clean_text("".join(self._option_text))
            value = self._cur_option["value"] or text
            self._cur_field.options.append({
                "value": value, "text": text,
                "selected": self._cur_option["selected"],
            })
            self._cur_option = None
        elif tag == "textarea":
            self._in_textarea = False
            if self._cur_field is not None:
                self._cur_field.value = clean_text("".join(self._textarea_text))
            self._cur_field = None

    def handle_data(self, data: str) -> None:
        if self._in_label:
            self._label_text.append(data)
        elif self._cur_option is not None:
            self._option_text.append(data)
        elif self._in_textarea:
            self._textarea_text.append(data)

    def _attach_field(self, field: FormField, elem_id: str) -> None:
        assert self._cur is not None
        if elem_id and elem_id in self._labels:
            field.label = self._labels[elem_id]
        self._cur.fields.append(field)


# ---------------------------------------------------------------------------
# Metadata parsing
# ---------------------------------------------------------------------------

@dataclass
class PageMetadata:
    """title, description, Open Graph / Twitter tags, canonical, misc meta."""
    title: str = ""
    description: str = ""
    keywords: str = ""
    canonical: str = ""
    og: dict[str, str] = field(default_factory=dict)
    twitter: dict[str, str] = field(default_factory=dict)
    other: dict[str, str] = field(default_factory=dict)
    language: str = ""
    charset: str = ""


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta = PageMetadata()
        self._in_title = False
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "html":
            self.meta.language = a.get("lang", "") or ""
        elif tag == "title":
            self._in_title = True
            self._title_parts = []
        elif tag == "meta":
            name = (a.get("name", "") or "").lower()
            prop = (a.get("property", "") or "").lower()
            content = a.get("content", "") or ""
            charset = a.get("charset", "") or ""
            if charset:
                self.meta.charset = charset
            key = name or prop
            if prop.startswith("og:"):
                self.meta.og[prop[3:]] = content
            elif prop.startswith("twitter:") or name.startswith("twitter:"):
                clean = (prop or name).split(":", 1)[1]
                self.meta.twitter[clean] = content
            elif name == "description":
                self.meta.description = content
            elif name == "keywords":
                self.meta.keywords = content
            elif key:
                self.meta.other[key] = content
        elif tag == "link":
            rel = (a.get("rel", "") or "").lower()
            if rel == "canonical":
                self.meta.canonical = a.get("href", "") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            self.meta.title = clean_text("".join(self._title_parts))

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)


# ---------------------------------------------------------------------------
# Link parsing + categorization
# ---------------------------------------------------------------------------

DOWNLOAD_EXTENSIONS = frozenset({
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2",
    ".exe", ".msi", ".dmg", ".pkg", ".apk", ".deb", ".rpm",
    ".mp3", ".mp4", ".avi", ".mkv", ".mov", ".wav", ".flac",
    ".csv", ".tsv", ".json", ".xml", ".epub", ".mobi",
    ".iso", ".img", ".torrent",
})


@dataclass
class LinkInfo:
    url: str
    text: str
    title: str = ""
    rel: str = ""


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[LinkInfo] = []
        self._cur: LinkInfo | None = None
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            a = dict(attrs)
            self._cur = LinkInfo(
                url=a.get("href", "") or "",
                text="",
                title=a.get("title", "") or "",
                rel=a.get("rel", "") or "",
            )
            self._text_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._cur is not None:
            self._cur.text = clean_text("".join(self._text_parts))
            self.links.append(self._cur)
            self._cur = None

    def handle_data(self, data: str) -> None:
        if self._cur is not None:
            self._text_parts.append(data)


# ---------------------------------------------------------------------------
# Structured text parsing (headings hierarchy, paragraphs, lists)
# ---------------------------------------------------------------------------

@dataclass
class Heading:
    level: int
    text: str


@dataclass
class TextList:
    ordered: bool
    items: list[str]


@dataclass
class StructuredText:
    headings: list[Heading] = field(default_factory=list)
    paragraphs: list[str] = field(default_factory=list)
    lists: list[TextList] = field(default_factory=list)


class _StructuredTextParser(HTMLParser):
    """Collect headings (h1-h6), paragraphs, and (possibly nested) lists."""

    SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg",
                           "nav", "footer", "aside"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.result = StructuredText()
        self._skip_depth = 0
        self._in_heading: int = 0
        self._heading_text: list[str] = []
        self._in_p = False
        self._p_text: list[str] = []
        # List stack: each entry = {"ordered": bool, "items": [...], "cur_item": [...]}
        self._list_stack: list[dict] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._in_heading = int(tag[1])
            self._heading_text = []
        elif tag == "p":
            self._in_p = True
            self._p_text = []
        elif tag in ("ul", "ol"):
            self._list_stack.append({"ordered": tag == "ol", "items": [],
                                     "cur_item": None})
        elif tag == "li" and self._list_stack:
            self._list_stack[-1]["cur_item"] = []

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6") and self._in_heading:
            text = clean_text("".join(self._heading_text))
            if text:
                self.result.headings.append(Heading(level=self._in_heading, text=text))
            self._in_heading = 0
        elif tag == "p" and self._in_p:
            text = clean_text("".join(self._p_text))
            if text:
                self.result.paragraphs.append(text)
            self._in_p = False
        elif tag == "li" and self._list_stack:
            cur = self._list_stack[-1]["cur_item"]
            if cur is not None:
                text = clean_text("".join(cur))
                if text:
                    self._list_stack[-1]["items"].append(text)
            self._list_stack[-1]["cur_item"] = None
        elif tag in ("ul", "ol") and self._list_stack:
            lst = self._list_stack.pop()
            if lst["items"]:
                self.result.lists.append(TextList(ordered=lst["ordered"],
                                                  items=lst["items"]))

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_heading:
            self._heading_text.append(data)
        elif self._in_p:
            self._p_text.append(data)
        elif self._list_stack and self._list_stack[-1]["cur_item"] is not None:
            self._list_stack[-1]["cur_item"].append(data)


# ---------------------------------------------------------------------------
# JSON-LD parsing
# ---------------------------------------------------------------------------

class _JsonLdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict | list] = []
        self._in_ld = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            a = dict(attrs)
            if (a.get("type", "") or "").lower() == "application/ld+json":
                self._in_ld = True
                self._parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_ld:
            self._in_ld = False
            raw = "".join(self._parts).strip()
            if raw:
                try:
                    self.blocks.append(json.loads(raw))
                except (json.JSONDecodeError, ValueError):
                    pass

    def handle_data(self, data: str) -> None:
        if self._in_ld:
            self._parts.append(data)


# ---------------------------------------------------------------------------
# Readability-style boilerplate removal
# ---------------------------------------------------------------------------

_BOILERPLATE_RE = re.compile(
    r"\b(comment|comments|widget|sidebar|footer|header|nav|menu|ad-|ads|"
    r"banner|popup|modal|share|social|related|subscribe|newsletter|cookie)\b",
    re.IGNORECASE,
)


class _ReadabilityParser(HTMLParser):
    """Score block-level elements by text density vs link density."""

    BLOCK_TAGS = frozenset({"p", "div", "article", "section", "main", "td", "li",
                            "h1", "h2", "h3", "h4", "blockquote", "pre"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict] = []  # {text, link_chars, total_chars, tag, cls}
        self._stack: list[dict] = []
        self._skip = 0
        self._in_link = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "noscript", "template", "svg", "nav",
                   "footer", "aside", "form", "iframe"):
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "a":
            self._in_link += 1
        if tag in self.BLOCK_TAGS:
            a = dict(attrs)
            cls = f"{a.get('class', '') or ''} {a.get('id', '') or ''}"
            self._stack.append({"tag": tag, "cls": cls, "text": [],
                                "link_chars": 0})

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "template", "svg", "nav",
                   "footer", "aside", "form", "iframe"):
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag == "a":
            self._in_link = max(0, self._in_link - 1)
        if tag in self.BLOCK_TAGS and self._stack:
            blk = self._stack.pop()
            text = clean_text("".join(blk["text"]))
            if len(text) >= 25:
                self.blocks.append({
                    "text": text,
                    "link_chars": blk["link_chars"],
                    "total_chars": len(text),
                    "tag": blk["tag"],
                    "cls": blk["cls"],
                })

    def handle_data(self, data: str) -> None:
        if self._skip or not self._stack:
            return
        top = self._stack[-1]
        top["text"].append(data)
        if self._in_link:
            top["link_chars"] += len(data)


def _score_block(block: dict) -> float:
    total = max(1, block["total_chars"])
    link_density = block["link_chars"] / total
    length_score = min(1.0, total / 800.0)
    boilerplate_penalty = 0.4 if _BOILERPLATE_RE.search(block["cls"]) else 0.0
    tag_bonus = 0.2 if block["tag"] in ("article", "p") else 0.0
    return length_score * (1.0 - link_density) - boilerplate_penalty + tag_bonus


# ---------------------------------------------------------------------------
# Main facade
# ---------------------------------------------------------------------------

class AdvancedExtractor:
    """Rich HTML extraction: tables, forms, metadata, links, text, JSON-LD.

    All methods take raw HTML (already fetched) plus an optional base_url
    used to resolve relative links.
    """

    def __init__(self, max_text_chars: int = 50000):
        self.max_text_chars = max_text_chars

    # -- tables ------------------------------------------------------------
    def extract_tables(self, html: str, base_url: str = "") -> list[TableData]:
        """Parse every <table> into headers + list-of-dict rows.

        Handles colspan/rowspan by expanding into a full grid. Nested tables
        are flattened into the outer table's text.
        """
        parser = _TableParser()
        parser.feed(html or "")
        parser.close()
        return parser.tables

    def tables_as_dicts(self, html: str, base_url: str = "") -> list[dict]:
        """Convenience: tables serialized to plain dicts."""
        out = []
        for t in self.extract_tables(html, base_url):
            out.append({
                "headers": t.headers,
                "rows": t.rows,
                "caption": t.caption,
                "id": t.table_id,
                "class": t.table_class,
            })
        return out

    # -- forms -------------------------------------------------------------
    def extract_forms(self, html: str, base_url: str = "") -> list[FormData]:
        """Parse every <form> with fields, labels, types, and options."""
        parser = _FormParser()
        parser.feed(html or "")
        parser.close()
        for form in parser.forms:
            if form.action:
                form.action = resolve_url(form.action, base_url)
        return parser.forms

    # -- metadata ----------------------------------------------------------
    def extract_metadata(self, html: str, base_url: str = "") -> PageMetadata:
        """title, meta description/keywords, Open Graph, Twitter cards, canonical."""
        parser = _MetaParser()
        parser.feed(html or "")
        parser.close()
        if parser.meta.canonical:
            parser.meta.canonical = resolve_url(parser.meta.canonical, base_url)
        return parser.meta

    # -- links -------------------------------------------------------------
    def extract_links_categorized(self, html: str,
                                  base_url: str = "") -> dict[str, list[LinkInfo]]:
        """Split links into internal / external / downloads / emails / anchors.

        - internal: same host as base_url (or relative)
        - external: different host
        - downloads: URL ends with a known file extension
        - emails: mailto: links
        - anchors: pure #fragment links
        """
        parser = _LinkParser()
        parser.feed(html or "")
        parser.close()

        base_host = urllib.parse.urlparse(base_url or "").netloc.lower()
        cats: dict[str, list[LinkInfo]] = {
            "internal": [], "external": [], "downloads": [],
            "emails": [], "anchors": [],
        }
        seen: set[str] = set()
        for link in parser.links:
            raw = (link.url or "").strip()
            if not raw:
                continue
            if raw.startswith("mailto:"):
                cats["emails"].append(LinkInfo(url=raw, text=link.text))
                continue
            if raw.startswith("#"):
                cats["anchors"].append(link)
                continue
            if raw.lower().startswith(("javascript:", "tel:", "ftp:")):
                continue
            abs_url = resolve_url(raw, base_url)
            if not abs_url or abs_url in seen:
                continue
            seen.add(abs_url)
            info = LinkInfo(url=abs_url, text=link.text,
                            title=link.title, rel=link.rel)
            path = urllib.parse.urlparse(abs_url).path.lower()
            ext = "." + path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ""
            if ext in DOWNLOAD_EXTENSIONS:
                cats["downloads"].append(info)
            host = urllib.parse.urlparse(abs_url).netloc.lower()
            if not base_host or host == base_host or host.endswith("." + base_host):
                cats["internal"].append(info)
            else:
                cats["external"].append(info)
        return cats

    # -- structured text ---------------------------------------------------
    def extract_text_structured(self, html: str) -> StructuredText:
        """Headings hierarchy (h1-h6), paragraphs, and lists."""
        parser = _StructuredTextParser()
        parser.feed(html or "")
        parser.close()
        return parser.result

    def extract_headings_outline(self, html: str) -> list[dict]:
        """Headings as an indented outline: [{level, text, indent}]."""
        st = self.extract_text_structured(html)
        min_level = min((h.level for h in st.headings), default=1)
        return [{"level": h.level, "text": h.text,
                 "indent": h.level - min_level} for h in st.headings]

    # -- JSON-LD -----------------------------------------------------------
    def extract_json_ld(self, html: str) -> list[dict | list]:
        """All <script type='application/ld+json'> blocks, parsed to objects."""
        parser = _JsonLdParser()
        parser.feed(html or "")
        parser.close()
        return parser.blocks

    def extract_json_ld_types(self, html: str) -> dict[str, list[dict]]:
        """JSON-LD blocks grouped by their @type."""
        grouped: dict[str, list[dict]] = {}
        for block in self.extract_json_ld(html):
            items = block if isinstance(block, list) else [block]
            for item in items:
                if isinstance(item, dict):
                    t = item.get("@type", "Unknown")
                    types = t if isinstance(t, list) else [t]
                    for one in types:
                        grouped.setdefault(str(one), []).append(item)
        return grouped

    # -- readability -------------------------------------------------------
    def readability_text(self, html: str) -> str:
        """Article-like main text with boilerplate removed.

        Heuristic: score text blocks by length, penalize link density and
        boilerplate class names, keep blocks scoring above a threshold.
        Falls back to plain stripped text when nothing scores well.
        """
        parser = _ReadabilityParser()
        parser.feed(html or "")
        parser.close()
        scored = [(blk, _score_block(blk)) for blk in parser.blocks]
        good = [blk for blk, s in scored if s >= 0.15]
        if good:
            # Preserve document order.
            order = {id(b): i for i, b in enumerate(parser.blocks)}
            good.sort(key=lambda b: order[id(b)])
            text = "\n\n".join(b["text"] for b in good)
        else:
            # Fallback: plain stripped text.
            text = strip_tags(html)
        return text[:self.max_text_chars]

    def readability_paragraphs(self, html: str) -> list[str]:
        """Main text as a list of paragraphs."""
        return [p for p in self.readability_text(html).split("\n\n") if p.strip()]

    # -- convenience -------------------------------------------------------
    def extract_all(self, html: str, base_url: str = "") -> dict:
        """Run every extractor; return one combined dict (JSON-serializable)."""
        meta = self.extract_metadata(html, base_url)
        links = self.extract_links_categorized(html, base_url)
        st = self.extract_text_structured(html)
        return {
            "metadata": {
                "title": meta.title,
                "description": meta.description,
                "keywords": meta.keywords,
                "canonical": meta.canonical,
                "language": meta.language,
                "og": meta.og,
                "twitter": meta.twitter,
            },
            "tables": self.tables_as_dicts(html, base_url),
            "forms": [
                {
                    "action": f.action, "method": f.method,
                    "fields": [
                        {"name": fld.name, "type": fld.field_type,
                         "label": fld.label, "required": fld.required,
                         "value": fld.value, "options": fld.options}
                        for fld in f.fields
                    ],
                }
                for f in self.extract_forms(html, base_url)
            ],
            "links": {
                cat: [{"url": l.url, "text": l.text} for l in items]
                for cat, items in links.items()
            },
            "headings": [{"level": h.level, "text": h.text} for h in st.headings],
            "paragraphs": st.paragraphs,
            "lists": [{"ordered": l.ordered, "items": l.items} for l in st.lists],
            "json_ld_types": list(self.extract_json_ld_types(html).keys()),
            "main_text": self.readability_text(html),
        }
