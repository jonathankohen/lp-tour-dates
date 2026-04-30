import logging

import requests

from config import TICKETMASTER_API_KEY, _key_set
from models import Show

log = logging.getLogger(__name__)


def fetch_ticketmaster(artist: str) -> list[Show]:
    """Fetch upcoming events from Ticketmaster Discovery API."""
    if not _key_set(TICKETMASTER_API_KEY):
        log.warning("TICKETMASTER_API_KEY not set, skipping Ticketmaster")
        return []
    url = "https://app.ticketmaster.com/discovery/v2/events.json"
    params = {
        "keyword": artist,
        "apikey": TICKETMASTER_API_KEY,
        "classificationName": "music",
        "sort": "date,asc",
        "size": 100,
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.error("Ticketmaster error for %s: %s", artist, exc)
        return []

    shows = []
    for ev in data.get("_embedded", {}).get("events", []):
        venues = ev.get("_embedded", {}).get("venues", [{}])
        v = venues[0] if venues else {}
        city = v.get("city", {}).get("name", "")
        region = v.get("state", {}).get("stateCode", "")
        country = v.get("country", {}).get("countryCode", "")
        date_str = ev.get("dates", {}).get("start", {}).get("localDate", "")
        ticket_url = ev.get("url", "")
        shows.append(
            Show(
                artist=artist,
                date=date_str,
                venue=v.get("name", ""),
                city=city,
                region=region,
                country=country,
                ticket_url=ticket_url,
                source="ticketmaster",
                raw_id=str(ev.get("id", "")),
            )
        )
    log.info("Ticketmaster: %d shows for %s", len(shows), artist)
    return shows
