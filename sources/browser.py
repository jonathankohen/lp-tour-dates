"""Shared headless-browser helper with guaranteed teardown.

Playwright's *sync* API drives its own event loop behind a greenlet. If an exception
escapes a `with sync_playwright()` block while a browser is still open — a `goto`
timeout, a failed `expect_response` — the browser is never closed and the driver's loop
is left running. Every later sync_playwright() call in that PROCESS then dies instantly
with "This event loop is already running", and after that "It looks like you are using
Playwright Sync API inside the asyncio loop".

That turned one artist's page timeout into a silent, run-wide outage on 2026-07-22:
`Michael Griffin Escapes` timed out on goto, and from that point every Playwright-backed
source failed instantly — the Bandsintown widget scrapes (A1A, Bohemian Queen, Free
Fallin, Back 2 Mac), the Elfsight calendar (Monkee Men), and every PLAYWRIGHT_RENDER_PAGES
scrape. Six artists collapsed to near-zero and only the regression guard kept their real
data from being published away.

Every Playwright user in this package must go through `browser_page()` so the browser is
closed in a `finally` before the playwright context exits, keeping the loop clean for the
next caller.
"""
import logging
import subprocess
import sys
from contextlib import contextmanager

log = logging.getLogger(__name__)

# Playwright pins each package version to a specific browser BUILD (1.60.0 wants
# chromium-1223). The browsers live outside the venv in a shared user cache
# (~/Library/Caches/ms-playwright on macOS), so upgrading the `playwright` package — or
# building a fresh venv — leaves the cache holding only the OLD build and every launch
# fails with "Executable doesn't exist ... run `playwright install`". Because a failed
# scrape just returns 0 shows, that surfaces as silent data loss rather than an error:
# it took The Platters' tab from 20 shows to 1 on 2026-08-04.
#
# So the missing binary self-heals here instead of being a recurring manual chore. We
# install with `sys.executable -m playwright`, which guarantees the build matches the
# interpreter's OWN package version — the exact thing that drifts.
_MISSING_BROWSER_MARKERS = ("executable doesn't exist", "playwright install")
_install_attempted = False


def _is_missing_browser_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _MISSING_BROWSER_MARKERS)


def _ensure_browser_installed() -> bool:
    """Download the chromium build this playwright version expects. Once per process.

    Returns True only if the install command succeeded, so the caller knows whether a
    retry is worth attempting. Never raises — if we can't install (offline, sandboxed,
    read-only cache), the caller re-raises the original launch error.
    """
    global _install_attempted
    if _install_attempted:
        return False  # already tried this process; don't re-download on every artist
    _install_attempted = True

    log.warning("Playwright chromium is missing or stale for playwright's current version — "
                "installing it now (one-time, ~100MB). This is expected after a playwright "
                "upgrade or a fresh venv.")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True, text=True, timeout=900,
        )
    except Exception as exc:
        log.error("Could not run `playwright install chromium`: %s", exc)
        return False
    if proc.returncode != 0:
        log.error("`playwright install chromium` failed (exit %d): %s",
                  proc.returncode, (proc.stderr or proc.stdout or "").strip()[:500])
        return False
    log.info("Playwright chromium installed.")
    return True


@contextmanager
def browser_page(*, timeout_ms: int = 45000):
    """Yield a fresh headless Chromium `page`, tearing the browser down no matter what.

    Yields None if Playwright isn't installed, so callers can degrade gracefully:

        with browser_page() as page:
            if page is None:
                return []
            page.goto(...)

    Exceptions from the body propagate to the caller (who logs/retries) — but only AFTER
    the browser is closed and the playwright context exited cleanly.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        log.warning("playwright not installed — skipping headless-browser scrape")
        yield None
        return

    with sync_playwright() as pw:
        browser = None
        try:
            try:
                browser = pw.chromium.launch(headless=True)
            except Exception as exc:
                # A missing/stale browser build is recoverable: install it and retry once.
                # Anything else (a real launch failure) propagates untouched.
                if not _is_missing_browser_error(exc) or not _ensure_browser_installed():
                    raise
                browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_default_timeout(timeout_ms)
            yield page
        finally:
            # The whole point: close before sync_playwright.__exit__ runs, on EVERY path.
            if browser is not None:
                try:
                    browser.close()
                except Exception as exc:  # already failing; don't mask the original error
                    log.debug("Ignoring browser.close() error during teardown: %s", exc)
