import logging

import requests

from config import SEATGEEK_CLIENT_ID, _key_set
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
            )
        )
    log.info("SeatGeek: %d shows for %s", len(shows), artist)
    return shows
