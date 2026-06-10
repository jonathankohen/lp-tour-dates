"""
Tour Date Automation Tool
Aggregates tour dates from multiple sources and publishes to output destinations.
"""

import logging
import sys
from datetime import date as _date, datetime as _datetime

import anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# config must be imported after logging is configured so submodule loggers work correctly
import claude_state
from config import (
    BAND_NAMES,
    ANTHROPIC_API_KEY,
    CLAUDE_MODEL,
    CLAUDE_CALL_LIMIT,
    _is_platform_url,
)
from models import Show
from aggregation import aggregate
from enrichment import enrich_ticket_urls_for_artist, enrich_ticket_urls_all
from airtable import fetch_airtable_priority_artists
from sources.ticketmaster import fetch_ticketmaster
from sources.artist_website import fetch_artist_website
from sources.claude_web_search import fetch_claude_web_search
from outputs.json_output import write_json
from outputs.sheets import write_google_sheets
from outputs.doc import write_google_doc
from outputs.website import write_website
from outputs.wordpress_events import publish_events, cleanup_duplicate_events
from outputs.blocking_email_doc import write_blocking_email_doc
from utils import build_doc_from_sheets, read_shows_from_sheets


def run() -> None:
    artist_records = fetch_airtable_priority_artists()
    if not artist_records:
        log.warning("Airtable fetch returned empty — falling back to hardcoded BAND_NAMES")
        artist_records = [{"name": n, "priority": "hardcoded"} for n in BAND_NAMES]

    artist_names = [r["name"] for r in artist_records]
    if not artist_names:
        log.error("No artists to process.")
        return

    source = "hardcoded fallback" if any(r["priority"] == "hardcoded" for r in artist_records) else "Airtable"
    log.info("Processing %d artists (source: %s)", len(artist_names), source)

    # Phase 1: Per-artist — APIs + website scrape (Claude) + web search (Claude, threshold-gated)
    all_shows: list[Show] = []
    for artist in artist_names:
        log.info("=== %s ===", artist)
        shows = aggregate(artist, enrich=False)
        log.info("  -> %d shows", len(shows))
        all_shows.extend(shows)

    all_shows.sort(key=lambda s: (s.date, s.artist))

    # Phase 2: ONE batch Claude call — venue-direct ticket URLs for all artists
    enrich_ticket_urls_all(all_shows)

    log.info(
        "Total Claude API calls: %d / %d  |  Est. cost: $%.4f / $%.2f cap",
        claude_state._claude_call_count, CLAUDE_CALL_LIMIT,
        claude_state._estimated_cost_usd, claude_state.COST_CAP_USD,
    )

    write_json(all_shows)
    write_google_sheets(all_shows)
    write_google_doc(all_shows)
    write_website(all_shows)

    log.info("Done. %d total shows across %d artists.", len(all_shows), len(artist_names))


def test_sheets() -> None:
    dummy = [
        Show(artist="Test Artist", date="2026-06-01", venue="Venue A", city="Nashville", region="TN", country="US", ticket_url="https://example.com/1", source="test"),
        Show(artist="Test Artist", date="2026-06-04", venue="Venue B", city="Atlanta", region="GA", country="US", ticket_url="https://example.com/2", source="test"),
        Show(artist="Test Artist", date="2026-06-20", venue="Venue C", city="Chicago", region="IL", country="US", ticket_url="https://example.com/3", source="test"),
        Show(artist="Test Artist 2", date="2026-07-10", venue="Venue D", city="London", region="", country="GB", ticket_url="", source="test"),
        Show(artist="Test Artist 2", date="2026-07-11", venue="Venue E", city="Manchester", region="", country="GB", ticket_url="", source="test"),
        Show(artist="Test Artist 2", date="2026-07-14", venue="Venue F", city="Edinburgh", region="", country="GB", ticket_url="", source="test"),
    ]
    log.info("Writing %d dummy shows to Google Sheets...", len(dummy))
    write_google_sheets(dummy)
    log.info("Test complete.")


def test_ticketmaster() -> None:
    all_shows: list[Show] = []
    for artist in BAND_NAMES:
        log.info("Fetching Ticketmaster shows for: %s", artist)
        shows = fetch_ticketmaster(artist)
        log.info("  -> %d shows", len(shows))
        all_shows.extend(shows)
    all_shows.sort(key=lambda s: (s.date, s.artist))
    log.info("Total: %d shows across %d artists", len(all_shows), len(BAND_NAMES))
    write_google_sheets(all_shows)
    log.info("Test complete.")


def test_claude() -> None:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=64,
        messages=[{"role": "user", "content": "Reply with only the word PONG."}],
    )
    log.info("Claude ping response: %s", resp.content[0].text.strip())


def test_claude_artist() -> None:
    artist = BAND_NAMES[0]
    log.info("Testing Claude web search for: %s", artist)
    shows = fetch_claude_web_search(artist)
    log.info("Found %d shows", len(shows))
    write_google_sheets(shows)
    log.info("Test complete.")


def test_claude_calls() -> None:
    """Test artist website scrape + web search + enrichment for the first two artists, log results only — no Sheets write."""
    from hashlib import md5
    for artist in BAND_NAMES[:2]:
        log.info("=== Claude call test for: %s ===", artist)

        log.info("Step 1: Artist website scrape...")
        website_shows = fetch_artist_website(artist)
        log.info("  -> %d shows from artist website", len(website_shows))

        log.info("Step 2: Web search...")
        shows = fetch_claude_web_search(artist)
        log.info("  -> %d shows from web search", len(shows))

        seen: dict[str, Show] = {}
        for s in website_shows + shows:
            k = s.dedup_key()
            if k not in seen or (s.source == "artist_website" and seen[k].source != "artist_website"):
                seen[k] = s
        shows = sorted(seen.values(), key=lambda s: s.date)
        log.info("  -> %d shows after merge+dedup", len(shows))

        log.info("Step 2: Enrichment (venue-direct ticket URLs)...")
        fallbacks: dict[str, str] = {
            s.dedup_key(): s.ticket_url for s in shows if s.ticket_url
        }
        enrich_ticket_urls_for_artist(shows, fallbacks)

        log.info("Results after enrichment:")
        venue_direct = 0
        for s in sorted(shows, key=lambda s: s.date):
            is_direct = s.ticket_url and not _is_platform_url(s.ticket_url)
            tag = "VENUE" if is_direct else "platform" if s.ticket_url else "none"
            if is_direct:
                venue_direct += 1
            log.info(
                "  [%s] %s | %s, %s | %s",
                tag,
                s.date,
                s.venue,
                s.city,
                s.ticket_url or "",
            )

        log.info(
            "Summary: %d/%d shows have venue-direct URLs", venue_direct, len(shows)
        )

    log.info("Total Claude calls this test: %d", claude_state._claude_call_count)
    log.info("Test complete.")


def test_doc() -> None:
    """Write dummy shows to the Google Doc to verify tab/subtab structure."""
    dummy = [
        Show(artist="Test Artist", date="2026-06-01", venue="Venue A", city="Nashville", region="TN", country="US", ticket_url="", source="test"),
        Show(artist="Test Artist", date="2026-06-05", venue="Venue B", city="Atlanta", region="GA", country="US", ticket_url="", source="test"),
        Show(artist="Test Artist", date="2026-07-10", venue="Venue C", city="Chicago", region="IL", country="US", ticket_url="", source="test"),
        Show(artist="Test Artist 2", date="2026-06-15", venue="Venue D", city="Austin", region="TX", country="US", ticket_url="", source="test"),
        Show(artist="Test Artist 2", date="2026-06-20", venue="Venue E", city="Dallas", region="TX", country="US", ticket_url="", source="test"),
    ]
    log.info("Writing %d dummy shows to Google Doc...", len(dummy))
    write_google_doc(dummy)
    log.info("Test complete.")


def _cli_value(name: str, default: str = "") -> str:
    """Read a `--name value` or `--name=value` CLI argument; default if absent."""
    flag = f"--{name}"
    for i, arg in enumerate(sys.argv):
        if arg == flag and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if arg.startswith(flag + "="):
            return arg.split("=", 1)[1]
    return default


def _normalize_time(raw: str) -> str | None:
    """Parse a 12h or 24h clock time into a 12-hour display string like "7:00 PM".

    Accepts "7:00 PM", "7pm", "7:00", "19:00", etc. Empty input returns "" (no
    time set). Returns None when the value can't be parsed as a time.
    """
    s = raw.strip().upper().replace(".", "")
    if not s:
        return ""
    for fmt in ("%I:%M %p", "%I:%M%p", "%I %p", "%I%p", "%H:%M", "%H"):
        try:
            t = _datetime.strptime(s, fmt)
        except ValueError:
            continue
        return f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"
    return None


def _cli_values(name: str) -> list[str]:
    """Collect every `--name value` / `--name=value`, splitting comma-joined values."""
    flag = f"--{name}"
    out: list[str] = []
    for i, arg in enumerate(sys.argv):
        if arg == flag and i + 1 < len(sys.argv):
            out.append(sys.argv[i + 1])
        elif arg.startswith(flag + "="):
            out.append(arg.split("=", 1)[1])
    return [v.strip() for raw in out for v in raw.split(",") if v.strip()]


if __name__ == "__main__":
    if "--debug" in sys.argv:
        logging.getLogger().setLevel(logging.DEBUG)

    if "--blocking-email-doc" in sys.argv:
        shows = read_shows_from_sheets()
        if not shows:
            log.error("No shows read from sheets — aborting blocking email doc update.")
        else:
            shows.sort(key=lambda s: (s.date, s.artist))
            write_blocking_email_doc(shows)
    elif "--publish-events" in sys.argv:
        dry_run = "--dry-run" in sys.argv
        one_month = "--one-month" in sys.argv
        limit = 0
        for i, arg in enumerate(sys.argv):
            if arg == "--limit" and i + 1 < len(sys.argv):
                val = sys.argv[i + 1]
            elif arg.startswith("--limit="):
                val = arg.split("=", 1)[1]
            else:
                continue
            try:
                limit = int(val)
            except ValueError:
                log.error("--limit requires an integer, e.g.: --limit 3 or --limit=3")
            break
        artist_filter = ""
        for i, arg in enumerate(sys.argv):
            if arg == "--artist" and i + 1 < len(sys.argv):
                artist_filter = sys.argv[i + 1]
            elif arg.startswith("--artist="):
                artist_filter = arg.split("=", 1)[1]
            else:
                continue
            break
        shows = read_shows_from_sheets()
        if not shows:
            log.error("No shows read from sheets — aborting event publish.")
        else:
            if artist_filter:
                needle = artist_filter.lower()
                matched = [s for s in shows if needle in s.artist.lower()]
                if not matched:
                    log.error("No shows match artist filter %r — aborting event publish.", artist_filter)
                    sys.exit(1)
                log.info("Artist filter %r matched %d of %d shows.", artist_filter, len(matched), len(shows))
                shows = matched
            shows.sort(key=lambda s: (s.date, s.artist))
            log.info("%s %d shows to WordPress events...", "Dry-run for" if dry_run else "Publishing", len(shows))
            publish_events(shows, dry_run=dry_run, limit=limit, one_month=one_month)
    elif "--add-show" in sys.argv:
        # Manually publish a single show that the scrapers didn't pick up.
        dry_run = "--dry-run" in sys.argv
        artist = _cli_value("artist")
        dates = _cli_values("date")                # one or more YYYY-MM-DD (repeat or comma-join)
        venue = _cli_value("venue")
        city = _cli_value("city")
        region = _cli_value("region")              # state/province, e.g. "TN"
        country = _cli_value("country", "US")
        ticket_url = _cli_value("ticket-url") or _cli_value("ticket-link")
        time_raw = _cli_value("time")              # optional clock time, 12h or 24h
        start_time = _normalize_time(time_raw)     # normalized to "7:00 PM"; None if unparseable
        title = _cli_value("title")                # optional event title override
        source = _cli_value("source", "manual")

        missing = [f"--{n}" for n, v in (("artist", artist), ("date", dates),
                                         ("venue", venue), ("city", city)) if not v]
        valid = True
        if missing:
            log.error("--add-show requires %s. Example: --add-show --artist \"Tony Danza\" "
                      "--date 2026-08-15 --date 2026-08-16 --venue \"The Fillmore\" "
                      "--city Detroit --region MI --ticket-url https://example.com/tix --time \"8:00 PM\"",
                      ", ".join(missing))
            valid = False
        else:
            for d in dates:
                try:
                    _date.fromisoformat(d)
                except ValueError:
                    log.error("--date must be ISO 8601 (YYYY-MM-DD), got %r.", d)
                    valid = False
            if start_time is None:
                log.error("--time must be a clock time, e.g. \"7:00 PM\" or 19:00, got %r.", time_raw)
                valid = False
                start_time = ""
        if valid:
            shows = [Show(artist=artist, date=d, venue=venue, city=city, region=region,
                          country=country, ticket_url=ticket_url, source=source,
                          start_time=start_time, title=title) for d in sorted(set(dates))]
            log.info("%s %d show(s): %s | %s | %s, %s%s%s",
                     "Dry-run for" if dry_run else "Publishing", len(shows),
                     title or artist, ", ".join(s.date for s in shows), venue, city,
                     f" @ {start_time}" if start_time else "",
                     f' (title: "{title}")' if title else "")
            publish_events(shows, dry_run=dry_run)
    elif "--cleanup-duplicates" in sys.argv:
        # Report by default; --apply trashes the surplus, --force-delete deletes for good.
        apply = "--apply" in sys.argv
        force_delete = "--force-delete" in sys.argv
        cleanup_duplicate_events(dry_run=not apply, force_delete=force_delete)
    elif "--doc-from-sheets" in sys.argv:
        build_doc_from_sheets()
    elif "--test-doc" in sys.argv:
        test_doc()
    elif "--test-sheets" in sys.argv:
        test_sheets()
    elif "--test-ticketmaster" in sys.argv:
        test_ticketmaster()
    elif "--test-claude" in sys.argv:
        test_claude()
    elif "--test-claude-artist" in sys.argv:
        test_claude_artist()
    elif "--test-claude-calls" in sys.argv:
        test_claude_calls()
    elif "--test-airtable" in sys.argv:
        artists = fetch_airtable_priority_artists()
        print(f"\n{'Priority':<22} Artist")
        print("-" * 60)
        for a in artists:
            print(f"{a['priority']:<22} {a['name']}")
        print(f"\nTotal: {len(artists)} artists")
    elif "--artist" in sys.argv:
        idx = sys.argv.index("--artist")
        artist_arg = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
        if not artist_arg:
            log.error("--artist requires a name, e.g.: --artist \"Kiss The Sky: A Jimi Hendrix Tribute\"")
        else:
            log.info("=== Single-artist run: %s ===", artist_arg)
            shows = aggregate(artist_arg)
            log.info("  -> %d shows", len(shows))
            for s in shows:
                tag = "VENUE" if s.ticket_url and not _is_platform_url(s.ticket_url) else "platform" if s.ticket_url else "none"
                log.info("  [%s] %s | %s, %s | %s", tag, s.date, s.venue, s.city, s.ticket_url or "")
            write_google_sheets(shows, reorder=False)
            write_google_doc(shows, partial=True)
            write_blocking_email_doc(shows)
    else:
        run()
