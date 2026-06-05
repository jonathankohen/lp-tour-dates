import json
import logging
import re

import requests

from config import BANDSINTOWN_APP_IDS, BANDSINTOWN_ARTIST_NAMES, BANDSINTOWN_WIDGET_PAGES, _iso_time
from models import Show

log = logging.getLogger(__name__)


def _fetch_bandsintown_via_widget(artist: str, page_url: str) -> list[Show]:
    """
    Load an artist's tour page in a headless browser and intercept the Bandsintown
    API response the widget makes internally. Used when the REST API returns 0.
    Requires: playwright install chromium
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        log.warning("playwright not installed — skipping widget scrape for %s", artist)
        return []

    captured: list[dict] = []

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()

            # Wait specifically for the Bandsintown events API call the widget makes,
            # rather than waiting for all network activity to stop (which never happens).
            with page.expect_response(
                lambda r: "rest.bandsintown.com" in r.url and "/events" in r.url,
                timeout=20000,
            ) as response_info:
                page.goto(page_url, wait_until="domcontentloaded", timeout=30000)

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
            browser.close()
    except Exception as exc:
        log.error("Playwright widget scrape error for %s: %s", artist, exc)
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
        offers = ev.get("offers", [])
        ticket_url = offers[0].get("url", "") if offers else ""
        shows.append(Show(
            artist=artist,
            date=dt,
            venue=venue.get("name", ""),
            city=venue.get("city", ""),
            region=venue.get("region", ""),
            country=venue.get("country", ""),
            ticket_url=ticket_url,
            source="bandsintown",
            start_time=_iso_time(dt_raw),
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
            offers = ev.get("offers", [])
            ticket_url = offers[0].get("url", "") if offers else ""
            shows.append(
                Show(
                    artist=artist,
                    date=ev.get("datetime", "")[:10],
                    venue=venue.get("name", ""),
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
