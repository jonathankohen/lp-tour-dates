import logging

import requests

from config import SEATGEEK_CLIENT_ID, _key_set, _iso_time, act_name_matches
from models import Show

log = logging.getLogger(__name__)


def fetch_seatgeek(artist: str) -> list[Show]:
    """Fetch upcoming events from SeatGeek API."""
    if not _key_set(SEATGEEK_CLIENT_ID):
        log.warning("SEATGEEK_CLIENT_ID not set, skipping SeatGeek")
        return []
    url = "https://api.seatgeek.com/2/events"
    params = {
        "performers.slug": artist.lower().replace(" ", "-"),
        "client_id": SEATGEEK_CLIENT_ID,
        "per_page": 100,
        "sort": "datetime_local.asc",
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.error("SeatGeek error for %s: %s", artist, exc)
        return []

    shows = []
    for ev in data.get("events", []):
        venue = ev.get("venue", {})
        # SeatGeek flags shows with no confirmed time via time_tbd.
        start_time = "" if ev.get("time_tbd") else _iso_time(ev.get("datetime_local", ""))
        # Record the real performer name so the act-name guard can drop a similarly-named
        # act (the slug query is tighter than Ticketmaster's keyword, but still validated).
        # Check the performer names AND the event title/short_title — like Ticketmaster, the
        # act may be named correctly in the title even if a performer entry is a variant.
        perf_names = [p.get("name", "") for p in ev.get("performers", []) if p.get("name")]
        titles = [ev.get("title", ""), ev.get("short_title", "")]
        candidates = perf_names + [t for t in titles if t]
        performer = next(
            (n for n in candidates if act_name_matches(n, artist)),
            perf_names[0] if perf_names else ev.get("title", ""),
        )
        shows.append(
            Show(
                artist=artist,
                date=ev.get("datetime_local", "")[:10],
                venue=venue.get("name", ""),
                city=venue.get("city", ""),
                region=venue.get("state", ""),
                country=venue.get("country", ""),
                ticket_url=ev.get("url", ""),
                source="seatgeek",
                raw_id=str(ev.get("id", "")),
                start_time=start_time,
                performer=performer,
            )
        )
    log.info("SeatGeek: %d shows for %s", len(shows), artist)
    return shows
