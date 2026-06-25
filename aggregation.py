import logging
import re
from datetime import date as _date

import claude_state
from config import US_ONLY_ARTISTS, WEB_SEARCH_SKIP_THRESHOLD, _time_from_url, act_name_matches
from models import Show
from sources.bandsintown import fetch_bandsintown
from sources.seatgeek import fetch_seatgeek
from sources.ticketmaster import fetch_ticketmaster
from sources.artist_website import fetch_artist_website
from sources.claude_web_search import fetch_claude_web_search
from sources.back2mac_sheets import fetch_back2mac_sheets, ARTIST as BACK2MAC_ARTIST
from sources.ticket_page import fill_start_times_from_pages, fetch_page_text
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


def _url_start_times(shows: list[Show]) -> dict[str, str]:
    """Per dedup_key, a start_time recovered from an ISO datetime in any ticket URL.

    Used only as a fallback when no source supplied an explicit start_time — some
    venues embed the show time in the ticket URL (e.g. Reza's branson.direct links
    end in '...2026-06-25T20:00:00'). Prefers the most trusted source's URL.
    """
    best: dict[str, tuple[int, str]] = {}
    for show in shows:
        t = _time_from_url(show.ticket_url)
        if not t:
            continue
        key = show.dedup_key()
        rank = _TIME_PRIORITY.get(show.source, 99)
        if key not in best or rank < best[key][0]:
            best[key] = (rank, t)
    return {key: t for key, (_, t) in best.items()}


def _dedup_shows(shows: list[Show]) -> list[Show]:
    """Deduplicate shows keeping highest-priority source, filter to future dates."""
    seen: dict[str, Show] = {}
    for show in shows:
        key = show.dedup_key()
        if key not in seen or _SOURCE_PRIORITY.get(show.source, 99) < _SOURCE_PRIORITY.get(seen[key].source, 99):
            seen[key] = show
    # APIs win on time: stamp the kept record with the best time across all duplicates.
    # Fall back to a time embedded in the ticket URL only when no source gave one.
    best_times = _best_start_times(shows)
    url_times = _url_start_times(shows)
    for key, show in seen.items():
        if best_times.get(key):
            show.start_time = best_times[key]
        elif url_times.get(key):
            show.start_time = url_times[key]
    today = _date.today().isoformat()
    return sorted((s for s in seen.values() if s.date >= today), key=lambda s: s.date)


# Normalized country values that count as the United States. An empty country is
# treated as US: sources leave it blank when a US state was parsed but the country
# label was not (e.g. artist_website sets country="US" only when a region is present).
_US_COUNTRY_VALUES = {"US", "USA", "UNITEDSTATES", "UNITEDSTATESOFAMERICA"}


def _is_us_show(show: Show) -> bool:
    norm = re.sub(r"[^A-Z]", "", show.country.upper())
    return norm == "" or norm in _US_COUNTRY_VALUES


# Sources that report a real performer/attraction name we can validate (structured APIs).
# A show from one of these is dropped when its performer name doesn't name the act.
_STRUCTURED_SOURCES = {"bandsintown", "seatgeek", "ticketmaster"}


def _collect_shows(artist: str, claude: bool = True) -> list[Show]:
    """Fetch the raw shows for an artist from every source (pre-dedup, pre-guard)."""
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
    return all_shows


def _act_name_check(show: Show, artist: str) -> tuple[bool, str]:
    """Decide whether `show` really belongs to `artist`. Returns (passed, reason).

    Structured-API shows carry a `performer` name from the source — a fuzzy keyword match
    for a similarly-named act (e.g. "Queen by The Bohemians" vs "Bohemian Queen") is dropped
    here. Claude web-search shows have no performer field, so they're confirmed against their
    ticket page: dropped ONLY on positive disconfirmation (a page that loads but never names
    the act). A show with no usable URL / unreachable page is kept (can't disprove it) and is
    surfaced by `--audit-names` instead. Never makes a Claude call.
    """
    if show.source in _STRUCTURED_SOURCES and show.performer:
        if not act_name_matches(show.performer, artist):
            return False, f"performer '{show.performer}' is not {artist}"
    elif show.source == "claude_web_search" and show.ticket_url.startswith("http"):
        text = fetch_page_text(show.ticket_url)
        if text and not act_name_matches(text, artist):
            # The static HTML didn't name the act — but venue/ticketing pages routinely inject
            # the act name via JS, so a plain fetch can miss it. Render with a headless browser
            # before dropping, so a JS-only name isn't a false disconfirmation. If the render
            # fails or returns nothing, we can't disprove the show, so keep it (audit flags it).
            rendered = fetch_page_text(show.ticket_url, render=True, force_render=True)
            if rendered and not act_name_matches(rendered, artist):
                return False, "ticket page does not name the act (checked rendered page)"
    return True, ""


def _filter_by_act_name(shows: list[Show], artist: str) -> list[Show]:
    """Drop shows whose real performer/page doesn't name the act (see `_act_name_check`)."""
    kept: list[Show] = []
    for s in shows:
        passed, reason = _act_name_check(s, artist)
        if passed:
            kept.append(s)
        else:
            log.info("Act-name guard dropped %s show on %s: %s", s.source, s.date, reason)
    dropped = len(shows) - len(kept)
    if dropped:
        log.info("Act-name guard: dropped %d show(s) for %s", dropped, artist)
    return kept


def audit_act_names(artist: str, claude: bool = True) -> list[tuple[Show, bool, str]]:
    """Read-only: collect every raw show for an artist and annotate each with whether it
    passes the act-name guard and why. Used by the `--audit-names` CLI; writes nothing."""
    return [(s, *_act_name_check(s, artist)) for s in _collect_shows(artist, claude=claude)]


def aggregate(artist: str, enrich: bool = True, claude: bool = True) -> list[Show]:
    """
    Collect shows from all sources, deduplicate, then optionally enrich ticket links.
    Show dedup priority: Bandsintown > SeatGeek > artist_website > Ticketmaster > Claude web search.
    Ticket URL priority: venue-direct (via Claude) > platform fallback.
    Pass enrich=False when run() will do a single batched enrichment call for all artists.
    """
    all_shows = _collect_shows(artist, claude=claude)

    # Drop cross-act contamination BEFORE dedup so a mislabeled show can't win dedup or
    # leak its ticket URL into the enrichment fallback map.
    all_shows = _filter_by_act_name(all_shows, artist)

    deduped = _dedup_shows(all_shows)

    if artist in US_ONLY_ARTISTS:
        before = len(deduped)
        deduped = [s for s in deduped if _is_us_show(s)]
        dropped = before - len(deduped)
        if dropped:
            log.info("Dropped %d non-US show(s) for %s (US-only)", dropped, artist)

    if enrich:
        fallbacks = {s.dedup_key(): s.ticket_url for s in all_shows if s.ticket_url}
        enrich_ticket_urls_for_artist(deduped, fallbacks)
        # Fill any still-missing start times from the ticket page content (no Claude).
        fill_start_times_from_pages(deduped)

    return deduped
