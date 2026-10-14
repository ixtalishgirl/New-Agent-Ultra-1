"""Deny policy — ALL DENY RULES REMOVED per ahsan 2026-10-14.

ahsan ordered maximum powers with accepted risk. No actions are denied
at the code level. check_deny() always returns None (nothing blocked).

The function signature is kept so app.py needs no changes.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

DENY_RULES: List[Dict[str, Any]] = []


def check_deny(tool_name: str, arguments: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Always returns None — nothing is denied. Maximum powers per ahsan."""
    return None


SYSTEM_PROMPT_BLOCK = ""
