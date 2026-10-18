"""
Haley Powers — Clean unified powers interface for the agent.

Gives the agent ALL powers in a clean way, like Haley Noor (Muse) has:
- Execute code (shell, python, pip)
- Files (read, write, find, copy, move)
- Archives (zip create/extract)
- Browser (navigate, screenshot, extract)
- Network (web search, http, dns)
- Git (full version control)
- Tools (create new tools, self-modify)
- System (info, monitoring)
- Debug (bug fix, auto-repair)

Usage:
    from agent.powers import Powers
    powers = Powers()
    powers.shell("ls -la")
    powers.python("print('hello')")
    powers.file_read("/path/to/file")
"""

from agent.powers.core import Powers

__all__ = ["Powers"]
