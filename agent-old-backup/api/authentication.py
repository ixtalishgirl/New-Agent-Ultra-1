"""API authentication — token-based auth for the local API.

Per ChatGPT spec: agent/api/authentication.py
Bearer tokens. Timing-safe comparison. Per ahsan: single-user agent,
so a single API token model is sufficient.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class ApiToken:
    """An API token."""
    token_hash: str
    name: str
    created_at: float = field(default_factory=time.time)
    last_used: Optional[float] = None
    revoked: bool = False


class AuthManager:
    """Manages API tokens."""

    def __init__(self):
        self._tokens: Dict[str, ApiToken] = {}
        # Bootstrap from env if present
        env_token = os.environ.get("HALEY_API_TOKEN", "")
        if env_token:
            self._tokens[self._hash(env_token)] = ApiToken(
                token_hash=self._hash(env_token),
                name="env-bootstrap")

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def create_token(self, name: str = "") -> str:
        """Generate a new token. Returns the raw token (show once)."""
        raw = secrets.token_urlsafe(32)
        self._tokens[self._hash(raw)] = ApiToken(
            token_hash=self._hash(raw),
            name=name or f"token-{len(self._tokens) + 1}")
        return raw

    def verify(self, token: str) -> bool:
        """Timing-safe token verification."""
        if not token:
            return False
        h = self._hash(token)
        # Constant-time lookup over stored hashes
        match = None
        for stored in self._tokens:
            if hmac.compare_digest(stored, h):
                match = stored
                break
        if match is None:
            return False
        tok = self._tokens[match]
        if tok.revoked:
            return False
        tok.last_used = time.time()
        return True

    def revoke(self, name: str) -> bool:
        for tok in self._tokens.values():
            if tok.name == name and not tok.revoked:
                tok.revoked = True
                return True
        return False

    def list_tokens(self) -> list:
        return [{"name": t.name, "created_at": t.created_at,
                 "last_used": t.last_used, "revoked": t.revoked}
                for t in self._tokens.values()]

    @staticmethod
    def extract_bearer(auth_header: str) -> str:
        """Extract token from 'Bearer <token>' header."""
        if not auth_header:
            return ""
        parts = auth_header.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        return ""


class RateLimiter:
    """Simple in-memory rate limiter (token bucket per key)."""

    def __init__(self, max_requests: int = 100,
                 window_seconds: int = 60):
        self.max_requests = max_requests
        self.window = window_seconds
        self._hits: Dict[str, list] = {}

    def allow(self, key: str) -> bool:
        now = time.time()
        hits = self._hits.get(key, [])
        hits = [h for h in hits if now - h < self.window]
        if len(hits) >= self.max_requests:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        return True

    def remaining(self, key: str) -> int:
        now = time.time()
        hits = [h for h in self._hits.get(key, [])
                if now - h < self.window]
        return max(0, self.max_requests - len(hits))
