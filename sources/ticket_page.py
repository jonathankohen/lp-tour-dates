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

import requests

from config import _iso_time
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
