"""Base vision-model adapter interface + registry.

A vision adapter answers one question:

    describe_image(png_bytes, prompt) -> str

It takes raw PNG bytes and a prompt, and returns the model's plain-text
description. Everything else (stamping URL/timestamp, merging DOM facts,
marking content untrusted) lives in `pipeline.py`.

To add a new backend:
    1. Subclass BaseVisionAdapter and implement describe_image().
    2. Register it with a name via register_adapter("myname", MyAdapter).
    3. Set VISION_ADAPTER=myname (plus backend-specific env vars).

The default configured adapter is chosen by get_adapter() from the
VISION_ADAPTER env var. If nothing is configured / available, get_adapter()
returns None and the pipeline runs in text-only fallback mode.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Callable, Dict, Optional, Type

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class AdapterError(RuntimeError):
    """Raised when a vision adapter cannot describe an image."""


class BaseVisionAdapter(ABC):
    """Interface every vision backend must implement."""

    # Human-readable name, e.g. "openai-compatible".
    name: str = "base"

    @abstractmethod
    def describe_image(self, png_bytes: bytes, prompt: str) -> str:
        """Return a plain-text description of the PNG image.

        Args:
            png_bytes: raw PNG image bytes (must start with the PNG magic).
            prompt: the describe-only prompt (already injection-guarded by
                the pipeline; adapters must pass it through unchanged).

        Raises:
            ValueError: if png_bytes is not a PNG.
            AdapterError: if the backend call fails.
        """
        raise NotImplementedError

    def check_png(self, png_bytes: bytes) -> None:
        if not isinstance(png_bytes, bytes) or png_bytes[:8] != PNG_MAGIC:
            raise ValueError("png_bytes must be valid PNG image bytes")


# ---------------------------------------------------------------------------
# Registry + env-based selection
# ---------------------------------------------------------------------------

_ADAPTERS: Dict[str, Type[BaseVisionAdapter]] = {}
_FACTORY_OVERRIDES: Dict[str, Callable[[], BaseVisionAdapter]] = {}


def register_adapter(name: str, cls: Type[BaseVisionAdapter]) -> None:
    """Register an adapter class under a short name (used by VISION_ADAPTER)."""
    _ADAPTERS[name.lower()] = cls


def register_adapter_factory(
    name: str, factory: Callable[[], BaseVisionAdapter]
) -> None:
    """Register a zero-arg factory (useful for adapters needing config)."""
    _FACTORY_OVERRIDES[name.lower()] = factory


def available_adapters() -> list[str]:
    return sorted(set(_ADAPTERS) | set(_FACTORY_OVERRIDES))


def get_adapter(name: Optional[str] = None) -> Optional[BaseVisionAdapter]:
    """Build the configured adapter, or None if none is configured.

    Selection order:
      1. `name` argument, else VISION_ADAPTER env var.
      2. If unset/empty -> None (pipeline falls back to text-only).
      3. If the named adapter is registered, instantiate it; construction
         errors (e.g. missing VISION_MODEL_URL) are swallowed -> None,
         so the pipeline degrades gracefully instead of crashing.

    This module intentionally does NOT import concrete adapters — they
    self-register on import. Import them before calling get_adapter(),
    e.g. `import adapters.adapter_openai_compat`.
    """
    key = (name or os.environ.get("VISION_ADAPTER", "")).strip().lower()
    if not key:
        return None
    try:
        if key in _FACTORY_OVERRIDES:
            return _FACTORY_OVERRIDES[key]()
        cls = _ADAPTERS.get(key)
        if cls is None:
            return None
        return cls()
    except Exception:
        # Misconfiguration must degrade to text-only, never crash the agent.
        return None
