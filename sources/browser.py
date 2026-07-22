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
from contextlib import contextmanager

log = logging.getLogger(__name__)


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
