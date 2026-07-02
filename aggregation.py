import logging
import re
from datetime import date as _date

import claude_state
from config import (US_ONLY_ARTISTS, CRUISE_ACTS, _US_REGION_TOKENS, WEB_SEARCH_SKIP_THRESHOLD,
                    _time_from_url, act_name_matches)
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


def _normalize_ticket_url(url: str) -> str:
    """Loosely normalize a ticket URL for equality: drop scheme, leading www, trailing slash."""
    u = re.sub(r"^https?://", "", url.strip().lower())
    u = re.sub(r"^www\.", "", u)
    return u.rstrip("/")


def _collapse_by_ticket_url(shows: list[Show]) -> list[Show]:
    """Merge records that are the SAME show under slightly different venue/country spellings
    but share a ticket URL.

    The MD5 dedup key includes the venue, so "Suquamish Clearwater Casino Resort" and
    "Suquamish Clearwater Resort Lawn" (same date, city, and ticket link) slip through as two
    rows. An act plays one show per (date, ticket link), so same artist+date+URL is the same
    event — keep the highest-priority source, backfilling a missing start_time from the dropped
    twin. Shows with no http ticket URL are passed through untouched (nothing reliable to
    merge on)."""
    best: dict[tuple, Show] = {}
    passthrough: list[Show] = []
    for show in shows:
        if not show.ticket_url.startswith("http"):
            passthrough.append(show)
            continue
        key = (show.artist, show.date, _normalize_ticket_url(show.ticket_url))
        cur = best.get(key)
        if cur is None:
            best[key] = show
            continue
        winner, loser = (
            (show, cur) if _SOURCE_PRIORITY.get(show.source.lower(), 99) < _SOURCE_PRIORITY.get(cur.source.lower(), 99)
            else (cur, show)
        )
        if not winner.start_time and loser.start_time:
            winner.start_time = loser.start_time
        best[key] = winner
    return list(best.values()) + passthrough


# Generic venue words that don't distinguish one venue from another in the same city.
_VENUE_STOPWORDS = {
    "the", "at", "for", "of", "and", "theatre", "theater", "center", "centre",
    "performing", "arts", "hall", "stage", "room", "live", "park", "amphitheater",
    "amphitheatre", "casino", "resort", "hotel",
}


def _venue_tokens(venue: str) -> set[str]:
    """Distinctive lowercased venue tokens (drops generic words like 'theater', 'center')."""
    return {t for t in re.split(r"[^a-z0-9]+", venue.lower())
            if len(t) >= 4 and t not in _VENUE_STOPWORDS}


def _collapse_by_city_venue(shows: list[Show]) -> list[Show]:
    """Merge same artist+date+city rows whose venue names share a distinctive token — the same
    show under two venue spellings from different sources ("...Clearwater Casino Resort" vs
    "...Clearwater Resort Lawn", "Yucaipa Performing Arts Center" vs "...Indoor Theatre").

    An act rarely plays two different venues in one city on one day, so a shared distinctive
    token (e.g. "clearwater", "yucaipa") is strong evidence it's one show. Keeps the
    higher-priority source's record, backfilling a missing URL/time from the twin. Rows with no
    distinctive venue token (or no city) are left untouched to avoid over-merging.
    """
    groups: dict[tuple, list[Show]] = {}
    out: list[Show] = []
    for s in shows:
        city = s.city.lower().strip()
        if city:
            groups.setdefault((s.artist.lower(), s.date, city), []).append(s)
        else:
            out.append(s)
    for group in groups.values():
        reps: list[tuple[Show, set[str]]] = []
        for s in group:
            toks = _venue_tokens(s.venue)
            for i, (rep, rtoks) in enumerate(reps):
                if toks and rtoks and (toks & rtoks):  # same show, different spelling
                    if _SOURCE_PRIORITY.get(s.source.lower(), 99) < _SOURCE_PRIORITY.get(rep.source.lower(), 99):
                        rep, s = s, rep  # the higher-priority record becomes the kept one
                    if not rep.ticket_url and s.ticket_url:
                        rep.ticket_url = s.ticket_url
                    if not rep.start_time and s.start_time:
                        rep.start_time = s.start_time
                    reps[i] = (rep, rtoks | toks)
                    break
            else:
                reps.append((s, toks))
        out.extend(rep for rep, _ in reps)
    return out


def dedup_for_publish(shows: list[Show]) -> list[Show]:
    """Final dedup applied to whatever is posted to the front-end (the authoritative output) —
    whether it came straight from aggregation or from a Sheet read-back. Collapses same-URL and
    same-city/venue-spelling duplicates so the public calendar never shows a date twice."""
    return _collapse_by_city_venue(_collapse_by_ticket_url(shows))


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
    # Second pass: collapse same-show rows that the venue-keyed MD5 pass can't catch — first by
    # shared ticket URL, then by same city + overlapping venue tokens (different spellings).
    deduped = _collapse_by_city_venue(_collapse_by_ticket_url(list(seen.values())))
    today = _date.today().isoformat()
    return sorted((s for s in deduped if s.date >= today), key=lambda s: s.date)


# Normalized country values that count as the United States.
_US_COUNTRY_VALUES = {"US", "USA", "UNITEDSTATES", "UNITEDSTATESOFAMERICA"}


def _norm_alpha(s: str) -> str:
    return re.sub(r"[^A-Z]", "", s.upper())


def _has_us_signal(show: Show) -> bool:
    """A positive indication the show is in the US: a US country value, or a region that is a
    US state (code or full name) or US territory."""
    if _norm_alpha(show.country) in _US_COUNTRY_VALUES:
        return True
    return _norm_alpha(show.region) in _US_REGION_TOKENS


def _has_nonus_signal(show: Show) -> bool:
    """A positive indication the show is NOT in the US: a non-empty, non-US country label."""
    country = _norm_alpha(show.country)
    return bool(country) and country not in _US_COUNTRY_VALUES


def _is_us_show(show: Show, strict: bool = False) -> bool:
    """Whether to KEEP a show under the US-only policy.

    - A positive US signal (US country, or a US-state/territory region) is always kept.
    - A positive non-US signal (an explicit foreign country label) is always dropped.
    - Otherwise the location is ambiguous (blank/unlabelled): lenient mode KEEPS it (protects
      US residencies like Reza whose city/region/country columns are blank), while STRICT mode
      (US_ONLY_ARTISTS — acts that tour abroad with unlabelled foreign dates like The Dolly
      Show's UK towns) DROPS it.
    """
    if _has_us_signal(show):
        return True
    if _has_nonus_signal(show):
        return False
    return not strict


def _venue_is_meaningful(venue: str) -> bool:
    """True if the venue name contains a real word (an alphabetic token ≥ 4 chars). Distinguishes
    a genuine named venue like "Kaatsbaan 2026 Annual Festival" (which may legitimately arrive
    with no city) from poster-scrape noise codes like "ST"/"IC"."""
    return any(len(t) >= 4 and t.isalpha() for t in re.split(r"[^a-z0-9]+", venue.lower()))


def _is_locatable(show: Show) -> bool:
    """A show is publishable only if a reader can tell WHERE it is or click a link.

    The poster-image vision scrape sometimes emits rows with only a date and a cryptic
    venue token (e.g. an unresolved cruise-ship code like "ST"/"IC") and no city, region,
    country, or ticket URL — unactionable noise. Drop those; keep anything with a location
    (city/region/country), a ticket link, OR a real named venue (so a legit festival/venue
    with no listed city, like Calpulli's "Kaatsbaan 2026 Annual Festival", isn't lost).
    """
    return bool(show.city.strip() or show.region.strip()
                or show.country.strip() or show.ticket_url.strip()
                or _venue_is_meaningful(show.venue))


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


def _filter_web_search_shows(shows: list[Show], artist: str) -> list[Show]:
    """Constrain the web-search source to only ADD genuinely new, actionable dates.

    Claude web-search is the least reliable source and routinely resurfaces a show another
    source already reported, under a different venue/city name — e.g. "Concerts in the Clouds,
    Moultonborough" for the same night as "Great Waters Music Festival, Wolfeboro". Those differ
    in both venue and city, so the venue-token dedup can't collapse them and a duplicate ships.

    So a web-search show is kept only when it (a) carries a real http ticket link and (b) falls
    on a date NOT already covered by any other (non-web-search) source for this artist. Shows
    from every other source pass through untouched. Runs after the act-name guard so a dropped
    contaminated show never counts as an existing date.
    """
    listed_dates = {s.date for s in shows if s.source != "claude_web_search"}
    kept: list[Show] = []
    dropped = 0
    for s in shows:
        if s.source != "claude_web_search":
            kept.append(s)
        elif s.ticket_url.startswith("http") and s.date not in listed_dates:
            kept.append(s)
        else:
            dropped += 1
    if dropped:
        log.info(
            "Web-search filter: dropped %d show(s) for %s (no ticket link or date already listed)",
            dropped, artist,
        )
    return kept


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

    # Constrain web-search results to genuinely new, ticketed dates (no duplicate-shipping).
    all_shows = _filter_web_search_shows(all_shows, artist)

    deduped = _dedup_shows(all_shows)

    before = len(deduped)
    deduped = [s for s in deduped if _is_locatable(s)]
    dropped = before - len(deduped)
    if dropped:
        log.info("Dropped %d unlocatable show(s) for %s (no city/region/country/link)", dropped, artist)

    # US-only policy: applies to every artist EXCEPT the cruise acts (their ship itineraries
    # legitimately call on foreign ports). US_ONLY_ARTISTS get the strict variant.
    if artist not in CRUISE_ACTS:
        strict = artist in US_ONLY_ARTISTS
        before = len(deduped)
        deduped = [s for s in deduped if _is_us_show(s, strict=strict)]
        dropped = before - len(deduped)
        if dropped:
            log.info("Dropped %d non-US show(s) for %s (US-only%s)",
                     dropped, artist, ", strict" if strict else "")

    if enrich:
        fallbacks = {s.dedup_key(): s.ticket_url for s in all_shows if s.ticket_url}
        enrich_ticket_urls_for_artist(deduped, fallbacks)
        # Fill any still-missing start times from the ticket page content (no Claude).
        fill_start_times_from_pages(deduped)

    return deduped
