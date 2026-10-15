"""API module — HTTP API server with authentication."""
from .authentication import AuthManager, ApiToken, RateLimiter
from .schemas import (
    ChatRequest, ChatResponse, ToolCallRequest, ToolCallResponse,
    PlanRequest, ErrorResponse, HealthResponse,
    PaginatedResponse, paginate,
)
from .endpoints import (
    Router, APIContext, build_router, API_DOCS,
)

__all__ = [
    "AuthManager", "ApiToken", "RateLimiter",
    "ChatRequest", "ChatResponse", "ToolCallRequest", "ToolCallResponse",
    "PlanRequest", "ErrorResponse", "HealthResponse",
    "PaginatedResponse", "paginate",
    "Router", "APIContext", "build_router", "API_DOCS",
]
