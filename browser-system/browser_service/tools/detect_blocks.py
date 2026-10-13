"""Tool: detect_blocks — DETECT anti-bot / CAPTCHA / login walls. NEVER solve.

Checks the current page for signals of:
  - captcha            (reCAPTCHA / hCaptcha / Turnstile widgets, "verify you are human")
  - bot_check          (PerimeterX / DataDome / Cloudflare challenge pages)
  - login_wall         (page demands sign-in before showing content)
  - blocked            (HTTP 403/429 with block wording, access-denied pages)
  - rate_limited       ("too many requests" wording)

Returns a structured verdict plus the raw signals found. The service will
NOT attempt to click, hold, solve, or bypass anything — that is a hard rule
from ahsan. The caller (Node app) must surface the verdict to ahsan and stop.
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List

from registry import ToolContext, ToolRegistry

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {"task_id": {"type": "string"}},
    "required": ["task_id"],
}

_JS_SIGNALS = """() => {
    const html = document.documentElement.innerHTML || '';
    const text = (document.body ? document.body.innerText : '').slice(0, 8000);
    const low = (html + '\\n' + text).toLowerCase();
    const signals = [];
    const has = (re, label) => { if (re.test(low)) signals.push(label); };

    // CAPTCHA widgets
    has(/g-recaptcha|recaptcha\\/api|recaptcha__en/, 'recaptcha_widget');
    has(/hcaptcha\\.com|h-captcha/, 'hcaptcha_widget');
    has(/challenges\\.cloudflare\\.com|cf-turnstile|turnstile/, 'cloudflare_turnstile');
    has(/perimeterx|px-captcha|_px/, 'perimeterx_present');
    has(/datadome/, 'datadome_present');
    has(/funcaptcha|arkoselabs/, 'funcaptcha_present');

    // Challenge / verify wording
    has(/verify you are human|prove you.{0,20}human|i am not a robot|i'm not a robot/, 'human_verification_text');
    has(/press (&|and) hold|press and hold to confirm/, 'press_and_hold_challenge');
    has(/attention required!?.{0,40}cloudflare|cf-please-wait|just a moment/, 'cloudflare_challenge');
    has(/checking your browser/, 'browser_check_text');
    has(/unusual traffic|automated requests|bot detection/, 'bot_detection_text');

    // HTTP-ish blocks rendered in-page
    has(/access denied|access to this page has been denied|403 forbidden/, 'access_denied_text');
    has(/too many requests|rate limit|429/, 'rate_limit_text');
    has(/captcha/, 'captcha_word');

    // Login walls: password field + almost no readable content
    const pw = document.querySelectorAll('input[type="password"]').length;
    const contentLen = (document.body ? document.body.innerText.trim().length : 0);
    if (pw > 0 && contentLen < 1500) signals.push('login_wall_likely');

    return {signals, title: document.title, textSample: text.slice(0, 500)};
}"""

_BLOCK_TYPE_PRIORITY = [
    ("captcha", ["recaptcha_widget", "hcaptcha_widget", "cloudflare_turnstile",
                 "funcaptcha_present", "captcha_word", "human_verification_text"]),
    ("bot_check", ["perimeterx_present", "datadome_present", "press_and_hold_challenge",
                   "cloudflare_challenge", "browser_check_text", "bot_detection_text"]),
    ("blocked", ["access_denied_text"]),
    ("rate_limited", ["rate_limit_text"]),
    ("login_wall", ["login_wall_likely"]),
]


def _classify(signals: List[str]) -> str | None:
    for block_type, markers in _BLOCK_TYPE_PRIORITY:
        if any(m in signals for m in markers):
            return block_type
    return None


async def _handle(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    task = ctx.manager.get_task(args["task_id"])
    page = ctx.manager.active_page(task)

    data = await page.evaluate(_JS_SIGNALS)
    signals: List[str] = data.get("signals") or []
    block_type = _classify(signals)

    # HTTP status hint from the last response, if we can get one cheaply.
    http_status = None
    try:
        resp = await page.request.get(page.url)  # lightweight re-GET for status only
        http_status = resp.status
    except Exception:
        http_status = None
    if http_status in (403, 429) and not block_type:
        block_type = "blocked" if http_status == 403 else "rate_limited"
        signals.append(f"http_{http_status}")

    blocked = block_type is not None
    verdict = {
        "task_id": task.id,
        "url": page.url,
        "title": data.get("title", ""),
        "timestamp": time.time(),
        "blocked": blocked,
        "block_type": block_type,          # captcha | bot_check | login_wall | blocked | rate_limited | null
        "signals": sorted(set(signals)),
        "text_sample": data.get("textSample", ""),
        "action": ("STOP — anti-bot/verification detected. This service never solves "
                   "or bypasses challenges. Report to ahsan and do not retry rapidly."
                   if blocked else
                   "OK — no block signals detected; page appears normally accessible."),
    }
    await ctx.emit(task.id, {"event": "detect_blocks",
                             "blocked": blocked, "block_type": block_type})
    if ctx.history and blocked:
        ctx.history.record(task.id, page.url, data.get("title", ""),
                           f"BLOCK DETECTED: {block_type} ({', '.join(verdict['signals'])})")
    return verdict


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="detect_blocks",
        description="DETECT-ONLY check for CAPTCHA / bot-check / login-wall / block "
                    "pages. Returns block_type + signals. NEVER attempts to solve or "
                    "bypass — the caller must stop and report to ahsan.",
        schema=SCHEMA,
        handler=_handle,
        tags=["safety"],
    )
