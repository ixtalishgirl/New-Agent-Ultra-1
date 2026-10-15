"""Runtime browser self-install: detection + install-once logic.

Uses fake browser dirs and a mocked `playwright install` — never the network.
"""
import os
from pathlib import Path

import pytest

from agent.browser import ensure_browser as eb


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    d = tmp_path / "ms-playwright"
    d.mkdir()
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(d))
    eb._tried.clear()
    return d


def _make_headless_shell(base: Path):
    exe_dir = base / "chromium_headless_shell-1200" / "chrome-linux"
    exe_dir.mkdir(parents=True)
    (exe_dir / "headless_shell").write_text("fake")
    return exe_dir / "headless_shell"


def test_has_browser_false_on_empty(fake_home):
    assert eb.has_browser() is False


def test_has_browser_true_with_headless_shell(fake_home):
    _make_headless_shell(fake_home)
    assert eb.has_browser() is True


def test_has_browser_true_with_full_chromium(fake_home):
    exe_dir = fake_home / "chromium-1200" / "chrome-linux"
    exe_dir.mkdir(parents=True)
    (exe_dir / "chrome").write_text("fake")
    assert eb.has_browser() is True


def test_ensure_installs_when_missing(fake_home, monkeypatch):
    calls = []

    class _Proc:
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kw):
        calls.append(cmd)
        assert cmd[1:4] == ["-m", "playwright", "install"]
        _make_headless_shell(fake_home)  # simulate a successful download
        return _Proc()

    monkeypatch.setattr(eb.subprocess, "run", fake_run)
    ok, msg = eb.ensure_browser_installed()
    assert ok is True
    assert calls, "playwright install was never invoked"
    assert eb.has_browser() is True


def test_ensure_tries_only_once_per_process(fake_home, monkeypatch):
    calls = []

    class _Proc:
        stdout = ""
        stderr = "boom"

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _Proc()  # never creates the binary

    monkeypatch.setattr(eb.subprocess, "run", fake_run)
    ok1, _ = eb.ensure_browser_installed()
    ok2, msg2 = eb.ensure_browser_installed()
    assert ok1 is False
    assert ok2 is False
    assert "already attempted" in msg2
    assert len(calls) == 2, "expected headless-shell + full-chromium attempts, then stop"


def test_ensure_skips_when_already_present(fake_home, monkeypatch):
    _make_headless_shell(fake_home)

    def fake_run(cmd, **kw):  # pragma: no cover - must not be called
        raise AssertionError("install should not run when browser exists")

    monkeypatch.setattr(eb.subprocess, "run", fake_run)
    ok, _ = eb.ensure_browser_installed()
    assert ok is True
