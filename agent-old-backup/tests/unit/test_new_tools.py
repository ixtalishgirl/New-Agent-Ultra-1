"""Real tests for the Phase-2 tool modules: git, file, network, data, system.

- File/git tests run against fresh temp dirs (never the real repo).
- Network tests target example.com with short timeouts and are SKIPPED
  entirely when the sandbox has no outbound connectivity.
- All assertions check real behavior, not mocks.
"""
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from agent.tools.git_tools import GitTools, GitResult
from agent.tools.file_tools import FileTools, FileResult
from agent.tools.network_tools import NetworkTools, NetResult
from agent.tools.data_tools import DataTools, DataResult
from agent.tools.system_tools import SystemTools, SysResult
from agent.tools.registry import default_registry
from agent.tools.dispatcher import Dispatcher

GIT_AVAILABLE = shutil.which("git") is not None


def _network_available() -> bool:
    """Quick connectivity probe: TCP to example.com:80 within 4s."""
    try:
        with socket.create_connection(("example.com", 80), timeout=4):
            return True
    except OSError:
        return False


NETWORK_AVAILABLE = _network_available()


# ---------------------------------------------------------------------------
# Git tools
# ---------------------------------------------------------------------------

@unittest.skipUnless(GIT_AVAILABLE, "git binary not installed")
class TestGitTools(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="git-test-")
        self.g = GitTools(timeout=30)
        r = self.g.init(self.repo, default_branch="main")
        self.assertTrue(r.ok, r.error)
        self.g.config_set("user.email", "test@example.com", path=self.repo)
        self.g.config_set("user.name", "Test User", path=self.repo)

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def _commit_file(self, name="a.txt", content="hello\n", msg="first"):
        p = os.path.join(self.repo, name)
        with open(p, "w") as f:
            f.write(content)
        self.assertTrue(self.g.add(name, path=self.repo).ok)
        r = self.g.commit(msg, path=self.repo)
        self.assertTrue(r.ok, r.error)
        return r

    def test_is_repo_and_not_a_repo(self):
        r = self.g.is_repo(self.repo)
        self.assertTrue(r.ok)
        self.assertTrue(r.parsed["is_repo"])
        other = tempfile.mkdtemp(prefix="not-repo-")
        try:
            r2 = self.g.is_repo(other)
            self.assertTrue(r2.ok)
            self.assertFalse(r2.parsed["is_repo"])
            r3 = self.g.status(other)
            self.assertFalse(r3.ok)
            self.assertIn("Not a git repository", r3.error)
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_status_clean_then_dirty(self):
        self._commit_file()
        st = self.g.status(self.repo)
        self.assertTrue(st.ok)
        self.assertEqual(st.parsed["branch"], "main")
        self.assertTrue(st.parsed["clean"])
        self.assertFalse(st.parsed["dirty"])
        # dirty the tree
        with open(os.path.join(self.repo, "a.txt"), "a") as f:
            f.write("more\n")
        st2 = self.g.status(self.repo)
        self.assertTrue(st2.parsed["dirty"])
        self.assertEqual(len(st2.parsed["unstaged"]), 1)
        # untracked file
        with open(os.path.join(self.repo, "new.txt"), "w") as f:
            f.write("x")
        st3 = self.g.status(self.repo)
        self.assertIn("new.txt", st3.parsed["untracked"])

    def test_log_parsing(self):
        self._commit_file(msg="first commit")
        self._commit_file(name="b.txt", content="b\n", msg="second commit")
        r = self.g.log(path=self.repo, max_count=5)
        self.assertTrue(r.ok)
        self.assertEqual(r.parsed["count"], 2)
        c0 = r.parsed["commits"][0]
        self.assertEqual(c0["subject"], "second commit")
        self.assertEqual(c0["author"], "Test User")
        self.assertEqual(len(c0["sha"]), 40)
        self.assertGreater(c0["timestamp"], 0)

    def test_diff_unstaged_and_staged(self):
        self._commit_file()
        with open(os.path.join(self.repo, "a.txt"), "a") as f:
            f.write("line2\n")
        d = self.g.diff(path=self.repo)
        self.assertTrue(d.ok)
        self.assertEqual(d.parsed["file_count"], 1)
        self.assertEqual(d.parsed["files"][0]["path"], "a.txt")
        self.assertEqual(d.parsed["total_added"], 1)
        ds = self.g.diff(path=self.repo, staged=True)
        self.assertEqual(ds.parsed["file_count"], 0)
        self.g.add("a.txt", path=self.repo)
        ds2 = self.g.diff(path=self.repo, staged=True)
        self.assertEqual(ds2.parsed["file_count"], 1)

    def test_branch_create_list_checkout(self):
        self._commit_file()
        r = self.g.branch_create("feature", path=self.repo)
        self.assertTrue(r.ok)
        self.assertEqual(r.parsed["name"], "feature")
        bl = self.g.branch_list(path=self.repo)
        names = [b["name"] for b in bl.parsed["branches"]]
        self.assertIn("main", names)
        self.assertIn("feature", names)
        self.assertEqual(bl.parsed["current"], "main")
        co = self.g.checkout("feature", path=self.repo)
        self.assertTrue(co.ok)
        bl2 = self.g.branch_list(path=self.repo)
        self.assertEqual(bl2.parsed["current"], "feature")

    def test_checkout_refuses_dirty_tree(self):
        self._commit_file()
        self.g.branch_create("feature", path=self.repo)
        with open(os.path.join(self.repo, "a.txt"), "a") as f:
            f.write("dirty\n")
        co = self.g.checkout("feature", path=self.repo)
        self.assertFalse(co.ok)
        self.assertIn("dirty", co.error.lower())

    def test_stash_and_pop(self):
        self._commit_file()
        with open(os.path.join(self.repo, "a.txt"), "a") as f:
            f.write("wip\n")
        s = self.g.stash("wip work", path=self.repo)
        self.assertTrue(s.ok)
        st = self.g.status(self.repo)
        self.assertTrue(st.parsed["clean"])
        sl = self.g.stash_list(path=self.repo)
        self.assertEqual(sl.parsed["count"], 1)
        p = self.g.stash_pop(path=self.repo)
        self.assertTrue(p.ok)
        sl2 = self.g.stash_list(path=self.repo)
        self.assertEqual(sl2.parsed["count"], 0)
        with open(os.path.join(self.repo, "a.txt")) as f:
            self.assertIn("wip", f.read())

    def test_show_file_at_commit_and_blame(self):
        self._commit_file(content="line1\nline2\n")
        r = self.g.show_file_at_commit("a.txt", "HEAD", path=self.repo)
        self.assertTrue(r.ok)
        self.assertIn("line1", r.output)
        b = self.g.blame_summary("a.txt", path=self.repo)
        self.assertTrue(b.ok)
        self.assertEqual(b.parsed["total_lines"], 2)
        self.assertEqual(b.parsed["authors"][0]["author"], "Test User")
        self.assertEqual(b.parsed["authors"][0]["lines"], 2)

    def test_reset_modes(self):
        self._commit_file()
        with open(os.path.join(self.repo, "a.txt"), "a") as f:
            f.write("v2\n")
        self.g.add("a.txt", path=self.repo)
        r = self.g.reset(mode="mixed", target="HEAD", path=self.repo)
        self.assertTrue(r.ok)
        st = self.g.status(self.repo)
        self.assertEqual(len(st.parsed["staged"]), 0)
        self.assertEqual(len(st.parsed["unstaged"]), 1)
        r2 = self.g.reset(mode="hard", target="HEAD", path=self.repo)
        self.assertTrue(r2.ok)
        st2 = self.g.status(self.repo)
        self.assertTrue(st2.parsed["clean"])

    def test_remote_list_empty_and_conflict_detection(self):
        self._commit_file()
        rl = self.g.remote_list(path=self.repo)
        self.assertTrue(rl.ok)
        self.assertEqual(rl.parsed["count"], 0)
        cf = self.g.conflict_files(path=self.repo)
        self.assertTrue(cf.ok)
        self.assertFalse(cf.parsed["in_conflict"])
        mip = self.g.merge_in_progress(path=self.repo)
        self.assertTrue(mip.ok)
        self.assertFalse(mip.parsed["in_progress"])

    def test_worktree_summary(self):
        self._commit_file()
        s = self.g.worktree_summary(path=self.repo)
        self.assertTrue(s.ok)
        self.assertEqual(s.parsed["status"]["branch"], "main")
        self.assertIn("last_commit", s.parsed)
        self.assertEqual(s.parsed["last_commit"]["subject"], "first")

    def test_tag_and_last_commit(self):
        self._commit_file()
        lc = self.g.last_commit(path=self.repo)
        self.assertTrue(lc.ok)
        self.assertEqual(lc.parsed["subject"], "first")
        code, _, _ = self.g._run(["tag", "v0.1"], path=self.repo)
        self.assertEqual(code, 0)
        tl = self.g.tag_list(path=self.repo)
        self.assertIn("v0.1", tl.parsed["tags"])


# ---------------------------------------------------------------------------
# File tools
# ---------------------------------------------------------------------------

class TestFileTools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="file-test-")
        self.f = FileTools()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _p(self, *parts):
        return os.path.join(self.tmp, *parts)

    def test_write_atomic_and_advanced_read(self):
        p = self._p("doc.txt")
        content = "line1\nline2\nline3\nline4\nline5\n"
        w = self.f.write_atomic(p, content)
        self.assertTrue(w.ok)
        self.assertTrue(w.parsed["atomic"])
        self.assertFalse(w.parsed["existed"])
        r = self.f.advanced_read(p, start_line=2, end_line=4)
        self.assertTrue(r.ok)
        self.assertEqual(r.parsed["total_lines"], 5)
        self.assertEqual(r.parsed["start_line"], 2)
        self.assertEqual(r.parsed["end_line"], 4)
        body = r.output.split("\n", 1)[1]
        self.assertEqual(body, "line2\nline3\nline4")
        # start beyond EOF
        r2 = self.f.advanced_read(p, start_line=99)
        self.assertFalse(r2.ok)

    def test_write_atomic_backup(self):
        p = self._p("b.txt")
        self.f.write_atomic(p, "v1")
        self.f.write_atomic(p, "v2", make_backup=True)
        self.assertTrue(os.path.exists(p + ".bak"))
        with open(p + ".bak") as fh:
            self.assertEqual(fh.read(), "v1")

    def test_append_and_prepend(self):
        p = self._p("c.txt")
        self.f.write_atomic(p, "middle")
        self.f.append(p, "end")
        self.f.prepend(p, "start\n")
        with open(p) as fh:
            text = fh.read()
        self.assertTrue(text.startswith("start\n"))
        self.assertIn("middle", text)
        self.assertTrue(text.rstrip().endswith("end"))

    def test_copy_move(self):
        src = self._p("src.txt")
        self.f.write_atomic(src, "data")
        dst = self._p("sub", "dst.txt")
        c = self.f.copy(src, dst)
        self.assertTrue(c.ok)
        self.assertTrue(os.path.exists(dst))
        # copy refuses overwrite by default
        c2 = self.f.copy(src, dst)
        self.assertFalse(c2.ok)
        moved = self._p("moved.txt")
        m = self.f.move(dst, moved)
        self.assertTrue(m.ok)
        self.assertFalse(os.path.exists(dst))
        self.assertTrue(os.path.exists(moved))

    def test_delete_safe_and_restore(self):
        p = self._p("doomed.txt")
        self.f.write_atomic(p, "bye")
        d = self.f.delete_safe(p)
        self.assertTrue(d.ok)
        self.assertFalse(os.path.exists(p))
        self.assertTrue(os.path.exists(d.parsed["trash_path"]))
        # restore
        trash_name = os.path.basename(d.parsed["trash_path"])
        back = self._p("restored.txt")
        rr = self.f.trash_restore(trash_name, back)
        self.assertTrue(rr.ok)
        with open(back) as fh:
            self.assertEqual(fh.read(), "bye")

    def test_trash_list(self):
        p = self._p("t.txt")
        self.f.write_atomic(p, "x")
        self.f.delete_safe(p)
        tl = self.f.trash_list()
        self.assertTrue(tl.ok)
        self.assertGreaterEqual(tl.parsed["count"], 1)

    def test_mkdir_p_and_touch_chmod(self):
        d = self._p("a", "b", "c")
        self.assertTrue(self.f.mkdir_p(d).ok)
        self.assertTrue(os.path.isdir(d))
        fp = os.path.join(d, "f.txt")
        self.assertTrue(self.f.touch(fp).ok)
        self.assertTrue(os.path.exists(fp))
        self.assertTrue(self.f.chmod(fp, "600").ok)
        self.assertEqual(oct(os.stat(fp).st_mode & 0o777), "0o600")

    def test_list_tree_filters(self):
        os.makedirs(self._p("sub"))
        self.f.write_atomic(self._p("a.py"), "x" * 100)
        self.f.write_atomic(self._p("b.md"), "y" * 10)
        self.f.write_atomic(self._p("sub", "c.py"), "z" * 5)
        self.f.write_atomic(self._p(".hidden"), "h")
        r = self.f.list_tree(self.tmp, extensions=[".py"])
        self.assertTrue(r.ok)
        self.assertEqual(r.parsed["file_count"], 2)
        r2 = self.f.list_tree(self.tmp, include_hidden=True)
        names = [e["name"] for e in r2.parsed["entries"]]
        self.assertIn(".hidden", names)
        r3 = self.f.list_tree(self.tmp, min_size=50)
        self.assertEqual(r3.parsed["file_count"], 1)

    def test_find_by_pattern_and_grep(self):
        self.f.write_atomic(self._p("one.py"), "def hello():\n    pass\n")
        self.f.write_atomic(self._p("two.txt"), "hello world\n")
        r = self.f.find_by_pattern(self.tmp, name_glob="*.py")
        self.assertTrue(r.ok)
        self.assertEqual(r.parsed["count"], 1)
        r2 = self.f.find_by_pattern(self.tmp, name_glob="*",
                                    content_regex=r"def \w+")
        self.assertEqual(r2.parsed["count"], 1)
        self.assertEqual(r2.parsed["matches"][0]["content_hits"], [1])
        g = self.f.grep(r"hello", self.tmp, extensions=[".py", ".txt"])
        self.assertTrue(g.ok)
        self.assertEqual(g.parsed["count"], 2)
        g2 = self.f.grep(r"([", self.tmp)
        self.assertFalse(g2.ok)  # invalid regex

    def test_file_info_and_checksum(self):
        p = self._p("info.txt")
        self.f.write_atomic(p, "hello checksum")
        fi = self.f.file_info(p)
        self.assertTrue(fi.ok)
        self.assertEqual(fi.parsed["kind"], "file")
        self.assertGreater(fi.parsed["size"], 0)
        self.assertIn("permissions", fi.parsed)
        c = self.f.checksum(p, "sha256")
        self.assertTrue(c.ok)
        self.assertEqual(len(c.parsed["digest"]), 64)
        v = self.f.verify_checksum(p, c.parsed["digest"])
        self.assertTrue(v.ok)
        self.assertTrue(v.parsed["match"])
        v2 = self.f.verify_checksum(p, "0" * 64)
        self.assertFalse(v2.ok)
        self.assertFalse(v2.parsed["match"])
        c_bad = self.f.checksum(p, "nope")
        self.assertFalse(c_bad.ok)

    def test_binary_refusal(self):
        p = self._p("bin.dat")
        with open(p, "wb") as fh:
            fh.write(b"\x00\x01\x02binary\xff")
        r = self.f.advanced_read(p)
        self.assertFalse(r.ok)
        self.assertIn("Binary", r.error)

    def test_watch_file_detects_change(self):
        p = self._p("watched.txt")
        self.f.write_atomic(p, "v1")

        def _modify():
            time.sleep(0.6)
            with open(p, "w") as fh:
                fh.write("v2 changed")

        t = threading.Thread(target=_modify)
        t.start()
        r = self.f.watch_file(p, timeout_sec=8, poll_interval=0.2)
        t.join()
        self.assertTrue(r.ok)
        self.assertTrue(r.parsed["changed"])
        self.assertEqual(r.parsed["event"], "modified")

    def test_watch_file_timeout(self):
        p = self._p("stable.txt")
        self.f.write_atomic(p, "stable")
        r = self.f.watch_file(p, timeout_sec=1, poll_interval=0.2)
        self.assertTrue(r.ok)
        self.assertFalse(r.parsed["changed"])

    def test_disk_usage_of_and_temp(self):
        self.f.write_atomic(self._p("x.txt"), "x" * 1000)
        du = self.f.disk_usage_of(self.tmp)
        self.assertTrue(du.ok)
        self.assertGreaterEqual(du.parsed["total_bytes"], 1000)
        tf = self.f.temp_file(suffix=".txt", content="tmp")
        self.assertTrue(tf.ok and os.path.exists(tf.parsed["path"]))
        os.unlink(tf.parsed["path"])
        td = self.f.temp_dir()
        self.assertTrue(td.ok and os.path.isdir(td.parsed["path"]))
        os.rmdir(td.parsed["path"])


# ---------------------------------------------------------------------------
# Network tools
# ---------------------------------------------------------------------------

class TestNetworkToolsOffline(unittest.TestCase):
    """Tests that need no connectivity."""

    def setUp(self):
        self.n = NetworkTools(timeout=10)

    def test_url_parse(self):
        r = self.n.url_parse("https://user:pw@example.com:8443/a/b?x=1&x=2#frag")
        self.assertTrue(r.ok)
        p = r.parsed
        self.assertEqual(p["scheme"], "https")
        self.assertEqual(p["hostname"], "example.com")
        self.assertEqual(p["port"], 8443)
        self.assertEqual(p["path_segments"], ["a", "b"])
        self.assertEqual(p["query_params"]["x"], ["1", "2"])
        self.assertEqual(p["fragment"], "frag")
        self.assertTrue(p["has_password"])
        self.assertTrue(p["is_https"])

    def test_url_parse_minimal(self):
        r = self.n.url_parse("http://example.com")
        self.assertTrue(r.ok)
        self.assertIsNone(r.parsed["port"])
        self.assertFalse(r.parsed["explicit_port"])

    def test_url_join(self):
        r = self.n.url_join("https://example.com/a/b/", "../c?x=1")
        self.assertTrue(r.ok)
        self.assertEqual(r.parsed["joined"], "https://example.com/a/c?x=1")

    def test_local_interfaces(self):
        r = self.n.local_interfaces()
        self.assertTrue(r.ok)
        self.assertIn("hostname", r.parsed)

    def test_dns_lookup_invalid_host(self):
        # Illegal hostname characters fail deterministically (no DNS needed).
        r = self.n.dns_lookup("bad..host!!name", timeout=5)
        self.assertFalse(r.ok)
        self.assertIn("hostname", r.parsed)

    def test_reverse_dns_localhost(self):
        r = self.n.reverse_dns("127.0.0.1", timeout=5)
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.parsed["ip"], "127.0.0.1")
        self.assertTrue(r.parsed["hostname"])  # typically 'localhost'


@unittest.skipUnless(NETWORK_AVAILABLE, "no outbound network connectivity")
class TestNetworkToolsOnline(unittest.TestCase):
    """Live tests against example.com — skipped when offline."""

    def setUp(self):
        self.n = NetworkTools(timeout=15)

    def test_dns_lookup(self):
        r = self.n.dns_lookup("example.com")
        self.assertTrue(r.ok, r.error)
        self.assertGreater(len(r.parsed["ipv4"]), 0)

    def test_tcp_connect_open(self):
        r = self.n.tcp_connect_test("example.com", 80, timeout=8)
        self.assertTrue(r.ok, r.error)
        self.assertTrue(r.parsed["open"])

    def test_http_get(self):
        r = self.n.http_get("https://example.com", timeout=15)
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.parsed["status"], 200)
        self.assertIn("Example Domain", r.parsed["body"])

    def test_http_get_404(self):
        r = self.n.http_get("https://example.com/does-not-exist-xyz",
                            timeout=15)
        self.assertFalse(r.ok)
        self.assertEqual(r.parsed["status"], 404)

    def test_http_head(self):
        r = self.n.http_head("https://example.com", timeout=15)
        self.assertTrue(r.ok)
        self.assertEqual(r.parsed["status"], 200)
        self.assertIn("text/html", r.parsed.get("content_type", ""))

    def test_headers_inspect(self):
        r = self.n.headers_inspect("https://example.com", timeout=15)
        self.assertTrue(r.ok)
        self.assertIn("security_headers", r.parsed)
        self.assertIn("missing_security_headers", r.parsed)

    def test_ping(self):
        r = self.n.ping_host("example.com", count=2, timeout_sec=4)
        # ping may be blocked in some sandboxes; only assert structure
        self.assertIn("reachable", r.parsed)

    def test_check_urls(self):
        r = self.n.check_urls(["https://example.com",
                               "https://example.com/nope-xyz"], timeout=15)
        self.assertTrue(r.ok)
        self.assertEqual(r.parsed["total"], 2)
        self.assertEqual(r.parsed["ok_count"], 1)

class TestNetworkToolsLocalhost(unittest.TestCase):
    """Deterministic network tests against local servers — always run,
    no external connectivity required."""

    def setUp(self):
        self.n = NetworkTools(timeout=10)

    @staticmethod
    def _closed_port() -> int:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def test_tcp_connect_closed_localhost(self):
        r = self.n.tcp_connect_test("127.0.0.1", self._closed_port(),
                                    timeout=3)
        self.assertFalse(r.ok)
        self.assertFalse(r.parsed["open"])
        self.assertTrue(r.parsed["error"])

    def test_tcp_connect_open_localhost(self):
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            r = self.n.tcp_connect_test("127.0.0.1", port, timeout=3)
        finally:
            listener.close()
        self.assertTrue(r.ok)
        self.assertTrue(r.parsed["open"])

    def test_port_check_localhost(self):
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        open_port = listener.getsockname()[1]
        closed_port = self._closed_port()
        try:
            r = self.n.port_check("127.0.0.1", [open_port, closed_port],
                                  timeout=3)
        finally:
            listener.close()
        self.assertTrue(r.ok)
        by_port = {p["port"]: p["open"] for p in r.parsed["ports"]}
        self.assertTrue(by_port[open_port])
        self.assertFalse(by_port[closed_port])
        self.assertEqual(r.parsed["open_ports"], [open_port])

    def test_resolve_and_check_local_http(self):
        import http.server
        srv = http.server.HTTPServer(("127.0.0.1", 0),
                                     http.server.SimpleHTTPRequestHandler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            r = self.n.resolve_and_check(f"http://127.0.0.1:{port}/",
                                         timeout=10)
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertTrue(r.ok, r.output)
        steps = r.parsed["steps"]
        self.assertTrue(steps["dns"]["ok"])
        self.assertTrue(steps["tcp"]["ok"])
        self.assertEqual(steps["http"]["status"], 200)

    def test_http_get_local(self):
        import http.server
        srv = http.server.HTTPServer(("127.0.0.1", 0),
                                     http.server.SimpleHTTPRequestHandler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            r = self.n.http_get(f"http://127.0.0.1:{port}/", timeout=10)
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.parsed["status"], 200)

    @unittest.skipUnless(shutil.which("openssl"), "openssl not installed")
    def test_ssl_info_self_signed(self):
        import ssl as _ssl
        import subprocess as _sp
        tmp = tempfile.mkdtemp(prefix="tls-test-")
        cert = os.path.join(tmp, "cert.pem")
        key = os.path.join(tmp, "key.pem")
        try:
            _sp.run(["openssl", "req", "-x509", "-newkey", "rsa:2048",
                     "-keyout", key, "-out", cert, "-days", "2",
                     "-nodes", "-subj", "/CN=localhost"],
                    capture_output=True, check=True, timeout=30)
            ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            raw = socket.socket()
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            raw.bind(("127.0.0.1", 0))
            raw.listen(1)
            port = raw.getsockname()[1]
            tls_srv = ctx.wrap_socket(raw, server_side=True)

            def _serve():
                try:
                    conn, _ = tls_srv.accept()
                    conn.do_handshake()
                    conn.close()
                except OSError:
                    pass

            t = threading.Thread(target=_serve, daemon=True)
            t.start()
            r = self.n.ssl_info("localhost", port=port, timeout=10,
                                verify=False)
            tls_srv.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertTrue(r.ok, r.error)
        self.assertFalse(r.parsed["expired"])
        self.assertIn("localhost", r.parsed["subject"])
        self.assertIsNotNone(r.parsed["days_until_expiry"])
        self.assertGreater(r.parsed["days_until_expiry"], 0)
        self.assertLess(r.parsed["days_until_expiry"], 3)

    def test_ssl_info_connection_refused(self):
        r = self.n.ssl_info("127.0.0.1", port=self._closed_port(),
                            timeout=3)
        self.assertFalse(r.ok)
        self.assertTrue(r.error)


# ---------------------------------------------------------------------------
# Data tools
# ---------------------------------------------------------------------------

class TestDataTools(unittest.TestCase):
    def setUp(self):
        self.d = DataTools()

    # -- JSON --
    def test_json_parse_validate_ok(self):
        r = self.d.json_parse_validate('{"a": 1, "b": "x"}')
        self.assertTrue(r.ok)
        self.assertEqual(r.parsed["value"]["a"], 1)

    def test_json_parse_invalid(self):
        r = self.d.json_parse_validate('{"a": }')
        self.assertFalse(r.ok)
        self.assertIn("Invalid JSON", r.output)

    def test_json_schema_valid(self):
        schema = {"type": "object", "required": ["name"],
                  "properties": {"name": {"type": "string", "minLength": 2},
                                 "age": {"type": "integer", "minimum": 0}}}
        r = self.d.json_parse_validate('{"name": "Ali", "age": 30}',
                                       schema=schema)
        self.assertTrue(r.ok)
        self.assertTrue(r.parsed["schema_valid"])

    def test_json_schema_violations(self):
        schema = {"type": "object", "required": ["name"],
                  "properties": {"age": {"type": "integer", "minimum": 0}}}
        r = self.d.json_parse_validate('{"age": -5}', schema=schema)
        self.assertFalse(r.ok)
        errs = r.parsed["schema_errors"]
        self.assertTrue(any("name" in e for e in errs))
        self.assertTrue(any("minimum" in e for e in errs))

    def test_json_query(self):
        data = {"store": {"book": [{"title": "A"}, {"title": "B"}]}}
        r = self.d.json_query(data, "$.store.book[0].title")
        self.assertTrue(r.ok)
        self.assertEqual(r.parsed["matches"], ["A"])
        r2 = self.d.json_query(data, "$.store.book[*].title")
        self.assertEqual(r2.parsed["count"], 2)
        r3 = self.d.json_query(data, "$.missing")
        self.assertEqual(r3.parsed["count"], 0)

    def test_json_pretty_minify(self):
        r = self.d.json_pretty({"b": 2, "a": 1})
        self.assertTrue(r.ok)
        self.assertLess(r.parsed["pretty"].index('"a"'),
                        r.parsed["pretty"].index('"b"'))
        m = self.d.json_minify('{ "a" : 1 }')
        self.assertEqual(m.parsed["minified"], '{"a":1}')

    # -- dicts --
    def test_merge_dicts_deep(self):
        base = {"a": 1, "n": {"x": 1, "y": 2}, "l": [1]}
        over = {"n": {"y": 99, "z": 3}, "l": [2], "new": True}
        r = self.d.merge_dicts(base, over)
        m = r.parsed["merged"]
        self.assertEqual(m["n"], {"x": 1, "y": 99, "z": 3})
        self.assertEqual(m["l"], [2])
        self.assertTrue(m["new"])
        # inputs not mutated
        self.assertEqual(base["n"]["y"], 2)

    def test_flatten_unflatten_roundtrip(self):
        nested = {"a": {"b": 1}, "l": [{"x": 5}]}
        f = self.d.flatten_dict(nested)
        self.assertIn("a.b", f.parsed["flat"])
        self.assertIn("l[0].x", f.parsed["flat"])
        u = self.d.unflatten_dict(f.parsed["flat"])
        self.assertEqual(u.parsed["nested"], nested)

    # -- CSV --
    CSV = "name,age,city\nAli,30,Karachi\nSara,25,Lahore\nBob,,Islamabad\n"

    def test_csv_read(self):
        r = self.d.csv_read(self.CSV)
        self.assertTrue(r.ok)
        self.assertEqual(r.parsed["row_count"], 3)
        self.assertEqual(r.parsed["columns"], ["name", "age", "city"])
        self.assertEqual(r.parsed["records"][0]["name"], "Ali")

    def test_csv_read_semicolon_sniff(self):
        r = self.d.csv_read("a;b\n1;2\n")
        self.assertTrue(r.ok)
        self.assertEqual(r.parsed["records"][0]["a"], "1")

    def test_csv_write_roundtrip(self):
        recs = [{"a": "1", "b": "x"}, {"a": "2", "b": "y"}]
        w = self.d.csv_write(recs)
        self.assertTrue(w.ok)
        r = self.d.csv_read(w.parsed["csv"])
        self.assertEqual(r.parsed["row_count"], 2)

    def test_csv_filter(self):
        recs = self.d.csv_read(self.CSV).parsed["records"]
        f = self.d.csv_filter(recs, "age", "gt", 26)
        self.assertEqual(f.parsed["count"], 1)
        self.assertEqual(f.parsed["records"][0]["name"], "Ali")
        f2 = self.d.csv_filter(recs, "city", "contains", "ore")
        self.assertEqual(f2.parsed["count"], 1)  # Lahore
        f3 = self.d.csv_filter(recs, "name", "regex", r"^S")
        self.assertEqual(f3.parsed["records"][0]["name"], "Sara")

    def test_csv_sort(self):
        recs = self.d.csv_read(self.CSV).parsed["records"]
        s = self.d.csv_sort(recs, "name")
        self.assertEqual([r["name"] for r in s.parsed["records"]],
                         ["Ali", "Bob", "Sara"])
        s2 = self.d.csv_sort(recs, "age", numeric=True, reverse=True)
        self.assertEqual(s2.parsed["records"][0]["name"], "Ali")

    def test_csv_stats(self):
        recs = self.d.csv_read(self.CSV).parsed["records"]
        r = self.d.csv_stats(recs)
        self.assertTrue(r.ok)
        age = r.parsed["stats"]["age"]
        self.assertEqual(age["type"], "numeric")
        self.assertEqual(age["min"], 25)
        self.assertEqual(age["max"], 30)
        self.assertAlmostEqual(age["mean"], 27.5)
        name = r.parsed["stats"]["name"]
        self.assertEqual(name["unique"], 3)

    # -- XML --
    XML = ('<root><item id="1">one</item><item id="2">two</item>'
           '<nested><leaf>x</leaf></nested></root>')

    def test_xml_parse(self):
        r = self.d.xml_parse(self.XML)
        self.assertTrue(r.ok)
        self.assertEqual(r.parsed["root_tag"], "root")
        items = r.parsed["data"]["root"]["item"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["@id"], "1")
        self.assertEqual(items[0]["#text"], "one")

    def test_xml_parse_invalid(self):
        r = self.d.xml_parse("<root><unclosed>")
        self.assertFalse(r.ok)

    def test_xml_find(self):
        r = self.d.xml_find(self.XML, "item")
        self.assertTrue(r.ok)
        self.assertEqual(r.parsed["count"], 2)

    # -- YAML --
    def test_yaml_parse(self):
        text = "name: Haley\nage: 7\ntags:\n  - a\n  - b\nnested:\n  x: true\n"
        r = self.d.yaml_parse(text)
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.parsed["data"]["name"], "Haley")
        self.assertEqual(r.parsed["data"]["nested"]["x"], True)

    # -- encoding / hashing --
    def test_base64_roundtrip(self):
        e = self.d.base64_encode("hello world")
        self.assertTrue(e.ok)
        d = self.d.base64_decode(e.parsed["encoded"])
        self.assertTrue(d.ok)
        self.assertEqual(d.parsed["decoded"], "hello world")
        bad = self.d.base64_decode("!!!not-base64!!!")
        self.assertFalse(bad.ok)

    def test_hash_string(self):
        r = self.d.hash_string("abc", "sha256")
        self.assertEqual(r.parsed["digest"],
                         "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        r2 = self.d.hash_string("abc", "md5")
        self.assertEqual(len(r2.parsed["digest"]), 32)
        r3 = self.d.hash_string("abc", "nope")
        self.assertFalse(r3.ok)

    # -- diff --
    def test_diff_text(self):
        r = self.d.diff_text("a\nb\nc\n", "a\nB\nc\nd\n")
        self.assertTrue(r.ok)
        self.assertFalse(r.parsed["identical"])
        self.assertGreater(r.parsed["added"], 0)
        r2 = self.d.diff_text("same", "same")
        self.assertTrue(r2.parsed["identical"])
        self.assertAlmostEqual(r2.parsed["similarity"], 1.0)

    def test_lines_process(self):
        r = self.d.lines_process("b\na\nb\n", "sort_unique")
        self.assertEqual(r.parsed["lines"], ["a", "b"])
        r2 = self.d.lines_process("x\ny", "numbered")
        self.assertTrue(r2.parsed["lines"][0].strip().startswith("1"))

    def test_word_count(self):
        r = self.d.word_count("hello world\nfoo")
        self.assertEqual(r.parsed["words"], 3)
        self.assertEqual(r.parsed["lines"], 2)


# ---------------------------------------------------------------------------
# System tools
# ---------------------------------------------------------------------------

class TestSystemTools(unittest.TestCase):
    def setUp(self):
        self.s = SystemTools()

    def test_sysinfo(self):
        r = self.s.sysinfo()
        self.assertTrue(r.ok)
        p = r.parsed
        self.assertTrue(p["python_version"].startswith("3."))
        self.assertGreater(p["cpu_count_logical"], 0)
        self.assertEqual(p["os"], "Linux")
        self.assertTrue(p["hostname"])

    def test_cpu_info(self):
        r = self.s.cpu_info()
        self.assertTrue(r.ok)
        self.assertGreater(r.parsed["logical"], 0)

    def test_memory_info(self):
        r = self.s.memory_info()
        self.assertTrue(r.ok)
        self.assertGreater(r.parsed["total"], 0)
        self.assertGreaterEqual(r.parsed["available"], 0)
        self.assertGreaterEqual(r.parsed["used_pct"], 0)

    def test_disk_usage(self):
        r = self.s.disk_usage("/tmp")
        self.assertTrue(r.ok)
        self.assertGreater(r.parsed["total"], 0)
        self.assertLessEqual(r.parsed["used_pct"], 100)

    def test_uptime(self):
        r = self.s.uptime()
        self.assertTrue(r.ok)
        self.assertGreater(r.parsed["uptime_seconds"], 0)
        self.assertIn("UTC", r.parsed["boot_time"])

    def test_load_avg(self):
        r = self.s.load_avg()
        self.assertTrue(r.ok)
        self.assertGreaterEqual(r.parsed["load_1"], 0)
        self.assertIn(r.parsed["verdict"], ("low", "moderate", "HIGH"))

    def test_process_list(self):
        r = self.s.process_list(limit=5)
        self.assertTrue(r.ok)
        self.assertGreater(r.parsed["total"], 0)
        me = [p for p in r.parsed["processes"]
              if p["pid"] == os.getpid()]
        # current process may fall outside top-5 by cpu; query directly
        r2 = self.s.process_list(limit=500)
        pids = [p["pid"] for p in r2.parsed["processes"]]
        self.assertIn(os.getpid(), pids)

    def test_process_list_name_filter(self):
        r = self.s.process_list(limit=50, name_filter="python")
        self.assertTrue(r.ok)

    def test_env_get_set_unset(self):
        key = "HALEY_TEST_VAR_XYZ"
        self.s.env_unset(key)
        g = self.s.env_get(key)
        self.assertFalse(g.ok)
        s = self.s.env_set(key, "123")
        self.assertTrue(s.ok)
        g2 = self.s.env_get(key)
        self.assertTrue(g2.ok)
        self.assertEqual(g2.parsed["value"], "123")
        self.s.env_unset(key)
        self.assertFalse(self.s.env_get(key).ok)
        # prefix listing works
        self.s.env_set("HALEY_TEST_PREFIX_A", "1")
        lp = self.s.env_get(prefix="HALEY_TEST_PREFIX_")
        self.assertGreaterEqual(lp.parsed["count"], 1)
        self.s.env_unset("HALEY_TEST_PREFIX_A")

    def test_env_set_bad_name(self):
        r = self.s.env_set("not a var!", "x")
        self.assertFalse(r.ok)

    def test_env_secrets_redacted(self):
        self.s.env_set("HALEY_TEST_API_KEY", "supersecret")
        try:
            r = self.s.env_get(prefix="HALEY_TEST_API_KEY")
            self.assertIn("<redacted>", r.output)
            self.assertNotIn("supersecret", r.output)
        finally:
            self.s.env_unset("HALEY_TEST_API_KEY")

    def test_which_command(self):
        r = self.s.which_command("python3")
        self.assertTrue(r.ok)
        self.assertTrue(r.parsed["primary"].endswith("python3"))
        r2 = self.s.which_command("definitely-not-a-real-cmd-xyz")
        self.assertFalse(r2.ok)

    def test_python_packages(self):
        r = self.s.python_packages(name_filter="pip", timeout=60)
        self.assertTrue(r.ok, r.error)
        names = [p["name"].lower() for p in r.parsed["packages"]]
        self.assertIn("pip", names)

    def test_pip_show(self):
        r = self.s.pip_show("pip", timeout=30)
        self.assertTrue(r.ok)
        self.assertTrue(r.parsed["installed"])
        self.assertIn("Version", r.parsed["info"])

    def test_temp_cleanup_dry_run(self):
        r = self.s.temp_files_cleanup(older_than_hours=24, dry_run=True)
        self.assertTrue(r.ok)
        self.assertTrue(r.parsed["dry_run"])
        self.assertIn("count", r.parsed)

    def test_disk_temp_summary(self):
        r = self.s.disk_temp_summary()
        self.assertTrue(r.ok)
        self.assertGreater(len(r.parsed["locations"]), 0)

    def test_open_ports_listening(self):
        r = self.s.open_ports_listening()
        self.assertTrue(r.ok)
        self.assertIn("count", r.parsed)

    def test_process_kill_safety(self):
        r = self.s.process_kill(1)
        self.assertFalse(r.ok)
        r2 = self.s.process_kill(os.getpid())
        self.assertFalse(r2.ok)
        r3 = self.s.process_kill(999999999)
        self.assertFalse(r3.ok)


# ---------------------------------------------------------------------------
# Dispatcher wiring
# ---------------------------------------------------------------------------

class TestDispatcherWiring(unittest.TestCase):
    def setUp(self):
        self.d = Dispatcher(default_registry(), workspace=tempfile.gettempdir())

    def test_new_tools_registered(self):
        names = {t.name for t in default_registry().list()}
        for expected in ["git_status", "git_log", "git_diff", "git_branch",
                         "git_commit", "file_tree", "file_find",
                         "file_checksum", "http_get", "http_post",
                         "dns_lookup", "port_check", "json_validate",
                         "csv_stats", "sysinfo", "disk_usage"]:
            self.assertIn(expected, names, f"missing: {expected}")

    def test_registry_schemas_present(self):
        reg = default_registry()
        for name in ["git_commit", "http_post", "port_check", "csv_stats"]:
            tool = reg.get(name)
            self.assertIsNotNone(tool)
            self.assertTrue(tool.input_schema, f"{name} has empty schema")

    @unittest.skipUnless(GIT_AVAILABLE, "git binary not installed")
    def test_dispatch_git_status(self):
        r = self.d.dispatch("git_status", {"path": "/tmp"})
        # /tmp is not a repo -> clean failure, not a crash
        self.assertFalse(r.ok)
        self.assertIn("Not a git repository", r.output)

    def test_dispatch_file_tree(self):
        r = self.d.dispatch("file_tree", {"path": tempfile.gettempdir(),
                                          "max_depth": 1})
        self.assertTrue(r.ok, r.output)
        self.assertIn("[parsed]", r.output)

    def test_dispatch_file_checksum(self):
        p = os.path.join(tempfile.gettempdir(), "dispatch-ck.txt")
        with open(p, "w") as fh:
            fh.write("dispatch")
        try:
            r = self.d.dispatch("file_checksum",
                                {"path": p, "algorithm": "sha256"})
            self.assertTrue(r.ok)
            self.assertIn("[parsed]", r.output)
        finally:
            os.unlink(p)

    def test_dispatch_json_validate(self):
        r = self.d.dispatch("json_validate",
                            {"text": '{"a": 1}',
                             "schema": {"type": "object"}})
        self.assertTrue(r.ok)
        r2 = self.d.dispatch("json_validate", {"text": "nope"})
        self.assertFalse(r2.ok)

    def test_dispatch_csv_stats(self):
        r = self.d.dispatch("csv_stats",
                            {"text": "a,b\n1,2\n3,4\n"})
        self.assertTrue(r.ok, r.output)
        self.assertIn("mean", r.output)

    def test_dispatch_sysinfo_disk(self):
        r = self.d.dispatch("sysinfo", {})
        self.assertTrue(r.ok)
        r2 = self.d.dispatch("disk_usage", {"path": "/tmp"})
        self.assertTrue(r2.ok)

    def test_dispatch_git_branch_unknown_action(self):
        r = self.d.dispatch("git_branch", {"action": "explode",
                                           "path": "/tmp"})
        self.assertFalse(r.ok)

    def test_dispatch_unknown_tool(self):
        r = self.d.dispatch("no_such_tool_xyz", {})
        self.assertFalse(r.ok)
        self.assertIn("Unknown or disabled", r.output)

    @unittest.skipUnless(NETWORK_AVAILABLE, "no outbound network connectivity")
    def test_dispatch_dns_lookup(self):
        r = self.d.dispatch("dns_lookup", {"hostname": "example.com"})
        self.assertTrue(r.ok, r.output)

    @unittest.skipUnless(NETWORK_AVAILABLE, "no outbound network connectivity")
    def test_dispatch_http_get(self):
        r = self.d.dispatch("http_get", {"url": "https://example.com",
                                         "timeout": 15})
        self.assertTrue(r.ok, r.output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
