"""Regression tests for Playwright teardown (sources/browser.browser_page).

Reproduces the 2026-07-22 run-wide outage: `Michael Griffin Escapes` timed out on
`page.goto`, the browser was never closed (close() sat on the success path only), and
Playwright's sync driver was left with a running event loop. Every later Playwright call
in the process then failed instantly — "This event loop is already running", then "It
looks like you are using Playwright Sync API inside the asyncio loop" — silently killing
the Bandsintown widget scrapes, the Elfsight calendar, and every rendered page after it.
Six artists collapsed and only the regression guard kept their real data from publishing.

The invariant: a failure inside the block must still close the browser, so the NEXT
caller gets a clean context.
"""
import sys
import types

import pytest

import sources.browser as browser_mod


class _FakePage:
    def __init__(self):
        self.default_timeout = None

    def set_default_timeout(self, ms):
        self.default_timeout = ms


class _FakeBrowser:
    def __init__(self, log):
        self._log = log
        self.closed = False

    def new_page(self):
        return _FakePage()

    def close(self):
        self.closed = True
        self._log.append("browser.close")


class _FakeChromium:
    def __init__(self, log):
        self._log = log
        self.last_browser = None

    def launch(self, headless=True):
        self._log.append("launch")
        self.last_browser = _FakeBrowser(self._log)
        return self.last_browser


class _FakePlaywright:
    def __init__(self, log):
        self.chromium = _FakeChromium(log)


class _FakeSyncPlaywrightCtx:
    """Stands in for sync_playwright(); records enter/exit so we can assert ordering."""

    def __init__(self, log):
        self._log = log
        self._pw = _FakePlaywright(log)

    def __enter__(self):
        self._log.append("pw.enter")
        return self._pw

    def __exit__(self, *exc):
        self._log.append("pw.exit")
        return False


@pytest.fixture
def call_log(monkeypatch):
    log = []
    fake_api = types.ModuleType("playwright.sync_api")
    fake_api.sync_playwright = lambda: _FakeSyncPlaywrightCtx(log)
    fake_pkg = types.ModuleType("playwright")
    fake_pkg.sync_api = fake_api
    monkeypatch.setitem(sys.modules, "playwright", fake_pkg)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_api)
    return log


def test_browser_closed_on_success(call_log):
    with browser_mod.browser_page() as page:
        assert page is not None
    assert call_log == ["pw.enter", "launch", "browser.close", "pw.exit"]


def test_browser_closed_when_the_body_raises(call_log):
    """THE regression: a goto timeout must not leave the browser open."""
    with pytest.raises(RuntimeError, match="Timeout 45000ms exceeded"):
        with browser_mod.browser_page():
            raise RuntimeError("Page.goto: Timeout 45000ms exceeded")

    assert "browser.close" in call_log, "browser must be closed even when the body raises"
    # And closed BEFORE the playwright context exits — that ordering is what keeps the
    # driver's event loop from being left running for the next caller.
    assert call_log.index("browser.close") < call_log.index("pw.exit")


def test_a_failure_does_not_poison_the_next_call(call_log):
    """The cascade itself: one failed scrape, then a clean one."""
    with pytest.raises(RuntimeError):
        with browser_mod.browser_page():
            raise RuntimeError("Page.goto: Timeout 45000ms exceeded")

    with browser_mod.browser_page() as page:
        assert page is not None, "a later scrape must still get a working page"

    assert call_log.count("browser.close") == 2
    assert call_log.count("pw.exit") == 2


def test_timeout_is_applied_to_the_page(call_log):
    with browser_mod.browser_page(timeout_ms=1234) as page:
        assert page.default_timeout == 1234


def test_yields_none_when_playwright_missing(monkeypatch):
    """Callers degrade gracefully rather than raising ImportError."""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "playwright.sync_api":
            raise ImportError("No module named 'playwright'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with browser_mod.browser_page() as page:
        assert page is None
