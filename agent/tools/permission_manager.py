"""Permission Manager — central runtime authority for every permission.

Loads the full permission set from three sources:
  1. agent/security/policy.py  — the 155 granted policy items + 5 hard denials
  2. agent/configs/COMPUTER_CONTROL_PERMISSIONS.md — the 128 computer-control
     items (each backed by a real tool in agent/tools/)
  3. The central Tool Registry — one permission per registered tool
     (key "tool:<name>")

Default state: EVERYTHING enabled except the 5 denials, which are LOCKED
disabled at the execution layer (permission_gate.py) and can never be
self-granted (ahsan's standing order: no restrictions beyond the 5).

Toggle state persists in agent/configs/permission_state.json so enable/disable
choices survive restarts; missing state regenerates from defaults.

Public API:
    mgr = PermissionManager(registry)
    mgr.is_allowed("tool:file_delete")   # or bare "file_delete"
    mgr.check("file_delete", {"path": "x", "confirm": True})
        -> CheckResult(allowed, reason, needs_confirmation)
    mgr.enable("tool:screen_shot") / mgr.disable("tool:screen_shot")
    mgr.list_all() -> [ {key, category, title, enabled, locked}, ... ]
    mgr.reset_defaults()
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..security.policy import DENIALS, DENIAL_DESCRIPTIONS, ALL_GRANTED
from .permission_gate import check_tool_call

STATE_PATH = Path(__file__).resolve().parent.parent / "configs" / "permission_state.json"

# Tools whose execution is destructive: when the destructive-confirmation
# toggle is ON (default, per ahsan 2026-10-15), these require confirm=true.
DESTRUCTIVE_TOOLS = {
    "file_delete",
    "app_close",
    "app_uninstall",
    "process_kill",
    "memory_delete",
    "perm_revoke",
}

# service_control is destructive only for stop/restart actions.
DESTRUCTIVE_SERVICE_ACTIONS = {"stop", "restart", "disable"}

# Special safety toggles (not tools).
SAFETY_TOGGLES = {
    "safety:destructive_confirmation": (
        "Destructive-action confirmation",
        "Destructive tools require confirm=true. Toggle, default ON (ahsan 2026-10-15).",
        True,
    ),
    "safety:audit": (
        "Audit logging",
        "Security-relevant actions are audit-logged. Toggle, default ON.",
        True,
    ),
}


@dataclass
class Permission:
    key: str
    category: str
    title: str
    description: str = ""
    default_enabled: bool = True
    locked: bool = False  # locked permissions cannot be toggled (the 5 denials)


@dataclass
class CheckResult:
    allowed: bool
    reason: str = ""
    needs_confirmation: bool = False


class PermissionManager:
    """Central permission authority. Wire into Dispatcher before execution."""

    def __init__(self, registry=None, state_path: Path | None = None):
        self.registry = registry
        self.state_path = Path(state_path) if state_path else STATE_PATH
        self._permissions: dict[str, Permission] = {}
        self._state: dict[str, bool] = {}
        self._build_defaults()
        self._load_state()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _add(self, perm: Permission) -> None:
        self._permissions[perm.key] = perm

    def _build_defaults(self) -> None:
        # 1. The 5 hard denials — locked OFF, never toggleable.
        for d in DENIALS:
            self._add(Permission(
                key=f"denial:{d}",
                category="denial",
                title=d,
                description=DENIAL_DESCRIPTIONS.get(d, ""),
                default_enabled=False,
                locked=True,
            ))
        # 2. The 155 granted policy items — ON.
        for item in ALL_GRANTED:
            num = item[0]
            self._add(Permission(
                key=f"policy:{num}",
                category="policy",
                title=f"policy item {num}",
                description=f"{item[1]}: {item[2]}",
                default_enabled=True,
            ))
        # 3. Safety toggles — ON by default.
        for key, (title, desc, default) in SAFETY_TOGGLES.items():
            self._add(Permission(
                key=key, category="safety", title=title,
                description=desc, default_enabled=default,
            ))
        # 4. One permission per registered tool — ON.
        if self.registry is not None:
            for tool in self.registry.list():
                self._add(Permission(
                    key=f"tool:{tool.name}",
                    category=f"tool:{tool.permission_profile}",
                    title=tool.name,
                    description=tool.description,
                    default_enabled=True,
                ))

    def refresh_tools(self, registry) -> None:
        """Re-register tool permissions (call after new tools are added)."""
        self.registry = registry
        for tool in registry.list():
            key = f"tool:{tool.name}"
            if key not in self._permissions:
                self._add(Permission(
                    key=key,
                    category=f"tool:{tool.permission_profile}",
                    title=tool.name,
                    description=tool.description,
                    default_enabled=True,
                ))
        self._load_state()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _load_state(self) -> None:
        state: dict[str, bool] = {}
        try:
            if self.state_path.exists():
                raw = json.loads(self.state_path.read_text())
                if isinstance(raw, dict):
                    state = {str(k): bool(v) for k, v in raw.items()}
        except Exception:
            state = {}
        self._state = state
        # Locked denials are always OFF regardless of stored state.
        for d in DENIALS:
            self._state[f"denial:{d}"] = False
        try:
            self._state_mtime = self.state_path.stat().st_mtime
        except OSError:
            self._state_mtime = 0.0

    def _maybe_reload(self) -> None:
        """Pick up toggle changes written by other manager instances
        (e.g. perm_grant/perm_revoke tools), based on file mtime."""
        try:
            mtime = self.state_path.stat().st_mtime
        except OSError:
            return
        if mtime != getattr(self, "_state_mtime", 0.0):
            self._load_state()

    def save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            # Persist only deviations from defaults (keeps file small).
            deviations = {
                k: v for k, v in self._state.items()
                if k in self._permissions
                and v != self._permissions[k].default_enabled
            }
            self.state_path.write_text(json.dumps(deviations, indent=2, sort_keys=True))
            try:
                self._state_mtime = self.state_path.stat().st_mtime
            except OSError:
                pass
        except Exception:
            pass

    def reset_defaults(self) -> None:
        self._state = {}
        for d in DENIALS:
            self._state[f"denial:{d}"] = False
        self.save()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def _normalize(self, name: str) -> str:
        """Accept bare tool names ('file_delete') as 'tool:file_delete'."""
        if name in self._permissions:
            return name
        cand = f"tool:{name}"
        if cand in self._permissions:
            return cand
        return name

    def get(self, name: str) -> Permission | None:
        return self._permissions.get(self._normalize(name))

    def is_allowed(self, name: str) -> bool:
        self._maybe_reload()
        key = self._normalize(name)
        perm = self._permissions.get(key)
        if perm is None:
            return False  # unknown permission -> deny closed
        if perm.locked:
            return False
        return self._state.get(key, perm.default_enabled)

    def list_all(self) -> list[dict]:
        out = []
        for key, perm in sorted(self._permissions.items()):
            out.append({
                "key": key,
                "category": perm.category,
                "title": perm.title,
                "description": perm.description,
                "enabled": self.is_allowed(key),
                "locked": perm.locked,
                "default_enabled": perm.default_enabled,
            })
        return out

    def count_enabled(self) -> tuple[int, int]:
        """(enabled, total) excluding locked denials from 'enabled'."""
        total = len(self._permissions)
        enabled = sum(1 for k in self._permissions if self.is_allowed(k))
        return enabled, total

    # ------------------------------------------------------------------
    # Toggling
    # ------------------------------------------------------------------
    def enable(self, name: str) -> CheckResult:
        key = self._normalize(name)
        perm = self._permissions.get(key)
        if perm is None:
            return CheckResult(False, f"Unknown permission: {name}")
        if perm.locked:
            return CheckResult(
                False,
                f"Permission '{key}' is a locked denial and can never be enabled.",
            )
        self._state[key] = True
        self.save()
        return CheckResult(True, f"Enabled {key}")

    def disable(self, name: str) -> CheckResult:
        key = self._normalize(name)
        perm = self._permissions.get(key)
        if perm is None:
            return CheckResult(False, f"Unknown permission: {name}")
        if perm.locked:
            return CheckResult(False, f"Permission '{key}' is already locked OFF.")
        self._state[key] = False
        self.save()
        return CheckResult(True, f"Disabled {key}")

    # ------------------------------------------------------------------
    # Enforcement hook — call BEFORE tool execution
    # ------------------------------------------------------------------
    def _is_destructive(self, tool_name: str, args: dict) -> bool:
        if tool_name in DESTRUCTIVE_TOOLS:
            return True
        if tool_name == "service_control":
            action = str(args.get("action", "")).lower()
            return action in DESTRUCTIVE_SERVICE_ACTIONS
        return False

    def check(self, tool_name: str, args: dict) -> CheckResult:
        """Full pre-execution check.

        Order: 5 hard denials (absolute) -> permission state ->
        destructive-confirmation toggle.
        """
        # 1. The 5 denials are absolute — checked at the execution layer.
        gate = check_tool_call(tool_name, args or {})
        if not gate.allowed:
            return CheckResult(False, gate.reason)

        # 2. Permission state for this tool.
        if not self.is_allowed(f"tool:{tool_name}"):
            # Unknown tools (not in registry) are denied closed here; the
            # dispatcher reports "unknown tool" separately.
            perm = self.get(f"tool:{tool_name}")
            if perm is None:
                return CheckResult(False, f"Unknown tool: {tool_name}")
            return CheckResult(False, f"Permission disabled for tool '{tool_name}'.")

        # 3. Destructive-confirmation toggle (default ON).
        if self._is_destructive(tool_name, args or {}):
            if self.is_allowed("safety:destructive_confirmation"):
                if not (args or {}).get("confirm"):
                    return CheckResult(
                        False,
                        f"Destructive action '{tool_name}' requires confirm=true "
                        f"(destructive-confirmation toggle is ON).",
                        needs_confirmation=True,
                    )
        return CheckResult(True, "allowed")


def build_manager(registry=None, state_path: Path | None = None) -> PermissionManager:
    """Convenience constructor."""
    return PermissionManager(registry=registry, state_path=state_path)
