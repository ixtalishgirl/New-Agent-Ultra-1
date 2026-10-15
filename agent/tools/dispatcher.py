"""Tool dispatcher — permission gate -> real execution -> structured result."""
from __future__ import annotations
import json
from dataclasses import dataclass
from .registry import ToolRegistry
from .permission_gate import check_tool_call
from ..execution.python_runner import run_python, ExecResult
from ..execution.shell_runner import run_shell
from .git_tools import GitTools
from .file_tools import FileTools
from .network_tools import NetworkTools
from .data_tools import DataTools
from .system_tools import SystemTools
from . import media_tools
from . import archive_tools
from . import text_tools


@dataclass
class DispatchResult:
    ok: bool
    output: str


class Dispatcher:
    def __init__(self, registry: ToolRegistry, workspace: str,
                 permission_manager=None):
        self.registry = registry
        self.workspace = workspace
        # Central permission authority. When absent, a default manager is
        # built from the registry (all ON except the 5 locked denials).
        if permission_manager is None:
            from .permission_manager import PermissionManager
            permission_manager = PermissionManager(registry=registry)
        self.permission_manager = permission_manager
        self._git: GitTools | None = None
        self._git_failed = False
        self._files: FileTools | None = None
        self._net: NetworkTools | None = None
        self._data: DataTools | None = None
        self._sys: SystemTools | None = None
        self._cc_handlers: dict | None = None

    @property
    def cc_handlers(self) -> dict:
        """Lazily merged HANDLERS from computer-control tool modules.

        Modules that expose a module-level ``set_executor(fn)`` (e.g.
        memory_tools for workflow_run/branch_if/parallel_run) get this
        dispatcher's own ``dispatch`` bound, so nested tool calls flow
        through the same permission checks.
        """
        if self._cc_handlers is None:
            import importlib
            from .registry import collect_handlers, _COMPUTER_CONTROL_MODULES
            self._cc_handlers = collect_handlers()
            for mod_name in _COMPUTER_CONTROL_MODULES:
                try:
                    mod = importlib.import_module(mod_name)
                except Exception:
                    continue
                binder = getattr(mod, "set_executor", None)
                if callable(binder):
                    try:
                        binder(self.dispatch)
                    except Exception:
                        pass
        return self._cc_handlers

    # -- lazy tool-module accessors --------------------------------------
    @property
    def git(self) -> GitTools:
        if self._git is None and not self._git_failed:
            try:
                self._git = GitTools()
            except RuntimeError:
                self._git_failed = True
                raise
        if self._git is None:
            raise RuntimeError("git binary not available")
        return self._git

    @property
    def files(self) -> FileTools:
        if self._files is None:
            self._files = FileTools()
        return self._files

    @property
    def net(self) -> NetworkTools:
        if self._net is None:
            self._net = NetworkTools()
        return self._net

    @property
    def data(self) -> DataTools:
        if self._data is None:
            self._data = DataTools()
        return self._data

    @property
    def sys(self) -> SystemTools:
        if self._sys is None:
            self._sys = SystemTools()
        return self._sys

    def _media(self, name: str, args: dict) -> "DispatchResult":
        """Dispatch media tool calls."""
        import json as _json
        funcs = {
            "image_info": media_tools.image_info,
            "image_resize": lambda a: media_tools.image_resize(
                a["path"], a["width"], a["height"], a.get("output")),
            "image_convert": lambda a: media_tools.image_convert(
                a["path"], a["format"], a.get("output")),
            "image_thumbnail": lambda a: media_tools.image_thumbnail(
                a["path"], a.get("size", 128), a.get("output")),
            "audio_info": media_tools.audio_info,
            "video_info": media_tools.video_info,
        }
        fn = funcs.get(name)
        if not fn:
            return DispatchResult(ok=False, output=f"Unknown media tool: {name}")
        try:
            if name in ("image_info", "audio_info", "video_info"):
                result = fn(args.get("path", ""))
            else:
                result = fn(args)
            ok = result.get("ok", True)
            return DispatchResult(ok=ok, output=_json.dumps(result, indent=2, default=str))
        except Exception as e:
            return DispatchResult(ok=False, output=f"Media tool error: {e}")

    def _archive(self, name: str, args: dict) -> "DispatchResult":
        """Dispatch archive tool calls."""
        import json as _json
        try:
            if name == "archive_create":
                result = archive_tools.archive_create(
                    args.get("paths", []), args.get("output", ""),
                    args.get("format", "zip"))
            elif name == "archive_extract":
                result = archive_tools.archive_extract(
                    args.get("archive", ""), args.get("dest"))
            elif name == "archive_list":
                result = archive_tools.archive_list(
                    args.get("archive", ""))
            elif name == "archive_info":
                result = archive_tools.archive_info(
                    args.get("archive", ""))
            elif name == "gzip_compress":
                result = archive_tools.gzip_compress(
                    args.get("path", ""), args.get("output"))
            elif name == "gzip_decompress":
                result = archive_tools.gzip_decompress(
                    args.get("path", ""), args.get("output"))
            else:
                return DispatchResult(ok=False,
                                      output=f"Unknown archive tool: {name}")
            ok = result.get("ok", True)
            return DispatchResult(
                ok=ok, output=_json.dumps(result, indent=2, default=str))
        except Exception as e:
            return DispatchResult(ok=False,
                                  output=f"Archive tool error: {e}")

    def _text(self, name: str, args: dict) -> "DispatchResult":
        """Dispatch text tool calls."""
        import json as _json
        try:
            if name == "text_search":
                result = text_tools.text_search(
                    args.get("pattern", ""), args.get("path", ""),
                    args.get("regex", False),
                    args.get("case_sensitive", True))
            elif name == "text_replace":
                result = text_tools.text_replace(
                    args.get("path", ""), args.get("old", ""),
                    args.get("new", ""), args.get("regex", False))
            elif name == "text_stats":
                result = text_tools.text_stats(args.get("path", ""))
            elif name == "text_diff":
                result = text_tools.text_diff(
                    args.get("text1", ""), args.get("text2", ""))
            else:
                return DispatchResult(ok=False,
                                      output=f"Unknown text tool: {name}")
            ok = result.get("ok", True)
            return DispatchResult(
                ok=ok, output=_json.dumps(result, indent=2, default=str))
        except Exception as e:
            return DispatchResult(ok=False,
                                  output=f"Text tool error: {e}")

    @staticmethod
    def _wrap(result) -> "DispatchResult":
        """Convert a GitResult/FileResult/NetResult/DataResult/SysResult
        into a DispatchResult, appending parsed JSON for machine use."""
        out = result.output or ""
        parsed = getattr(result, "parsed", None)
        if parsed:
            try:
                out += "\n[parsed]\n" + json.dumps(parsed, indent=2,
                                                   default=str)[:8000]
            except Exception:
                pass
        err = getattr(result, "error", "")
        if not result.ok and err and err not in out:
            out += f"\nError: {err}"
        return DispatchResult(ok=bool(result.ok), output=out)

    def dispatch(self, name: str, args: dict) -> DispatchResult:
        tool = self.registry.get(name)
        if tool is None:
            return DispatchResult(ok=False,
                                  output=f"Unknown or disabled tool: {name}")
        # Central permission check: 5 hard denials (absolute) -> permission
        # toggle state -> destructive-confirmation toggle.
        check = self.permission_manager.check(name, args or {})
        if not check.allowed:
            if check.needs_confirmation:
                return DispatchResult(
                    ok=False,
                    output=f"CONFIRMATION_REQUIRED: {check.reason}")
            return DispatchResult(ok=False, output=check.reason)
        try:
            return self._execute(name, args, tool.timeout)
        except Exception as e:
            return DispatchResult(ok=False, output=f"Execution error: {e}")

    @staticmethod
    def _exec_result(r) -> "DispatchResult":
        out = (f"exit={r.exit_code} duration_ms={r.duration_ms}\n"
               f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}")
        return DispatchResult(ok=r.ok, output=out)

    def _execute(self, name: str, args: dict, timeout: int) -> DispatchResult:
        if name == "shell":
            r: ExecResult = run_shell(str(args.get("command", "")),
                                      timeout=timeout,
                                      workspace=self.workspace)
            return self._exec_result(r)
        elif name == "python":
            r = run_python(str(args.get("code", "")),
                           timeout=timeout, workspace=self.workspace)
            return self._exec_result(r)
        elif name == "pip":
            r = run_shell(f"pip install {args.get('package', '')}",
                          timeout=timeout, workspace=self.workspace)
            return self._exec_result(r)
        elif name == "file_read":
            return self._file_read(str(args.get("path", "")))
        elif name == "file_write":
            return self._file_write(str(args.get("path", "")),
                                    str(args.get("content", "")))
        elif name == "web_search":
            return self._web_search(str(args.get("query", "")))
        elif name == "germany_fetch":
            return self._germany_fetch(str(args.get("url", "")))
        elif name == "browser_navigate":
            return self._browser("navigate", args)
        elif name == "browser_screenshot":
            return self._browser("screenshot", args)
        elif name == "browser_extract":
            return self._browser("extract", args)
        elif name == "tool_create":
            return self._tool_create(args)
        # ---- git tools ----
        elif name == "git_status":
            return self._wrap(self.git.status(path=args.get("path") or None))
        elif name == "git_log":
            return self._wrap(self.git.log(
                path=args.get("path") or None,
                max_count=int(args.get("max_count", 20)),
                branch=str(args.get("branch", "")),
                since=str(args.get("since", "")),
                author=str(args.get("author", "")),
                grep=str(args.get("grep", "")),
                pathspec=str(args.get("pathspec", ""))))
        elif name == "git_diff":
            return self._wrap(self.git.diff(
                path=args.get("path") or None,
                staged=bool(args.get("staged", False)),
                commit_a=str(args.get("commit_a", "")),
                commit_b=str(args.get("commit_b", "")),
                pathspec=str(args.get("pathspec", ""))))
        elif name == "git_branch":
            return self._git_branch(args)
        elif name == "git_commit":
            return self._git_commit(args)
        # ---- file tools ----
        elif name == "file_tree":
            return self._wrap(self.files.list_tree(
                path=args.get("path", "."),
                max_depth=int(args.get("max_depth", 5)),
                include_hidden=bool(args.get("include_hidden", False)),
                extensions=args.get("extensions")))
        elif name == "file_find":
            return self._wrap(self.files.find_by_pattern(
                root=args.get("root", "."),
                name_glob=str(args.get("name_glob", "*")),
                content_regex=str(args.get("content_regex", "")),
                max_results=int(args.get("max_results", 200)),
                extensions=args.get("extensions")))
        elif name == "file_checksum":
            return self._wrap(self.files.checksum(
                str(args.get("path", "")),
                algorithm=str(args.get("algorithm", "sha256"))))
        # ---- network tools ----
        elif name == "http_get":
            return self._wrap(self.net.http_get(
                str(args.get("url", "")),
                headers=args.get("headers"),
                params=args.get("params"),
                timeout=int(args.get("timeout", 20)) or None))
        elif name == "http_post":
            return self._wrap(self.net.http_post(
                str(args.get("url", "")),
                data=args.get("data"),
                json_data=args.get("json_data"),
                headers=args.get("headers"),
                timeout=int(args.get("timeout", 20)) or None))
        elif name == "dns_lookup":
            return self._wrap(self.net.dns_lookup(str(args.get("hostname", ""))))
        elif name == "port_check":
            ports = args.get("ports", [])
            if isinstance(ports, str):
                ports = [int(p.strip()) for p in ports.split(",") if p.strip()]
            return self._wrap(self.net.port_check(
                str(args.get("host", "")),
                [int(p) for p in ports]))
        # ---- data tools ----
        elif name == "json_validate":
            return self._wrap(self.data.json_parse_validate(
                str(args.get("text", "")),
                schema=args.get("schema")))
        elif name == "csv_stats":
            return self._csv_stats(args)
        # ---- system tools ----
        elif name == "sysinfo":
            return self._wrap(self.sys.sysinfo())
        elif name == "disk_usage":
            return self._wrap(self.sys.disk_usage(str(args.get("path", "/"))))
        elif name in ("image_info", "image_resize", "image_convert",
                      "image_thumbnail", "audio_info", "video_info"):
            return self._media(name, args)
        elif name in ("archive_create", "archive_extract",
                      "archive_list", "archive_info",
                      "gzip_compress", "gzip_decompress"):
            return self._archive(name, args)
        elif name in ("text_search", "text_replace", "text_stats",
                      "text_diff"):
            return self._text(name, args)
        else:
            return self._computer_control(name, args)

    def _computer_control(self, name: str, args: dict) -> DispatchResult:
        """Generic dispatch for computer-control tool modules.

        Each module exposes HANDLERS[name](args) -> result with
        .ok/.output/.parsed/.error. Merged lazily via registry.collect_handlers().
        """
        import json as _json
        fn = self.cc_handlers.get(name)
        if fn is None:
            return DispatchResult(
                ok=False,
                output=f"Tool '{name}' registered but executor not yet wired "
                       f"(browser/supabase/self_modify land in later phases).")
        try:
            result = fn(args or {})
            ok = bool(getattr(result, "ok", False))
            output = str(getattr(result, "output", "") or "")
            parsed = getattr(result, "parsed", None)
            if parsed:
                try:
                    output += "\n[parsed]\n" + _json.dumps(
                        parsed, indent=2, default=str)[:8000]
                except Exception:
                    pass
            err = getattr(result, "error", "")
            if not ok and err and err not in output:
                output += f"\nError: {err}"
            return DispatchResult(ok=ok, output=output)
        except Exception as e:
            return DispatchResult(
                ok=False, output=f"Computer-control tool error: {e}")

    def _git_branch(self, args: dict) -> DispatchResult:
        """git_branch dispatcher: action=list|create|delete|checkout."""
        action = str(args.get("action", "list")).lower()
        path = args.get("path") or None
        if action == "list":
            return self._wrap(self.git.branch_list(
                path=path,
                include_remote=bool(args.get("include_remote", False))))
        if action == "create":
            name = str(args.get("name", ""))
            if not name:
                return DispatchResult(ok=False, output="git_branch create needs 'name'")
            return self._wrap(self.git.branch_create(
                name,
                start_point=str(args.get("start_point", "HEAD")),
                checkout_after=bool(args.get("checkout_after", False)),
                path=path))
        if action == "delete":
            name = str(args.get("name", ""))
            if not name:
                return DispatchResult(ok=False, output="git_branch delete needs 'name'")
            return self._wrap(self.git.branch_delete(
                name, force=bool(args.get("force", False)), path=path))
        if action == "checkout":
            ref = str(args.get("name") or args.get("ref", ""))
            if not ref:
                return DispatchResult(ok=False, output="git_branch checkout needs 'name'/'ref'")
            return self._wrap(self.git.checkout(
                ref, create=bool(args.get("create", False)), path=path))
        return DispatchResult(ok=False,
                              output=f"Unknown git_branch action: {action} "
                                     f"(list|create|delete|checkout)")

    def _git_commit(self, args: dict) -> DispatchResult:
        """git_commit dispatcher: optionally stage `add` paths first, then commit."""
        path = args.get("path") or None
        message = str(args.get("message", ""))
        if not message.strip():
            return DispatchResult(ok=False, output="git_commit needs 'message'")
        staged: list[str] = []
        add = args.get("add")
        if add:
            specs = [add] if isinstance(add, str) else list(add)
            r = self.git.add(specs, path=path)
            if not r.ok:
                return self._wrap(r)
            staged = specs
        r = self.git.commit(
            message, path=path,
            amend=bool(args.get("amend", False)),
            allow_empty=bool(args.get("allow_empty", False)),
            author=str(args.get("author", "")))
        out = self._wrap(r)
        if staged and out.ok:
            out.output = f"Staged {staged}, then committed.\n" + out.output
        return out

    def _csv_stats(self, args: dict) -> DispatchResult:
        """csv_stats dispatcher: read CSV from text/path, then compute stats."""
        text = str(args.get("text", ""))
        path = args.get("path") or None
        if not text and not path:
            return DispatchResult(ok=False,
                                  output="csv_stats needs 'text' or 'path'")
        rr = self.data.csv_read(text=text, path=path)
        if not rr.ok:
            return self._wrap(rr)
        cols = args.get("columns")
        return self._wrap(self.data.csv_stats(rr.parsed["records"],
                                              columns=cols))

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

    def _browser(self, action: str, args: dict) -> DispatchResult:
        """Real Chromium automation via PlaywrightRunner (subprocess-isolated).

        browser_navigate {url} -> title/url/text snippet
        browser_screenshot {url} -> base64 PNG (data URI) for live viewing
        browser_extract {url} -> page text content
        Fails gracefully when playwright/chromium is not installed.
        """
        try:
            from ..browser.playwright_runner import PlaywrightRunner
        except Exception as e:
            return DispatchResult(ok=False, output=f"Browser unavailable: {e}")
        url = str(args.get("url", ""))
        if not url:
            return DispatchResult(ok=False, output="browser tool needs 'url'")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        try:
            runner = PlaywrightRunner(timeout=int(args.get("timeout", 60)))
            if action == "navigate":
                res = runner.navigate(url)
                err = res.get("error", "")
                if err:
                    if "ModuleNotFoundError" in err or "No module named 'playwright'" in err:
                        return DispatchResult(ok=False, output="Playwright not installed on this host — browser automation unavailable here.")
                    return DispatchResult(ok=False, output=err[:300])
                return DispatchResult(
                    ok=True,
                    output=json.dumps({
                        "title": res.get("title", ""),
                        "url": res.get("url", url),
                        "text": (res.get("text", "") or "")[:4000],
                    }, indent=1))
            if action == "extract":
                res = runner.navigate(url)
                err = res.get("error", "")
                if err:
                    if "ModuleNotFoundError" in err or "No module named 'playwright'" in err:
                        return DispatchResult(ok=False, output="Playwright not installed on this host — browser automation unavailable here.")
                    return DispatchResult(ok=False, output=err[:300])
                return DispatchResult(ok=True, output=(res.get("text", "") or "")[:12000])
            # screenshot -> base64 data URI so the dashboard can <img> it live
            import base64, os, tempfile
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            tmp.close()
            try:
                res = runner.screenshot(url, path=tmp.name)
                err = res.get("error", "")
                if err:
                    if "ModuleNotFoundError" in err or "playwright" in err.lower():
                        return DispatchResult(ok=False, output="Playwright not installed on this host — browser automation unavailable here.")
                    return DispatchResult(ok=False, output=err[:300])
                if not os.path.exists(tmp.name):
                    return DispatchResult(ok=False, output="screenshot failed (no file)")
                with open(tmp.name, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                return DispatchResult(
                    ok=True,
                    output=json.dumps({
                        "data_uri": "data:image/png;base64," + b64,
                        "url": url,
                    }))
            finally:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
        except Exception as e:
            return DispatchResult(ok=False, output=f"Browser error: {e}")

    def _tool_create(self, args: dict) -> DispatchResult:
        """Create a REAL tool the agent itself uses — like an assistant's tools.

        Writes agent/tools/custom/<name>.py, registers it in the central
        ToolRegistry IMMEDIATELY (usable in this same session via
        Dispatcher.dispatch), appends to custom/manifest.json, and backs the
        code up to Supabase `custom_tools` (Render disk is ephemeral).

        Contract for `code`: must define run(args: dict) -> dict.
        The 5 hard denials are enforced on the code by permission_gate
        (checked in dispatch() before this runs).
        """
        from . import custom_tools as ct
        name = str(args.get("name", "")).strip()
        code = str(args.get("code", ""))
        description = str(args.get("description", "") or f"Generated tool: {name}")
        schema = args.get("input_schema") or args.get("schema") or {"type": "object"}
        if not isinstance(schema, dict):
            return DispatchResult(ok=False, output="input_schema must be an object")
        if not name or not code:
            return DispatchResult(ok=False, output="tool_create needs 'name' and 'code'")
        try:
            safe, path = ct.save_custom_tool(name, description, schema, code)
        except ValueError as e:
            return DispatchResult(ok=False, output=f"tool_create rejected: {e}")
        except Exception as e:
            return DispatchResult(ok=False, output=f"tool_create failed: {e}")
        # Immediate activation in this session: registry + handlers + perms.
        try:
            mod = ct.load_custom_module(safe)
            from .registry import ToolDef
            for item in getattr(mod, "TOOL_DEFS", []) or []:
                tname, desc, tschema = item[0], item[1], item[2]
                profile = item[3] if len(item) > 3 else "custom"
                timeout = item[4] if len(item) > 4 else 60
                if self.registry.get(tname) is None:
                    self.registry.register(ToolDef(
                        name=tname, version="1.0.0", description=desc,
                        input_schema=tschema, permission_profile=profile,
                        timeout=timeout))
            handlers = self.cc_handlers  # ensure lazy cache exists
            mh = getattr(mod, "HANDLERS", None)
            if isinstance(mh, dict):
                handlers.update(mh)
            try:
                self.permission_manager.refresh_tools(self.registry)
            except Exception:
                pass
        except Exception as e:
            return DispatchResult(
                ok=False,
                output=f"tool_create saved to {path} but activation failed: {e}")
        bak_ok, bak_note = ct.backup_to_supabase(safe, code, schema, description)
        return DispatchResult(
            ok=True,
            output=json.dumps({
                "name": safe,
                "version": "1.0.0",
                "description": description,
                "path": str(path),
                "active": True,
                "supabase_backup": bak_ok,
                "note": ("registered and callable immediately via "
                         "Dispatcher.dispatch; ") + bak_note,
            }, indent=1))

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
