"""Browser settings: search engine selection + network mode selection.

Independent settings — the search engine and the network mode can be changed
separately (e.g. Google in normal mode, DuckDuckGo in Tor mode).

Persisted to /tmp/agent_browser_settings.json (survives in-deploy restarts;
a full redeploy resets to defaults — documented, not silent).

Network modes:
  normal    — direct connection (always available)
  germany   — via GERMANY_PROXY_URL env (error if not configured)
  virginia  — via VIRGINIA_PROXY_URL env (error if not configured)
  tor       — policy-blocked on Railway; always reports unavailable, never
              silently falls back to another network.
"""
from __future__ import annotations

import json
import os

_SETTINGS_PATH = "/tmp/agent_browser_settings.json"

DEFAULT_ENGINES = {
    "google": {
        "name": "Google",
        "template": "https://www.google.com/search?q={q}",
    },
    "duckduckgo": {
        "name": "DuckDuckGo",
        "template": "https://duckduckgo.com/?q={q}",
    },
    "startpage": {
        "name": "Startpage",
        "template": "https://www.startpage.com/sp/search?query={q}",
    },
    "bing": {
        "name": "Bing",
        "template": "https://www.bing.com/search?q={q}",
    },
}

NETWORK_MODES = {
    "normal": {"name": "Normal Internet", "description": "Direct connection, no proxy."},
    "germany": {"name": "Germany proxy",
                "description": "Via GERMANY_PROXY_URL env var.",
                "env": "GERMANY_PROXY_URL"},
    "virginia": {"name": "US/Virginia proxy",
                 "description": "Via VIRGINIA_PROXY_URL env var.",
                 "env": "VIRGINIA_PROXY_URL"},
    "tor": {"name": "Tor Network",
            "description": "Policy-blocked on this host (Railway AUP). Unavailable.",
            "blocked": True,
            "blocked_reason": ("Tor is prohibited by the hosting provider's Acceptable Use "
                               "Policy (proxies/anonymization services). A Tor module cannot "
                               "be deployed here without risking the whole project.")},
}

_DEFAULTS = {"default_engine": "duckduckgo", "custom_engines": {}, "network_mode": "normal"}


def _load() -> dict:
    try:
        with open(_SETTINGS_PATH) as f:
            data = json.load(f)
        # Merge with defaults so new keys appear after upgrades.
        merged = dict(_DEFAULTS)
        merged.update(data)
        return merged
    except Exception:
        return dict(_DEFAULTS)


def _save(data: dict) -> None:
    tmp = _SETTINGS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, _SETTINGS_PATH)


def get_settings() -> dict:
    return _load()


def list_engines() -> dict:
    s = _load()
    engines = dict(DEFAULT_ENGINES)
    engines.update(s.get("custom_engines", {}))
    return engines


def get_default_engine() -> dict:
    s = _load()
    engines = list_engines()
    eid = s.get("default_engine", "duckduckgo")
    if eid not in engines:
        eid = "duckduckgo"
    return {"id": eid, **engines[eid]}


def set_default_engine(engine_id: str) -> dict:
    engines = list_engines()
    if engine_id not in engines:
        raise ValueError(f"unknown search engine '{engine_id}'")
    s = _load()
    s["default_engine"] = engine_id
    _save(s)
    return get_default_engine()


def add_search_engine(engine_id: str, name: str, template: str) -> dict:
    if engine_id in DEFAULT_ENGINES:
        raise ValueError(f"'{engine_id}' is a built-in engine and cannot be overwritten")
    if "{q}" not in template:
        raise ValueError("template must contain {q} as the query placeholder")
    if not (template.startswith("https://") or template.startswith("http://")):
        raise ValueError("template must start with https:// or http://")
    s = _load()
    customs = s.get("custom_engines", {})
    customs[engine_id] = {"name": name, "template": template}
    s["custom_engines"] = customs
    _save(s)
    return {"id": engine_id, "name": name, "template": template}


def remove_search_engine(engine_id: str) -> None:
    if engine_id in DEFAULT_ENGINES:
        raise ValueError(f"'{engine_id}' is built-in and cannot be removed")
    s = _load()
    customs = s.get("custom_engines", {})
    customs.pop(engine_id, None)
    s["custom_engines"] = customs
    if s.get("default_engine") == engine_id:
        s["default_engine"] = "duckduckgo"
    _save(s)


def get_network_mode() -> str:
    s = _load()
    mode = s.get("network_mode", "normal")
    return mode if mode in NETWORK_MODES else "normal"


def set_network_mode(mode: str) -> dict:
    if mode not in NETWORK_MODES:
        raise ValueError(f"unknown network mode '{mode}'")
    s = _load()
    s["network_mode"] = mode
    _save(s)
    return network_status()


def network_status() -> dict:
    """Status of every mode: available / not_configured / blocked + reason."""
    out = {}
    for mode, info in NETWORK_MODES.items():
        if info.get("blocked"):
            out[mode] = {"name": info["name"], "available": False,
                         "reason": info["blocked_reason"]}
        elif "env" in info:
            url = os.environ.get(info["env"], "")
            out[mode] = {"name": info["name"], "available": bool(url),
                         "reason": None if url else f"{info['env']} is not set"}
        else:
            out[mode] = {"name": info["name"], "available": True, "reason": None}
    return {"active": get_network_mode(), "modes": out}


def search_url(query: str, engine_id: str = "") -> str:
    from urllib.parse import quote_plus
    engines = list_engines()
    eid = engine_id or get_settings().get("default_engine", "duckduckgo")
    if eid not in engines:
        eid = "duckduckgo"
    return engines[eid]["template"].replace("{q}", quote_plus(query))
