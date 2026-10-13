"""Pluggable vision adapters. Import the base class and any concrete adapters."""

try:  # used as a package (vision/ on sys.path)
    from adapters.base_adapter import (
        AdapterError,
        BaseVisionAdapter,
        get_adapter,
        register_adapter,
    )
except ImportError:  # used from inside vision/ directly
    from .base_adapter import (  # type: ignore
        AdapterError,
        BaseVisionAdapter,
        get_adapter,
        register_adapter,
    )

__all__ = ["BaseVisionAdapter", "AdapterError", "get_adapter", "register_adapter"]
