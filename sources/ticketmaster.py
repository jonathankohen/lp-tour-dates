import logging

import requests

from config import TICKETMASTER_API_KEY, _key_set, act_name_matches
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
        start = ev.get("dates", {}).get("start", {})
        date_str = start.get("localDate", "")
        # localTime is "HH:MM:SS"; honor the API's TBA/unspecified flags.
        if start.get("timeTBA") or start.get("dateTBA") or start.get("noSpecificTime"):
            start_time = ""
        else:
            start_time = start.get("localTime", "")[:5]
        ticket_url = ev.get("url", "")
        # The keyword search is fuzzy, so an event for a similarly-named act (e.g.
        # "Queen by The Bohemians" when searching "Bohemian Queen") can come back. Record a
        # name so the act-name guard can drop the mismatch. Check BOTH the attraction names
        # AND the event title — TM often files our act under a mangled attraction ("Dolly the
        # Show") while the event title is correct ("The Dolly Show starring Kelly O'Brien"),
        # or the reverse. Accept if EITHER names the act; otherwise keep the attraction (or
        # event title) so the audit shows exactly what was matched.
        event_name = ev.get("name", "")
        att_names = [a.get("name", "") for a in ev.get("_embedded", {}).get("attractions", []) if a.get("name")]
        candidates = att_names + ([event_name] if event_name else [])
        performer = next(
            (n for n in candidates if act_name_matches(n, artist)),
            att_names[0] if att_names else event_name,
        )
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
                start_time=start_time,
                performer=performer,
            )
        )
    log.info("Ticketmaster: %d shows for %s", len(shows), artist)
    return shows
