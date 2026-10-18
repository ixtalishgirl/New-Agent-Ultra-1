"""
Powers — Clean unified interface giving the agent all powers.

Each method maps to the underlying tool. Clean, simple, no confusion.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agent.tools.registry import default_registry
from agent.tools.dispatcher import Dispatcher


class Powers:
    """All agent powers in one clean interface."""

    def __init__(self):
        self._registry = default_registry()
        try:
            self._dispatcher = Dispatcher(self._registry, Path.cwd())
        except TypeError:
            # Fallback for dispatcher signature variations
            self._dispatcher = None

    def _run(self, tool_name: str, **kwargs):
        """Execute a tool by name with kwargs."""
        if self._dispatcher is None:
            return {"success": False, "error": "Dispatcher not available"}
        try:
            return self._dispatcher.dispatch(tool_name, kwargs)
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── EXECUTION ──
    def shell(self, command: str, timeout: int = 60):
        """Run a bash/shell command."""
        return self._run("shell", command=command, timeout=timeout)

    def python(self, code: str, timeout: int = 60):
        """Run Python code."""
        return self._run("python", code=code, timeout=timeout)

    def pip(self, package: str):
        """Install a Python package."""
        return self._run("pip", package=package)

    # ── FILES ──
    def file_read(self, path: str):
        """Read a file."""
        return self._run("file_read", path=path)

    def file_write(self, path: str, content: str):
        """Write/create a file."""
        return self._run("file_write", path=path, content=content)

    def file_tree(self, path: str = "."):
        """List directory tree."""
        return self._run("file_tree", path=path)

    def file_find(self, pattern: str, path: str = "."):
        """Find files by pattern."""
        return self._run("file_find", pattern=pattern, path=path)

    def file_copy(self, src: str, dst: str):
        """Copy a file (via shell)."""
        return self.shell(f"cp '{src}' '{dst}'")

    def file_move(self, src: str, dst: str):
        """Move/rename a file (via shell)."""
        return self.shell(f"mv '{src}' '{dst}'")

    def file_delete(self, path: str):
        """Delete a file (via shell)."""
        return self.shell(f"rm '{path}'")

    # ── ARCHIVES (zip) ──
    def zip_create(self, archive: str, sources: list):
        """Create a zip archive."""
        src_str = " ".join(f"'{s}'" for s in sources)
        return self._run("archive_create", archive=archive, sources=sources)

    def zip_extract(self, archive: str, dest: str = "."):
        """Extract a zip archive."""
        return self._run("archive_extract", archive=archive, dest=dest)

    def zip_list(self, archive: str):
        """List zip contents."""
        return self._run("archive_list", archive=archive)

    # ── BROWSER ──
    def browser_go(self, url: str):
        """Navigate browser to URL."""
        return self._run("browser_navigate", url=url)

    def browser_shot(self, url: str = None):
        """Take browser screenshot."""
        kwargs = {"url": url} if url else {}
        return self._run("browser_screenshot", **kwargs)

    def browser_read(self, url: str = None):
        """Extract text from page."""
        kwargs = {"url": url} if url else {}
        return self._run("browser_extract", **kwargs)

    # ── NETWORK ──
    def web_search(self, query: str):
        """Search the web."""
        return self._run("web_search", query=query)

    def http_get(self, url: str):
        """HTTP GET request."""
        return self._run("http_get", url=url)

    def http_post(self, url: str, data: dict = None):
        """HTTP POST request."""
        return self._run("http_post", url=url, data=data or {})

    # ── GIT ──
    def git_status(self):
        return self._run("git_status")

    def git_commit(self, message: str):
        return self._run("git_commit", message=message)

    def git_push(self):
        return self.shell("git push origin main")

    # ── SELF-IMPROVEMENT ──
    def tool_create(self, name: str, description: str, code: str):
        """Create a new tool."""
        return self._run("tool_create", name=name, description=description, code=code)

    def self_modify(self, description: str):
        """Modify own code."""
        return self._run("self_modify", description=description)

    def bug_fix(self, file: str, error: str):
        """Attempt to fix a bug in a file."""
        # Read the file, analyze error, apply fix
        content = self.file_read(file)
        return {
            "file": file,
            "error": error,
            "content_preview": str(content)[:500],
            "note": "Use debugging/auto_fixer for full auto-fix",
        }

    # ── SYSTEM ──
    def sysinfo(self):
        return self._run("sysinfo")

    def disk_usage(self, path: str = "."):
        return self._run("disk_usage", path=path)

    # ── TEXT ──
    def text_search(self, pattern: str, path: str = "."):
        return self._run("text_search", pattern=pattern, path=path)

    def text_replace(self, path: str, old: str, new: str):
        return self._run("text_replace", path=path, old=old, new=new)

    # ── INFO ──
    def list_powers(self):
        """List all available powers."""
        return [t.name for t in self._registry.list()]

    def power_info(self, name: str):
        """Get info about a specific power."""
        for t in self._registry.list():
            if t.name == name:
                return {"name": t.name, "description": t.description, "version": t.version}
        return {"error": f"Power '{name}' not found"}
