"""Recover a show's start time from the content of its ticket page.

Used as a last-resort fill when neither the source nor the ticket URL carried a
time. No Claude calls (per project rule: no per-show Claude calls) — extraction is
heuristic: schema.org Event `startDate` first (what most ticketing platforms emit),
then a clock time sitting next to a Show/Doors/Start label.
"""
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime as _dt

import requests

from config import _iso_time, _display_name
from models import Show

log = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

_JSONLD_RE = re.compile(
    r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.DOTALL | re.IGNORECASE
)
# "7:30 PM", "7:30pm", "8 p.m." — am/pm is required so bare numbers aren't misread.
_CLOCK_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?\b', re.IGNORECASE)
# Leading boundary only (no trailing) so "showtime", "doors", "starts", "begins" all match.
_TIME_LABEL_RE = re.compile(r'\b(?:show|start|curtain|performance|begin|door)', re.IGNORECASE)


def _to_24h(hour: int, minute: int, ampm: str) -> str:
    ampm = ampm.lower()
    if ampm == "p" and hour != 12:
        hour += 12
    elif ampm == "a" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return ""
    return f"{hour:02d}:{minute:02d}"


def _iter_jsonld_objects(html: str):
    """Yield every dict found in the page's JSON-LD blocks (flattening @graph/lists)."""
    for m in _JSONLD_RE.finditer(html):
        try:
            data = json.loads(m.group(1).strip())
        except (json.JSONDecodeError, ValueError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            obj = stack.pop()
            if isinstance(obj, list):
                stack.extend(obj)
            elif isinstance(obj, dict):
                graph = obj.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)
                yield obj


def _start_time_from_jsonld(html: str) -> str:
    """HH:MM from a schema.org Event `startDate`, if the page exposes one."""
    for obj in _iter_jsonld_objects(html):
        type_val = obj.get("@type", "")
        types = type_val if isinstance(type_val, list) else [type_val]
        if not any("Event" in str(t) for t in types):
            continue
        t = _iso_time(str(obj.get("startDate", "")))
        if t:
            return t
    return ""


def _start_time_from_text(html: str) -> str:
    """Conservative fallback: a clock time within ~40 chars after a Show/Doors label."""
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    for lm in _TIME_LABEL_RE.finditer(text):
        cm = _CLOCK_RE.search(text[lm.start(): lm.start() + 40])
        if cm:
            t = _to_24h(int(cm.group(1)), int(cm.group(2) or 0), cm.group(3))
            if t and t != "00:00":
                return t
    return ""


def start_time_from_html(html: str) -> str:
    """Best-effort show start time (HH:MM) from ticket-page HTML, or '' if none."""
    return _start_time_from_jsonld(html) or _start_time_from_text(html)


def _fetch_start_time(url: str) -> str:
    try:
        resp = requests.get(url, timeout=10, headers=_HEADERS)
        resp.raise_for_status()
    except Exception as exc:
        log.debug("Ticket-page fetch failed for %s: %s", url, exc)
        return ""
    return start_time_from_html(resp.text)


def fill_start_times_from_pages(shows: list[Show], max_workers: int = 8) -> None:
    """Fill missing start times by reading each show's ticket page.

    Only touches shows that have an http(s) ticket URL but no start_time — never
    overrides a time a source already supplied. One fetch per unique URL, run
    concurrently with a short timeout. Mutates `shows` in place.
    """
    targets = [s for s in shows if not s.start_time and s.ticket_url.startswith("http")]
    if not targets:
        return

    by_url: dict[str, list[Show]] = {}
    for s in targets:
        by_url.setdefault(s.ticket_url, []).append(s)

    log.info(
        "Ticket-page time lookup: %d unique pages for %d timeless shows",
        len(by_url), len(targets),
    )
    found = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_start_time, url): url for url in by_url}
        for fut in as_completed(futures):
            url = futures[fut]
            t = fut.result()
            if t:
                for s in by_url[url]:
                    s.start_time = t
                found += len(by_url[url])
    log.info("Ticket-page time lookup: filled %d shows from page content", found)


# --- Link verification (does a ticket page actually match this show?) -------------

# Words too generic to identify an act; dropped when deriving distinctive name tokens.
_ACT_STOPWORDS = {
    "the", "of", "a", "an", "and", "to", "in", "with", "for", "feat", "featuring",
    "presents", "tribute", "show", "shows", "concert", "experience", "original",
    "music", "band", "live", "evening", "ultimate", "salute", "celebration", "starring",
}


def _html_to_text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).lower()


def _render_page_html(url: str) -> str:
    """Render a JS page with Playwright and return its HTML, or '' if unavailable."""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        return ""
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(800)
            html = page.content()
            browser.close()
            return html
    except Exception as exc:
        log.debug("verify: render failed for %s: %s", url, exc)
        return ""


def fetch_page_text(url: str, render: bool = False) -> str:
    """Fetch a URL and return its visible text, lowercased. '' on any error.

    When `render` is set and the static fetch yields little text (a JS-rendered page),
    fall back to a headless-browser render. Rendering is sequential/blocking — only use
    it off the hot path (e.g. re-verifying a small set of AI-found links).
    """
    text = ""
    try:
        resp = requests.get(url, timeout=10, headers=_HEADERS)
        resp.raise_for_status()
        text = _html_to_text(resp.text)
    except Exception as exc:
        log.debug("verify: fetch failed for %s: %s", url, exc)
    if render and len(text) < 600:
        html = _render_page_html(url)
        if html:
            text = _html_to_text(html)
    return text


def _date_text_variants(iso: str) -> set[str]:
    """Lowercased textual renderings of an ISO date, to match however a page shows it."""
    try:
        d = _dt.strptime(iso, "%Y-%m-%d")
    except ValueError:
        return set()
    fmts = (
        "%B %-d", "%b %-d", "%-d %B", "%-d %b", "%B %-d, %Y", "%A, %B %-d",
        "%-m/%-d", "%m/%d", "%-m/%-d/%y", "%-m/%-d/%Y", "%Y-%m-%d",
    )
    return {d.strftime(f).lower() for f in fmts}


def _act_tokens(artist: str) -> set[str]:
    """Distinctive lowercased tokens identifying an act (e.g. 'a1a', 'buffett', 'fleetwood')."""
    names = f"{artist} {_display_name(artist)}".lower()
    return {t for t in re.split(r"[^a-z0-9]+", names) if len(t) >= 3 and t not in _ACT_STOPWORDS}


def _act_name_phrases(artist: str) -> set[str]:
    """Normalized (alnum-only) act name phrases used to confirm a page is really about
    this act — e.g. 'bohemianqueen', 'kissthesky', 'a1a'. Matching the whole name avoids
    false positives from a single common word (e.g. 'Queen' on a hotel room-rate page)."""
    core = re.split(r"[:\-(–—]", artist, 1)[0]  # drop subtitle after ':' / '-' / '('
    phrases = set()
    for n in (_display_name(artist), core, artist):
        norm = re.sub(r"[^a-z0-9]", "", n.lower())
        if len(norm) >= 3:
            phrases.add(norm)
    return phrases


def page_confirms_event(text: str, artist: str, date: str, start_time: str = "") -> bool:
    """True when the page mentions this act BY NAME and shows the date.

    The act check requires the full normalized act name (e.g. 'bohemianqueen'), not just
    any one of its words — so a hotel 'room-rate calendar' that merely contains 'Queen'
    and the date no longer counts as a Bohemian Queen ticket page. (start_time is accepted
    but only a soft signal — many correct pages don't render a matchable time.)
    """
    if not text:
        return False
    norm_text = re.sub(r"[^a-z0-9]", "", text)  # text is already lowercased
    has_act = any(p in norm_text for p in _act_name_phrases(artist))
    has_date = any(v in text for v in _date_text_variants(date))
    return has_act and has_date


# Path words that mark a listing/calendar page rather than one specific event.
_LISTING_WORDS = (
    "event", "events", "show", "shows", "upcoming", "calendar", "category", "tickets",
    "ticket", "season", "schedule", "detail", "default", "listing", "whatson",
    "performances", "tour", "dates", "music", "rock",
)


def url_event_slug_ok(url: str, artist: str, date: str) -> bool:
    """Guard against 'right venue, wrong show' pages. If a URL points at ONE specific event
    (a multi-word slug after /event//show//e/ etc.), the slug must reference this act or
    date — otherwise it's a different show's page that merely lists ours in a sidebar.
    Listing/calendar/short pages pass (they legitimately don't name a single act)."""
    from urllib.parse import urlparse
    path = urlparse(url).path.lower()
    if "category" in path:
        return True
    segs = [s for s in path.split("/") if s]
    if not segs:
        return True
    last = segs[-1].rsplit(".", 1)[0]  # drop any file extension
    if "-" not in last or len(last) < 9:
        return True  # short / single-word slug: act page, listing, or generic ticketer path
    norm = re.sub(r"[^a-z0-9]", "", last)
    if any(t in norm for t in _act_tokens(artist)):
        return True
    if any(re.sub(r"[^a-z0-9]", "", v) in norm for v in _date_text_variants(date)):
        return True
    return any(w in last for w in _LISTING_WORDS)


def verify_ticket_links(shows: list[Show], max_workers: int = 8) -> tuple[list[Show], list[Show]]:
    """Split shows into (verified, failed) by checking each ticket page for act + date.

    One fetch per unique http(s) URL (threaded). A show with no http URL, an unreachable
    page, or a page that doesn't confirm the act+date goes to `failed`.
    """
    by_url: dict[str, list[Show]] = {}
    failed: list[Show] = []
    for s in shows:
        if s.ticket_url.startswith("http"):
            by_url.setdefault(s.ticket_url, []).append(s)
        else:
            failed.append(s)

    texts: dict[str, str] = {}
    if by_url:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(fetch_page_text, u): u for u in by_url}
            for fut in as_completed(futures):
                texts[futures[fut]] = fut.result()

    verified: list[Show] = []
    for url, group in by_url.items():
        text = texts.get(url, "")
        for s in group:
            (verified if page_confirms_event(text, s.artist, s.date, s.start_time) else failed).append(s)

    log.info("Link verify: %d verified, %d need fixing (of %d).", len(verified), len(failed), len(shows))
    return verified, failed
