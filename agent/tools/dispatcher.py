"""Tool dispatcher — permission gate -> real execution -> structured result."""
from __future__ import annotations
import json
from dataclasses import dataclass
from .registry import ToolRegistry
from .permission_gate import check_tool_call
from ..execution.python_runner import run_python, ExecResult
from ..execution.shell_runner import run_shell


@dataclass
class DispatchResult:
    ok: bool
    output: str


class Dispatcher:
    def __init__(self, registry: ToolRegistry, workspace: str):
        self.registry = registry
        self.workspace = workspace

    def dispatch(self, name: str, args: dict) -> DispatchResult:
        tool = self.registry.get(name)
        if tool is None:
            return DispatchResult(ok=False,
                                  output=f"Unknown or disabled tool: {name}")
        gate = check_tool_call(name, args)
        if not gate.allowed:
            return DispatchResult(ok=False, output=gate.reason)
        try:
            return self._execute(name, args, tool.timeout)
        except Exception as e:
            return DispatchResult(ok=False, output=f"Execution error: {e}")

    def _execute(self, name: str, args: dict, timeout: int) -> DispatchResult:
        if name == "shell":
            r: ExecResult = run_shell(str(args.get("command", "")),
                                      timeout=timeout,
                                      workspace=self.workspace)
        elif name == "python":
            r = run_python(str(args.get("code", "")),
                           timeout=timeout, workspace=self.workspace)
        elif name == "pip":
            r = run_shell(f"pip install {args.get('package', '')}",
                          timeout=timeout, workspace=self.workspace)
        elif name == "file_read":
            return self._file_read(str(args.get("path", "")))
        elif name == "file_write":
            return self._file_write(str(args.get("path", "")),
                                    str(args.get("content", "")))
        elif name == "web_search":
            return self._web_search(str(args.get("query", "")))
        elif name == "germany_fetch":
            return self._germany_fetch(str(args.get("url", "")))
        else:
            return DispatchResult(
                ok=False,
                output=f"Tool '{name}' registered but executor not yet wired "
                       f"(browser/supabase/self_modify/tool_create land in later phases).")
        out = f"exit={r.exit_code} duration_ms={r.duration_ms}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
        return DispatchResult(ok=r.ok, output=out)

    def _file_read(self, path: str) -> DispatchResult:
        try:
            with open(path) as f:
                return DispatchResult(ok=True, output=f.read()[:20000])
        except Exception as e:
            return DispatchResult(ok=False, output=str(e))

    def _file_write(self, path: str, content: str) -> DispatchResult:
        try:
            with open(path, "w") as f:
                f.write(content)
            return DispatchResult(ok=True, output=f"Wrote {len(content)} bytes to {path}")
        except Exception as e:
            return DispatchResult(ok=False, output=str(e))

    def _web_search(self, query: str) -> DispatchResult:
        import urllib.request, urllib.parse
        try:
            url = ("https://html.duckduckgo.com/html/?q="
                   + urllib.parse.quote(query))
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                html = resp.read().decode("utf-8", "ignore")
            import re
            titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', html)[:8]
            clean = [re.sub(r"<[^>]+>", "", t).strip() for t in titles]
            return DispatchResult(ok=True,
                                  output="\n".join(f"- {t}" for t in clean if t))
        except Exception as e:
            return DispatchResult(ok=False, output=f"Search failed: {e}")

    def _germany_fetch(self, url: str) -> DispatchResult:
        import os, urllib.request
        proxy = os.environ.get("GERMANY_PROXY_URL", "")
        try:
            if proxy:
                req = urllib.request.Request(url)
                req.set_proxy(proxy, "http"); req.set_proxy(proxy, "https")
            else:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0",
                                  "X-Route-Note": "direct-fallback-no-germany-proxy"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read().decode("utf-8", "ignore")[:8000]
            note = "via GERMANY_PROXY" if proxy else "DIRECT (no Germany proxy configured)"
            return DispatchResult(ok=True, output=f"[{note}]\n{body}")
        except Exception as e:
            return DispatchResult(ok=False, output=f"Fetch failed: {e}")
