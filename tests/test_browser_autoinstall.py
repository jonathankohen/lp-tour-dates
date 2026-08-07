"""Regression tests for the self-healing browser install (sources/browser).

Playwright pins each package version to a specific chromium BUILD, and the browsers live in
a shared cache OUTSIDE the venv. Upgrading the package (or building a fresh venv) therefore
leaves only the OLD build on disk and every launch fails with "Executable doesn't exist".
Because a failed scrape just returns 0 shows, that reads as silent data loss, not an error —
it took The Platters' tab from 20 shows to 1 on 2026-08-04.

The invariant: a missing browser installs itself and the launch retries, ONCE per process,
and only for that specific error.
"""
import sys
import types

import pytest

import sources.browser as browser_mod

MISSING_MSG = (
    "BrowserType.launch: Executable doesn't exist at "
    "/Users/x/Library/Caches/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell\n"
    "Please run the following command to download new browsers: playwright install"
)


class _FakeBrowser:
    def new_page(self):
        return _FakePage()

    def close(self):
        pass


class _FakePage:
    def set_default_timeout(self, ms):
        pass


class _FakeChromium:
    """Fails with `error` until `fail_times` launches have been attempted."""

    def __init__(self, log, error, fail_times):
        self._log, self._error, self._fail_times = log, error, fail_times
        self.launches = 0

    def launch(self, headless=True):
        self.launches += 1
        self._log.append("launch")
        if self.launches <= self._fail_times:
            raise RuntimeError(self._error)
        return _FakeBrowser()


class _FakeCtx:
    def __init__(self, log, error, fail_times):
        self.chromium = _FakeChromium(log, error, fail_times)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def harness(monkeypatch):
    """Patches playwright + the install subprocess; returns the shared call log."""
    log = []
    state = {"error": MISSING_MSG, "fail_times": 1, "install_ok": True}

    def _make_ctx():
        return _FakeCtx(log, state["error"], state["fail_times"])

    fake_api = types.ModuleType("playwright.sync_api")
    fake_api.sync_playwright = _make_ctx
    fake_pkg = types.ModuleType("playwright")
    fake_pkg.sync_api = fake_api
    monkeypatch.setitem(sys.modules, "playwright", fake_pkg)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_api)

    class _Proc:
        returncode = 0 if state["install_ok"] else 1
        stdout = stderr = ""

    def _fake_run(cmd, **kw):
        log.append("install:" + " ".join(cmd[-3:]))
        _Proc.returncode = 0 if state["install_ok"] else 1
        return _Proc

    monkeypatch.setattr(browser_mod.subprocess, "run", _fake_run)
    # Each test gets a fresh "have we tried installing yet" flag.
    monkeypatch.setattr(browser_mod, "_install_attempted", False)
    return log, state


def test_missing_browser_installs_and_retries(harness):
    """THE fix: the launch fails once, we install, and the retry yields a working page."""
    log, _ = harness
    with browser_mod.browser_page() as page:
        assert page is not None, "scrape must succeed after the auto-install"
    assert log == ["launch", "install:playwright install chromium", "launch"]


def test_install_runs_for_the_current_interpreter(harness):
    """Installing via `sys.executable -m playwright` is what makes the build match the
    package version — the exact thing that drifts."""
    log, _ = harness
    with browser_mod.browser_page():
        pass
    cmd_seen = [c for c in log if c.startswith("install:")]
    assert cmd_seen == ["install:playwright install chromium"]


def test_unrelated_launch_error_is_not_masked(harness):
    """A real launch failure must propagate, not trigger a pointless 100MB download."""
    log, state = harness
    state["error"] = "BrowserType.launch: Target page crashed"
    state["fail_times"] = 99
    with pytest.raises(RuntimeError, match="Target page crashed"):
        with browser_mod.browser_page():
            pass
    assert not [c for c in log if c.startswith("install:")]


def test_install_is_attempted_only_once_per_process(harness):
    """23 artists must not each trigger their own download when the install can't work."""
    log, state = harness
    state["fail_times"] = 99  # install "succeeds" but the browser still won't launch
    for _ in range(3):
        with pytest.raises(RuntimeError):
            with browser_mod.browser_page():
                pass
    assert len([c for c in log if c.startswith("install:")]) == 1


def test_failed_install_reraises_the_original_error(harness):
    """Offline / read-only cache: surface the real Playwright error, don't swallow it."""
    log, state = harness
    state["install_ok"] = False
    state["fail_times"] = 99
    with pytest.raises(RuntimeError, match="Executable doesn't exist"):
        with browser_mod.browser_page():
            pass


def test_error_matcher_recognises_the_real_message():
    assert browser_mod._is_missing_browser_error(RuntimeError(MISSING_MSG))
    assert not browser_mod._is_missing_browser_error(RuntimeError("Timeout 45000ms exceeded"))
