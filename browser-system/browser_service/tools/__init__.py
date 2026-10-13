"""Loads and registers every tool module.

To add a new tool: create ``tools/my_tool.py`` with a
``register(registry, ctx_factory)`` function, then import + call it here.
"""
from registry import ToolRegistry, ToolContext

from . import (
    navigate,
    search,
    open_link,
    extract,
    scroll,
    paginate,
    fill_form,
    screenshot,
    page_state,
    tabs,
    download_file,
    detect_blocks,
)


def register_all(registry: ToolRegistry) -> None:
    for module in (
        navigate,
        search,
        open_link,
        extract,
        scroll,
        paginate,
        fill_form,
        screenshot,
        page_state,
        tabs,
        download_file,
        detect_blocks,
    ):
        module.register(registry)
