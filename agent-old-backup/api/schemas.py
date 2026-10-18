"""API schemas — request/response models (ChatGPT spec).

Per ChatGPT spec: agent/api/schemas.py
Dataclass-based DTOs with validation.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class ChatRequest:
    """POST /api/chat"""
    message: str
    session_id: Optional[str] = None
    stream: bool = False

    def validate(self) -> List[str]:
        errors = []
        if not self.message or not self.message.strip():
            errors.append("message is required")
        if len(self.message) > 50000:
            errors.append("message too long (max 50000 chars)")
        return errors

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ChatRequest":
        return cls(message=d.get("message", ""),
                   session_id=d.get("session_id"),
                   stream=bool(d.get("stream", False)))


@dataclass
class ChatResponse:
    """Response for /api/chat"""
    reply: str
    session_id: str
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    duration_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ToolCallRequest:
    """POST /api/tools/call"""
    name: str
    args: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> List[str]:
        errors = []
        if not self.name:
            errors.append("name is required")
        if not isinstance(self.args, dict):
            errors.append("args must be an object")
        return errors

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ToolCallRequest":
        return cls(name=d.get("name", ""), args=d.get("args", {}))


@dataclass
class ToolCallResponse:
    ok: bool
    output: str
    tool: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PlanRequest:
    """POST /api/plan"""
    goal: str

    def validate(self) -> List[str]:
        if not self.goal or not self.goal.strip():
            return ["goal is required"]
        return []

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PlanRequest":
        return cls(goal=d.get("goal", ""))


@dataclass
class ErrorResponse:
    error: str
    code: int = 400
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class HealthResponse:
    ok: bool = True
    agent: str = "haley-py"
    version: str = "2.0.0"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PaginatedResponse:
    items: List[Any]
    total: int
    page: int = 1
    per_page: int = 20

    def to_dict(self) -> Dict[str, Any]:
        return {"items": self.items, "total": self.total,
                "page": self.page, "per_page": self.per_page,
                "pages": max(1, (self.total + self.per_page - 1)
                             // self.per_page)}


def paginate(items: List[Any], page: int = 1,
             per_page: int = 20) -> PaginatedResponse:
    page = max(1, page)
    start = (page - 1) * per_page
    return PaginatedResponse(
        items=items[start:start + per_page],
        total=len(items), page=page, per_page=per_page)
