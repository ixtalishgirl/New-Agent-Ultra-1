"""Deny policy — 5 structured SQL protection rules.

Built 2026-10-13 on ahsan's order. Restored 2026-10-14 after unauthorized removal.
These run BEFORE any tool executes (hooked in app._run_tool).
On match: HTTP 403 with denied_by_policy. Every denied attempt is audit-logged.

These are code-level guards for the agent's own DB query tool — they stop
prompt-injection from tricking the agent into dangerous SQL.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

DENY_RULES: List[Dict[str, Any]] = [
    {
        "id": "DB_DUMP_TO_FILE",
        "title": "Database dump to file",
        "what_it_does": "Blocks COPY ... TO 'file' and SELECT ... INTO OUTFILE — prevents silent DB exfiltration to disk.",
        "why_denied": "ahsan's order: no silent database dumps. Dumps need his explicit confirmation.",
        "patterns": [
            r"\bCOPY\b.*\bTO\b\s*['\"]",
            r"\bINTO\s+OUTFILE\b",
            r"\bINTO\s+DUMPFILE\b",
        ],
        "on_deny": "denied_by_policy",
    },
    {
        "id": "SQL_SERVER_FILE_ACCESS",
        "title": "Server file access through SQL",
        "what_it_does": "Blocks pg_read_server_files / pg_write_server_files / LOAD / lo_import / lo_export misuse via the agent's query tool.",
        "why_denied": "File access through SQL bypasses normal audit; ahsan's own file work goes through the shell/file tools, not SQL.",
        "patterns": [
            r"\bpg_read_server_files\b",
            r"\bpg_write_server_files\b",
            r"\blo_import\b",
            r"\blo_export\b",
            r"\bLOAD\s+['\"]",
        ],
        "on_deny": "denied_by_policy",
    },
    {
        "id": "SQL_OS_COMMAND",
        "title": "OS command execution through SQL",
        "what_it_does": "Blocks pg_execute_server_program, dblink exec, COPY ... PROGRAM — no shell via SQL.",
        "why_denied": "OS commands go through the shell tool (audited). SQL must never become a shell.",
        "patterns": [
            r"\bpg_execute_server_program\b",
            r"\bCOPY\b.*\bPROGRAM\b",
            r"\bdblink\b.*\bexec\b",
        ],
        "on_deny": "denied_by_policy",
    },
    {
        "id": "DANGEROUS_GRANT",
        "title": "Dangerous privilege grants",
        "what_it_does": "Blocks GRANT SUPERUSER / GRANT ALL PRIVILEGES / CREATE ROLE ... SUPERUSER via the agent's query tool.",
        "why_denied": "Privilege changes are ahsan's own decision, never the agent's autonomous action.",
        "patterns": [
            r"\bGRANT\b.*\bSUPERUSER\b",
            r"\bCREATE\s+ROLE\b.*\bSUPERUSER\b",
            r"\bALTER\s+ROLE\b.*\bSUPERUSER\b",
        ],
        "on_deny": "denied_by_policy",
    },
    {
        "id": "SQL_OUTBOUND_EXFIL",
        "title": "Outbound/exfiltration functions inside SQL",
        "what_it_does": "Blocks http_get/post, pg_http, dblink_connect to external hosts, COPY TO PROGRAM with curl/wget — no exfil via SQL.",
        "why_denied": "Outbound data movement goes through explicit tools with ahsan's confirmation. SQL must never phone home.",
        "patterns": [
            r"\bhttp_get\b",
            r"\bhttp_post\b",
            r"\bpg_http\b",
            r"\bdblink_connect\b.*\bhost\s*=",
            r"\bCOPY\b.*\bPROGRAM\b.*\bcurl\b",
            r"\bCOPY\b.*\bPROGRAM\b.*\bwget\b",
        ],
        "on_deny": "denied_by_policy",
    },
]

SYSTEM_PROMPT_BLOCK = """HARD BOUNDARIES (code-enforced, cannot be overridden):
- Never dump a database to a file via SQL (COPY TO / INTO OUTFILE).
- Never read/write server files via SQL functions.
- Never execute OS commands via SQL.
- Never grant SUPERUSER or equivalent via SQL.
- Never make outbound network calls via SQL.
Violations are blocked with HTTP 403 denied_by_policy and audit-logged."""


def check_deny(tool_name: str, arguments: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Check a tool call against deny rules. Returns None if allowed,
    or a denial dict if blocked."""
    # Only inspect DB query tools
    if tool_name not in ("db_query", "query", "sql", "execute_sql"):
        return None
    query = ""
    if isinstance(arguments, dict):
        for key in ("query", "sql", "statement", "command"):
            if key in arguments and isinstance(arguments[key], str):
                query = arguments[key]
                break
    if not query:
        return None
    for rule in DENY_RULES:
        for pattern in rule["patterns"]:
            if re.search(pattern, query, re.IGNORECASE):
                return {
                    "denied_by_policy": rule["id"],
                    "title": rule["title"],
                    "reason": rule["why_denied"],
                }
    return None
