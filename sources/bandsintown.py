import json
import logging
import re

import requests

from config import (
    BANDSINTOWN_APP_IDS,
    BANDSINTOWN_ARTIST_NAMES,
    BANDSINTOWN_WIDGET_PAGES,
    _iso_time,
    act_name_matches,
)
from models import Show
from sources.browser import browser_page

log = logging.getLogger(__name__)


# Some DIY acts self-list on Bandsintown, registering the "venue" as the act itself: the
# feed carries the real address but names the venue after the band (e.g. Monkee Men's Oct
# 2026 Delray Beach run — address 950 NW 9th St = Delray Beach Playhouse, but venue.name is
# "The Monkee Men - Greatest Monkees Tribute"). We can't blank it — the Sheet→front-end
# read-back drops venueless rows — so map the act's self-listed venue to its real name here.
# Keyed by artist; only applied when venue.name is actually the act's own name.
_SELF_LISTED_VENUE_FIX: dict[str, str] = {
    "Monkee Men": "Delray Beach Playhouse",
}


def _clean_venue_name(raw: str, artist: str) -> str:
    """Correct a Bandsintown `venue.name` that is really the act's own name.

    Returns the mapped real venue for a known self-listing, else the raw name unchanged
    (never blank — a venueless row is dropped by the Sheet read-back).
    """
    if raw and act_name_matches(raw, artist):
        return _SELF_LISTED_VENUE_FIX.get(artist, raw)
    return raw


def _bandsintown_event_url(ev: dict) -> str:
    """Return the link to store for a Bandsintown event.

    Prefer the event's own Bandsintown page (`url`, a `.../e/<id>` link) over the
    `offers[].url` ticket deep-link (`.../t/<id>`). The ticket deep-links are
    fickle — they frequently break/redirect — whereas the event page is stable and
    always resolves to the same event (with its own ticket button). Both are
    bandsintown.com platform URLs, so enrichment still prefers a venue-direct link
    when it can confirm one; this only changes the fallback we keep.
    """
    event_url = (ev.get("url") or "").strip()
    if event_url:
        return event_url
    offers = ev.get("offers", [])
    return offers[0].get("url", "") if offers else ""


def _fetch_bandsintown_via_widget(artist: str, page_url: str) -> list[Show]:
    """
    Load an artist's tour page in a headless browser and intercept the Bandsintown
    API response the widget makes internally. Used when the REST API returns 0.
    Requires: playwright install chromium
    """
    captured: list[dict] = []

    # The widget's internal /events call is flaky to intercept: when the page's JS
    # initializes promptly it fires in a few seconds, but on a cold load it sometimes
    # never fires within the window. Retry with a fresh browser before giving up.
    _WIDGET_ATTEMPTS = 3
    last_exc: Exception | None = None
    for attempt in range(1, _WIDGET_ATTEMPTS + 1):
        try:
            with browser_page() as page:
                if page is None:
                    return []

                # Wait specifically for the Bandsintown events API call the widget makes,
                # rather than waiting for all network activity to stop (which never happens).
                # These widgets lazy-load below the fold: they only initialize (and fire
                # their internal /events call) once the page has fully loaded AND the widget
                # is scrolled into view. domcontentloaded / no-scroll leaves them dormant and
                # the interception times out. Wait for full load, then scroll.
                with page.expect_response(
                    lambda r: "rest.bandsintown.com" in r.url and "/events" in r.url,
                    timeout=45000,
                ) as response_info:
                    page.goto(page_url, wait_until="load", timeout=45000)
                    for _ in range(8):
                        page.mouse.wheel(0, 3000)
                        page.wait_for_timeout(1200)

                resp = response_info.value
                log.info("Bandsintown widget intercepted: %s", resp.url)
                body = resp.text().strip()
                # Response may be JSONP: callbackName([...]) — strip the wrapper
                jsonp_match = re.match(r"^\w+\((.+)\)\s*$", body, re.DOTALL)
                if jsonp_match:
                    body = jsonp_match.group(1)
                data = json.loads(body)
                if isinstance(data, list):
                    captured.extend(data)
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            log.warning(
                "Playwright widget scrape attempt %d/%d failed for %s: %s",
                attempt, _WIDGET_ATTEMPTS, artist, exc,
            )
    if last_exc is not None:
        log.error("Playwright widget scrape error for %s: %s", artist, last_exc)
        return []

    from datetime import date as _date
    today = _date.today().isoformat()
    shows = []
    for ev in captured:
        dt_raw = ev.get("datetime", "")
        dt = dt_raw[:10]
        if dt < today:
            continue
        venue = ev.get("venue", {})
        ticket_url = _bandsintown_event_url(ev)
        # Multi-act agency pages (e.g. Zenn Entertainment) embed ONE Bandsintown widget that
        # lists every act they book, so this intercepted feed mixes acts (Bohemian Queen, The
        # Z Street Band, Separate Journeys, …). Each event names its own act in `title`
        # ("BOHEMIAN QUEEN @ Venue") and `lineup`, so carry that onto Show.performer and let the
        # act-name guard drop the other acts. (The REST path is keyed by a single artist's
        # app_id, so it doesn't need this.)
        lineup = ev.get("lineup") or []
        performer = " ".join([ev.get("title", "")] + [str(a) for a in lineup if a]).strip()
        shows.append(Show(
            artist=artist,
            date=dt,
            venue=_clean_venue_name(venue.get("name", ""), artist),
            city=venue.get("city", ""),
            region=venue.get("region", ""),
            country=venue.get("country", ""),
            ticket_url=ticket_url,
            source="bandsintown",
            start_time=_iso_time(dt_raw),
            performer=performer,
        ))
    log.info("Bandsintown widget: %d shows for %s", len(shows), artist)
    return shows


def fetch_bandsintown(artist: str) -> list[Show]:
    """Fetch upcoming events from Bandsintown REST API or JS widget interception.

    Only runs for artists we have a confirmed working method for:
    - BANDSINTOWN_APP_IDS: artists with their own app_id (Kiss the Sky)
    - BANDSINTOWN_WIDGET_PAGES: artists whose site embeds a Bandsintown JS widget
    All other artists are skipped to avoid noisy 404s / useless zero-result calls.
    """
    has_app_id = artist in BANDSINTOWN_APP_IDS
    has_widget = artist in BANDSINTOWN_WIDGET_PAGES
    if not has_app_id and not has_widget:
        return []

    shows: list[Show] = []

    if has_app_id:
        bandsintown_name = BANDSINTOWN_ARTIST_NAMES.get(artist, artist)
        log.info("Bandsintown REST lookup: %s", bandsintown_name)
        url = f"https://rest.bandsintown.com/artists/{requests.utils.quote(bandsintown_name)}/events"
        params = {"app_id": BANDSINTOWN_APP_IDS[artist], "date": "upcoming"}
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            events = resp.json()
        except Exception as exc:
            log.error("Bandsintown error for %s: %s", artist, exc)
            events = []

        for ev in events:
            venue = ev.get("venue", {})
            ticket_url = _bandsintown_event_url(ev)
            shows.append(
                Show(
                    artist=artist,
                    date=ev.get("datetime", "")[:10],
                    venue=_clean_venue_name(venue.get("name", ""), artist),
                    city=venue.get("city", ""),
                    region=venue.get("region", ""),
                    country=venue.get("country", ""),
                    ticket_url=ticket_url,
                    source="bandsintown",
                    raw_id=str(ev.get("id", "")),
                    start_time=_iso_time(ev.get("datetime", "")),
                )
            )
        log.info("Bandsintown REST: %d shows for %s", len(shows), artist)

    if not shows and has_widget:
        log.info("Trying Bandsintown widget scrape for %s", artist)
        shows = _fetch_bandsintown_via_widget(artist, BANDSINTOWN_WIDGET_PAGES[artist])

    return shows
