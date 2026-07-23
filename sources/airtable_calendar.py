"""The Airtable Show Calendar as a show source.

Every row on the Show Calendar is a **fully-executed contract** — the act is playing that
date whether or not any ticketing site has published it yet. So this source is the
pipeline's floor: a contracted show is never dropped for a missing ticket link, a missing
location, or an unlabelled (foreign-looking) address. The exemptions live in
`aggregation` and all key off `SOURCE_NAME`.

It is also the *poorest* record we hold — no ticket URL, no start time, sometimes no city —
so it carries the LOWEST dedup priority. When a real ticketing source reports the same show,
that richer record wins and the contract row collapses into it; the show is published either
way. A contract row survives on its own only when nothing else knows about the date yet,
which is exactly the gap this source exists to close.

The whole calendar is fetched once per process and served from a cache, so adding this
source costs one Airtable call per run rather than one per artist.
"""
import logging
import re

from airtable import fetch_airtable_show_calendar
from config import band_for_name
from models import Show

log = logging.getLogger(__name__)

SOURCE_NAME = "airtable_calendar"

# Used when a contracted row has no Venue and no City. It must not contain the word "TBA":
# the event publisher drops anything matching /\btba\b/ as an unannounced placeholder, and a
# signed contract is the opposite of that. It also has to be a real word so the
# unlocatable-show guard treats it as a named venue.
_VENUE_PLACEHOLDER = "Venue TBD"

_URL_RE = re.compile(r"https?://\S+")

_rows_cache: list[dict] | None = None
_claimed_ids: set[str] = set()
_coverage_logged = False


def reset_cache() -> None:
    """Forget the cached calendar (tests, and any long-lived process that re-runs a pipeline)."""
    global _rows_cache, _coverage_logged
    _rows_cache = None
    _claimed_ids.clear()
    _coverage_logged = False


def _rows() -> list[dict]:
    global _rows_cache
    if _rows_cache is None:
        _rows_cache = fetch_airtable_show_calendar(upcoming_only=True)
    return _rows_cache


def _clean(value: str) -> str:
    """Collapse the whitespace a hand-maintained Airtable cell picks up (embedded newlines,
    double spaces, trailing blanks) so it doesn't reach the Sheet or an event title."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _split_venue_link(venue: str) -> tuple[str, str]:
    """Split a venue cell into (venue, url). Bookers sometimes paste the venue's website into
    the Venue field ("Alaska Raceway https://www.raceak.com/"); that URL is a better-than-
    nothing ticket link, and it does not belong in the venue name."""
    url = ""
    match = _URL_RE.search(venue)
    if match:
        url = match.group(0).rstrip(".,;")
        venue = _URL_RE.sub(" ", venue)
    return _clean(venue), url


def _is_meaningful_venue(venue: str) -> bool:
    """True if the venue names something — an alphabetic token of 3+ chars. Filters the
    placeholder cells ("??", "-") a calendar row can carry before the venue is confirmed."""
    return any(len(t) >= 3 and t.isalpha() for t in re.split(r"[^A-Za-z0-9]+", venue))


def fetch_airtable_calendar(artist: str) -> list[Show]:
    """Contracted shows for `artist` from the Airtable Show Calendar.

    Rows are matched to the act via `band_for_name` on the row's slug, so a slug the roster
    doesn't recognise yields nothing here and is reported by `log_calendar_coverage()`
    rather than disappearing.
    """
    shows: list[Show] = []
    for row in _rows():
        if band_for_name(row.get("slug", "")) != artist:
            continue
        venue, url = _split_venue_link(row.get("venue", ""))
        city = _clean(row.get("city", ""))
        # A contracted show must survive the Sheet round-trip, which skips venueless rows, so
        # an unnamed venue falls back to the city and finally to an explicit placeholder.
        if not _is_meaningful_venue(venue):
            venue = city or _VENUE_PLACEHOLDER
        shows.append(Show(
            artist=artist,
            date=row["date"],
            venue=venue,
            city=city,
            region=_clean(row.get("region", "")),
            country="",  # the calendar has no country field; don't invent one
            ticket_url=url,  # usually "" — enrichment may find a real one later
            source=SOURCE_NAME,
            raw_id=row.get("record_id", ""),
        ))
        if row.get("record_id"):
            _claimed_ids.add(row["record_id"])
    if shows:
        log.info("Airtable Show Calendar: %d contracted show(s) for %s", len(shows), artist)
    return shows


def log_calendar_coverage() -> None:
    """Report every contracted row this run did NOT pick up. Call once, after the per-artist
    aggregation loop.

    Two ways a signed contract can go unpublished, both of which have to be loud rather than
    silent: its act slug doesn't map to the roster (needs an `AIRTABLE_SLUG_ALIASES` entry, or
    it's genuinely an off-roster act we don't publish), or it maps to a roster act that wasn't
    in this run's artist list at all.
    """
    global _coverage_logged
    if _coverage_logged or _rows_cache is None:
        return
    _coverage_logged = True

    unmapped: dict[str, int] = {}
    unclaimed: dict[str, int] = {}
    for row in _rows_cache:
        if row.get("record_id") in _claimed_ids:
            continue
        band = band_for_name(row.get("slug", ""))
        bucket = unclaimed if band else unmapped
        bucket[band or (row.get("slug") or "(no act link)")] = bucket.get(
            band or (row.get("slug") or "(no act link)"), 0) + 1

    if unmapped:
        log.warning(
            "Show Calendar: %d contracted row(s) not published — act slug is off-roster or "
            "unrecognised (add an AIRTABLE_SLUG_ALIASES entry if one of these is ours): %s",
            sum(unmapped.values()),
            ", ".join(f"{slug} x{n}" for slug, n in sorted(unmapped.items())),
        )
    if unclaimed:
        log.error(
            "Show Calendar: %d contracted row(s) for ROSTER acts that this run never "
            "processed — these shows are missing from the outputs: %s",
            sum(unclaimed.values()),
            ", ".join(f"{band} x{n}" for band, n in sorted(unclaimed.items())),
        )
