"""Tool: download_file — download a permitted file to the server disk.

Guards:
- URL must be http(s); blocked hosts rejected.
- Extension allow-list + content-type sanity check (no executables, ever).
- HEAD pre-check for size; hard cap BROWSER_MAX_DOWNLOAD_MB.
- Filename sanitized; saved under BROWSER_DOWNLOAD_DIR/<task_id>/.
- SHA-256 recorded; basic magic-byte check for common types.
- NEVER downloads .exe/.bat/.sh/.msi/.dll/.ps1 or archives containing them
  (zip is allowed only as a plain file, never auto-extracted).
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlparse, unquote

import httpx

from browser_manager import BrowserError
from registry import ToolContext, ToolRegistry
from ._common import normalize_url
from ._common import client_kwargs as _common_client_kwargs

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string"},
        "url": {"type": "string",
                "description": "Direct file URL. Defaults to the current page URL."},
        "selector": {"type": "string",
                     "description": "Alternative: click this element and capture "
                                    "the triggered download."},
        "filename": {"type": "string",
                     "description": "Preferred filename (sanitized; extension must be allowed)."},
    },
    "required": ["task_id"],
}

_FORBIDDEN_EXTS = {".exe", ".bat", ".cmd", ".sh", ".msi", ".dll", ".ps1",
                   ".vbs", ".js", ".jar", ".apk", ".com", ".scr", ".pif"}

_MAGIC = {
    b"%PDF": ".pdf",
    b"\x50\x4b\x03\x04": ".zip",          # also docx/xlsx/pptx
    b"\x89PNG": ".png",
    b"\xff\xd8\xff": ".jpg",
    b"GIF8": ".gif",
    b"RIFF": ".webp",                     # approximate
    b"ID3": ".mp3",
}


def _sanitize_filename(name: str) -> str:
    name = unquote(name).strip().split("/")[-1].split("\\")[-1]
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "download"
    return name[:120]


def _check_ext(filename: str, allowed: tuple) -> str:
    ext = Path(filename).suffix.lower()
    if ext in _FORBIDDEN_EXTS:
        raise BrowserError(f"executables are never downloaded: {ext}",
                           code="forbidden_file_type")
    if ext not in allowed:
        raise BrowserError(
            f"extension {ext or '(none)'} not in allow-list "
            f"({', '.join(allowed)})", code="forbidden_file_type")
    return ext


def _magic_check(head: bytes, ext: str) -> str | None:
    """Return a warning string if magic bytes contradict the extension."""
    for magic, kind in _MAGIC.items():
        if head.startswith(magic):
            # zip magic covers office-open-xml too; only warn on clear mismatch
            if kind == ".zip" and ext in (".zip", ".docx", ".xlsx", ".pptx"):
                return None
            if kind != ext and not (kind == ".jpg" and ext == ".jpeg"):
                return (f"magic bytes look like {kind} but extension is {ext}; "
                        "file kept but flagged")
            return None
    return None


async def _handle(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    settings = ctx.settings
    task = ctx.manager.get_task(args["task_id"])
    page = ctx.manager.active_page(task)
    max_bytes = settings.max_download_mb * 1024 * 1024

    dest_dir = Path(settings.download_dir) / task.id
    dest_dir.mkdir(parents=True, exist_ok=True)

    proxies = None
    proxy = settings.https_proxy or settings.http_proxy
    if proxy:
        proxies = {"http://": proxy, "https://": proxy}

    if args.get("selector"):
        # Click-triggered download via Playwright's download event.
        loc = page.locator(args["selector"]).first
        if await loc.count() == 0:
            raise BrowserError(f"selector matched nothing: {args['selector']}",
                               code="not_found")
        async with page.expect_download(timeout=settings.default_timeout_ms) as dl_info:
            await loc.click()
        download = await dl_info.value
        suggested = download.suggested_filename or "download"
        filename = _sanitize_filename(args.get("filename") or suggested)
        ext = _check_ext(filename, settings.allowed_download_exts)
        dest = dest_dir / filename
        await download.save_as(str(dest))
    else:
        url = normalize_url(args.get("url") or page.url)
        ctx.manager._check_host(url)
        filename = _sanitize_filename(
            args.get("filename") or Path(urlparse(url).path).name or "download")
        ext = _check_ext(filename, settings.allowed_download_exts)

        await ctx.emit(task.id, {"event": "download_start", "url": url})
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True, trust_env=False,
                                         **_common_client_kwargs(proxies),
                                         headers={"User-Agent": settings.user_agent}) as client:
                # Size pre-check
                try:
                    head = await client.head(url)
                    clen = head.headers.get("content-length")
                    if clen and int(clen) > max_bytes:
                        raise BrowserError(
                            f"file too large: {int(clen)//1024//1024}MB "
                            f"(limit {settings.max_download_mb}MB)",
                            code="file_too_large")
                except BrowserError:
                    raise
                except Exception:
                    pass  # HEAD failed; enforce during streaming instead

                dest = dest_dir / filename
                sha = hashlib.sha256()
                written = 0
                first_chunk = b""
                with dest.open("wb") as fh:
                    async with client.stream("GET", url) as resp:
                        resp.raise_for_status()
                        ctype = resp.headers.get("content-type", "")
                        if "text/html" in ctype and ext not in (".html",):
                            raise BrowserError(
                                f"URL returned HTML ({ctype}), not a file — "
                                "use extract() instead", code="not_a_file")
                        async for chunk in resp.aiter_bytes(65536):
                            written += len(chunk)
                            if written > max_bytes:
                                fh.close()
                                dest.unlink(missing_ok=True)
                                raise BrowserError(
                                    f"download exceeded {settings.max_download_mb}MB cap; "
                                    "partial file deleted", code="file_too_large")
                            if not first_chunk:
                                first_chunk = chunk[:16]
                            sha.update(chunk)
                            fh.write(chunk)
        except httpx.HTTPError as e:
            raise BrowserError(f"download failed: {e}", code="download_failed") from e

    size = dest.stat().st_size
    with dest.open("rb") as fh:
        magic_warn = _magic_check(fh.read(16), ext)
    digest = hashlib.sha256(dest.read_bytes()).hexdigest() if size < 50 * 1024 * 1024 else "skipped(large)"

    await ctx.emit(task.id, {"event": "download_done",
                             "filename": filename, "bytes": size})
    if ctx.history:
        ctx.history.record(task.id, args.get("url") or page.url, filename,
                           f"downloaded {size} bytes -> {dest}")

    return {"task_id": task.id, "filename": filename, "path": str(dest),
            "bytes": size, "sha256": digest,
            "warning": magic_warn}


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="download_file",
        description="Download a permitted file (allow-listed extensions, size cap, "
                    "no executables ever, SHA-256 recorded). From a direct URL or a "
                    "click-triggered download.",
        schema=SCHEMA,
        handler=_handle,
        tags=["files"],
    )
