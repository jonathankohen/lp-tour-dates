import logging
from datetime import date as _date

import claude_state
from config import WEB_SEARCH_SKIP_THRESHOLD
from models import Show
from sources.bandsintown import fetch_bandsintown
from sources.seatgeek import fetch_seatgeek
from sources.ticketmaster import fetch_ticketmaster
from sources.artist_website import fetch_artist_website
from sources.claude_web_search import fetch_claude_web_search
from sources.back2mac_sheets import fetch_back2mac_sheets, ARTIST as BACK2MAC_ARTIST
from enrichment import enrich_ticket_urls_for_artist

log = logging.getLogger(__name__)

_SOURCE_PRIORITY = {
    "bandsintown": 0,
    "seatgeek": 1,
    "artist_website": 2,
    "ticketmaster": 3,
    "claude_web_search": 4,
    "back2mac_sheets": 5,  # lowest priority — provides dates but no venue; API sources win on overlap
}

# Start-time trust order, distinct from the record priority above: structured APIs
# carry an authoritative local time, so they outrank Claude-extracted times even when
# the kept record itself came from a lower-priority source (e.g. artist_website).
_TIME_PRIORITY = {
    "bandsintown": 0,
    "seatgeek": 1,
    "ticketmaster": 2,
    "artist_website": 3,
    "claude_web_search": 4,
    "back2mac_sheets": 5,
}


def _best_start_times(shows: list[Show]) -> dict[str, str]:
    """Per dedup_key, the start_time from the most trusted source that has one."""
    best: dict[str, tuple[int, str]] = {}
    for show in shows:
        if not show.start_time:
            continue
        key = show.dedup_key()
        rank = _TIME_PRIORITY.get(show.source, 99)
        if key not in best or rank < best[key][0]:
            best[key] = (rank, show.start_time)
    return {key: t for key, (_, t) in best.items()}


def _dedup_shows(shows: list[Show]) -> list[Show]:
    """Deduplicate shows keeping highest-priority source, filter to future dates."""
    seen: dict[str, Show] = {}
    for show in shows:
        key = show.dedup_key()
        if key not in seen or _SOURCE_PRIORITY.get(show.source, 99) < _SOURCE_PRIORITY.get(seen[key].source, 99):
            seen[key] = show
    # APIs win on time: stamp the kept record with the best time across all duplicates.
    best_times = _best_start_times(shows)
    for key, show in seen.items():
        if best_times.get(key):
            show.start_time = best_times[key]
    today = _date.today().isoformat()
    return sorted((s for s in seen.values() if s.date >= today), key=lambda s: s.date)


def aggregate(artist: str, enrich: bool = True, claude: bool = True) -> list[Show]:
    """
    Collect shows from all sources, deduplicate, then optionally enrich ticket links.
    Show dedup priority: Bandsintown > SeatGeek > artist_website > Ticketmaster > Claude web search.
    Ticket URL priority: venue-direct (via Claude) > platform fallback.
    Pass enrich=False when run() will do a single batched enrichment call for all artists.
    """
    all_shows: list[Show] = []
    all_shows.extend(fetch_bandsintown(artist))
    all_shows.extend(fetch_seatgeek(artist))
    if artist == BACK2MAC_ARTIST:
        all_shows.extend(fetch_back2mac_sheets())
    if claude:
        all_shows.extend(fetch_artist_website(artist))
    all_shows.extend(fetch_ticketmaster(artist))

    if claude:
        api_show_count = sum(
            1 for s in all_shows if s.source in ("bandsintown", "seatgeek", "ticketmaster")
        )
        if api_show_count >= WEB_SEARCH_SKIP_THRESHOLD:
            log.info("Skipping web search for %s — %d API shows found", artist, api_show_count)
        elif claude_state._under_cost_cap(f"web_search:{artist}"):
            all_shows.extend(fetch_claude_web_search(artist))

    deduped = _dedup_shows(all_shows)

    if enrich:
        fallbacks = {s.dedup_key(): s.ticket_url for s in all_shows if s.ticket_url}
        enrich_ticket_urls_for_artist(deduped, fallbacks)

    return deduped
