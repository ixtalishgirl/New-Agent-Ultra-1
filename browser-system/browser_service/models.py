"""Pydantic request/response models for the REST API."""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


# ---------- generic ----------

class ToolCallRequest(BaseModel):
    """Generic dispatch: POST /tools/call"""
    name: str = Field(..., description="Registered tool name, e.g. 'navigate'")
    arguments: Dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    ok: bool = True
    tool: str
    task_id: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    error_code: Optional[str] = None


class ToolSpec(BaseModel):
    name: str
    description: str
    schema: Dict[str, Any]
    tags: List[str] = []


# ---------- per-tool request bodies ----------

class NavigateRequest(BaseModel):
    task_id: Optional[str] = None
    url: str
    wait_until: Literal["load", "domcontentloaded", "networkidle"] = "domcontentloaded"
    timeout_ms: Optional[int] = None


class SearchRequest(BaseModel):
    task_id: Optional[str] = None
    query: str
    engine: Literal["duckduckgo", "duckduckgo-lite"] = "duckduckgo"
    max_results: int = Field(default=10, ge=1, le=30)


class OpenLinkRequest(BaseModel):
    task_id: str
    # exactly one of these:
    index: Optional[int] = None      # index from the last extract() links list
    href: Optional[str] = None       # exact/partial href match
    text: Optional[str] = None       # link text contains
    selector: Optional[str] = None   # CSS selector of the <a>
    new_tab: bool = False


class ExtractRequest(BaseModel):
    task_id: str
    selector: Optional[str] = None   # CSS scope; default: whole page
    include_links: bool = True
    include_tables: bool = True
    include_forms: bool = True
    max_chars: int = Field(default=20000, ge=500, le=200000)


class ScrollRequest(BaseModel):
    task_id: str
    direction: Literal["down", "up", "to_bottom", "to_top", "to_selector"] = "down"
    amount_px: int = Field(default=800, ge=1, le=20000)
    selector: Optional[str] = None   # required when direction == to_selector


class PaginateRequest(BaseModel):
    task_id: str
    selector: Optional[str] = None   # "next" link/button; auto-detected if omitted
    max_pages: int = Field(default=3, ge=1, le=20)


class FormField(BaseModel):
    selector: str
    action: Literal["fill", "click", "select", "check", "uncheck", "press"] = "fill"
    value: Optional[str] = None      # fill/select/press (key name for press)


class FillFormRequest(BaseModel):
    task_id: str
    fields: List[FormField]
    submit: bool = False             # click the form's submit button afterwards
    allow_sensitive: bool = False    # must be true to touch input[type=password]


class ScreenshotRequest(BaseModel):
    task_id: str
    full_page: bool = False
    selector: Optional[str] = None    # screenshot one element instead


class NewTabRequest(BaseModel):
    task_id: str
    url: Optional[str] = None


class SwitchTabRequest(BaseModel):
    task_id: str
    tab_id: str


class CloseTabRequest(BaseModel):
    task_id: str
    tab_id: str


class DownloadFileRequest(BaseModel):
    task_id: str
    url: Optional[str] = None        # defaults to the current page URL
    selector: Optional[str] = None   # or click-triggered download from this element
    filename: Optional[str] = None   # sanitized automatically


class DetectBlocksRequest(BaseModel):
    task_id: str
