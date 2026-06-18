"""Dig into a venue link to find the specific event/date page — no AI.

When a stored ticket link is a valid venue page that doesn't itself show the show's
date (e.g. a homepage that links off to an Events/Calendar page, or a JS calendar you
have to page forward through), this follows on-site links and, as a last resort, drives
a headless browser through the calendar widget to surface the date. Everything here is
plain HTTP + Playwright browser automation — no API calls, no AI cost. Returns a
confirmed deep URL or "".

Used by enrichment.verify_and_fix_ticket_links as the first repair step, before any web
search: the existing venue link is the best lead, so exhaust it before searching.
"""
import logging
import re
from urllib.parse import urljoin, urlparse

import requests

from config import _is_platform_url, _is_non_ticket_url
from models import Show
from sources.ticket_page import (
    _HEADERS,
    _html_to_text,
    _render_page_html,
    _act_tokens,
    _date_text_variants,
    page_confirms_event,
    url_event_slug_ok,
)

log = logging.getLogger(__name__)

# Link text / href fragments that suggest a path toward event listings.
_NAV_KEYWORDS = (
    "event", "events", "calendar", "ticket", "tickets", "schedule", "show", "shows",
    "performance", "performances", "upcoming", "whats-on", "what-s-on", "buy", "tour",
    "dates", "concerts",
)

_MAX_DEPTH = 2     # start_url (0) -> events/calendar (1) -> specific event page (2)
_PER_LEVEL = 8     # most candidate links to follow from a single page
_MAX_PAGES = 25    # overall fetch budget per dig, to bound a wandering crawl
_MAX_CLICKS = 14   # calendar "next" clicks before giving up (covers ~a year ahead)

# "Next month / next" controls on common calendar widgets.
_NEXT_SELECTORS = (
    "[aria-label*='next' i]",
    "button[title*='next' i]",
    "a[title*='next' i]",
    ".fc-next-button",            # FullCalendar
    "[class*='next-month']",
    "[class*='calendar-next']",
)
_NEXT_TEXTS = ("Next", "Next month", "›", "→", "»", ">")


def _norm(s: str) -> str:
    """Collapse to alphanumeric tokens so date forms match across spacing/punctuation
    (e.g. '2026-08-15' in a URL and 'August 15' in text both normalize cleanly)."""
    return re.sub(r"[^a-z0-9]+", " ", s.lower())


def _registrable(netloc: str) -> str:
    netloc = netloc.lower().split(":")[0]
    parts = netloc.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else netloc


def _same_site(url: str, base: str) -> bool:
    return _registrable(urlparse(url).netloc) == _registrable(urlparse(base).netloc)


def _fetch_html(url: str) -> str:
    """Static fetch, falling back to a headless render when the static text is thin."""
    html = ""
    try:
        resp = requests.get(url, timeout=10, headers=_HEADERS)
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        log.debug("dig: fetch failed for %s: %s", url, exc)
    if len(_html_to_text(html)) < 600:
        rendered = _render_page_html(url)
        if rendered:
            html = rendered
    return html


def _links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Return [(absolute_url, link_text_lowercased)] for usable anchors on the page."""
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except ImportError:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(base_url, href)
        if url.startswith("http"):
            out.append((url, a.get_text(" ", strip=True).lower()))
    return out


def _score(url: str, text: str, date_variants: set[str]) -> int:
    """3 = link references the show's date, 1 = looks like an events/calendar/ticket link."""
    hay = _norm(url + " " + text)
    if any(_norm(v) in hay for v in date_variants):
        return 3
    if any(kw in hay for kw in _NAV_KEYWORDS):
        return 1
    return 0


def _drill_to_specific(html: str, page_url: str, show: Show) -> str:
    """A confirming page may be a listing (e.g. '/events') that links to the individual
    show's ticket page. Follow the most show-specific same-site link that itself confirms
    the act + date, so we land on that show's page rather than the whole listing. '' if
    no more-specific page is found."""
    date_variants = _date_text_variants(show.date)
    act_toks = _act_tokens(show.artist)
    candidates: list[tuple[int, int, str]] = []
    for lu, lt in _links(html, page_url):
        if lu == page_url or not _same_site(lu, page_url) or _is_platform_url(lu) or _is_non_ticket_url(lu):
            continue
        hay = _norm(lu + " " + lt)
        score = 0
        if any(_norm(v) in hay for v in date_variants):
            score += 3
        if any(t in hay for t in act_toks):
            score += 2
        if any(k in hay for k in ("event", "ticket", "tickets", "show", "performance")):
            score += 1
        if score:
            # Prefer deeper paths — a specific event page sits below '/events'.
            depth_bonus = urlparse(lu).path.rstrip("/").count("/")
            candidates.append((score, depth_bonus, lu))
    candidates.sort(key=lambda x: (-x[0], -x[1]))
    for _, _, lu in candidates[:_PER_LEVEL]:
        if not url_event_slug_ok(lu, show.artist, show.date):
            continue
        if page_confirms_event(_html_to_text(_fetch_html(lu)), show.artist, show.date, show.start_time):
            return lu
    return ""


def crawl_for_event(start_url: str, show: Show) -> str:
    """BFS the venue's own site from start_url, following events/calendar/date links, and
    return the page that confirms the act + date — drilling from a listing into the show's
    own ticket page where possible. '' if none found."""
    date_variants = _date_text_variants(show.date)
    visited: set[str] = set()
    frontier: list[tuple[str, int]] = [(start_url, 0)]
    fetched = 0

    while frontier and fetched < _MAX_PAGES:
        url, depth = frontier.pop(0)
        if url in visited:
            continue
        visited.add(url)

        html = _fetch_html(url)
        fetched += 1
        if not html:
            continue

        if (url != start_url and not _is_non_ticket_url(url)
                and url_event_slug_ok(url, show.artist, show.date)
                and page_confirms_event(_html_to_text(html), show.artist, show.date, show.start_time)):
            # Land on the show's own page, not the listing it was found on, when possible.
            return _drill_to_specific(html, url, show) or url

        if depth >= _MAX_DEPTH:
            continue
        scored = []
        for lu, lt in _links(html, url):
            if lu in visited or not _same_site(lu, start_url) or _is_platform_url(lu) or _is_non_ticket_url(lu):
                continue
            s = _score(lu, lt, date_variants)
            if s > 0:
                scored.append((s, lu))
        scored.sort(key=lambda x: -x[0])
        for _, lu in scored[:_PER_LEVEL]:
            frontier.append((lu, depth + 1))
    return ""


def _click_next(page) -> bool:
    """Click a calendar 'next' control if one is present and visible."""
    for sel in _NEXT_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click(timeout=2000)
                return True
        except Exception:
            continue
    for label in _NEXT_TEXTS:
        try:
            el = page.get_by_text(label, exact=True).first
            if el.is_visible():
                el.click(timeout=2000)
                return True
        except Exception:
            continue
    return False


def _link_for_date(page, date_variants: set[str]) -> str:
    """Find an on-page anchor whose text/href references the date; return its URL or ''."""
    try:
        anchors = page.query_selector_all("a[href]")
    except Exception:
        return ""
    for a in anchors:
        try:
            hay = _norm((a.inner_text() or "") + " " + (a.get_attribute("href") or ""))
            if any(_norm(v) in hay for v in date_variants):
                full = urljoin(page.url, a.get_attribute("href") or "")
                if full.startswith("http") and not _is_platform_url(full):
                    return full
        except Exception:
            continue
    return ""


def interactive_calendar_search(start_url: str, show: Show) -> str:
    """Drive a headless browser through a JS calendar: page forward until the show's date
    appears, then return the event link for that date (or the current URL). '' on failure."""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        return ""
    date_variants = _date_text_variants(show.date)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(start_url, wait_until="domcontentloaded", timeout=20000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(800)

            for _ in range(_MAX_CLICKS):
                if page_confirms_event(
                    _html_to_text(page.content()), show.artist, show.date, show.start_time
                ):
                    found = _link_for_date(page, date_variants) or page.url
                    browser.close()
                    return found
                if not _click_next(page):
                    break
                page.wait_for_timeout(600)
            browser.close()
    except Exception as exc:
        log.debug("dig: interactive calendar failed for %s: %s", start_url, exc)
    return ""


def deepen_to_specific(url: str, show: Show) -> str:
    """Given a confirming venue URL, return a more show-specific page on the same site if
    one exists, else the url unchanged. Turns a listing/series page that a search returned
    into the individual show's ticket page. Skips platform/empty URLs."""
    if not url.startswith("http") or _is_platform_url(url):
        return url
    html = _fetch_html(url)
    if not html:
        return url
    return _drill_to_specific(html, url, show) or url


def dig_for_event(start_url: str, show: Show) -> str:
    """Find the deep event/date page reachable from a venue link. '' if none.

    Skips platform/empty links (digging a venue's own site is the point). Tries a static
    same-site crawl first, then interactive calendar navigation. No AI.
    """
    if not start_url.startswith("http") or _is_platform_url(start_url):
        return ""
    found = crawl_for_event(start_url, show)
    if found:
        log.info("dig: crawl resolved %s %s -> %s", show.artist, show.date, found)
        return found
    found = interactive_calendar_search(start_url, show)
    if found:
        log.info("dig: calendar resolved %s %s -> %s", show.artist, show.date, found)
    return found
