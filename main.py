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
    OUTPUT_WEBSITE_URL,
    _is_platform_url,
)
from models import Show
from aggregation import aggregate, audit_act_names
from enrichment import (
    enrich_ticket_urls_for_artist,
    enrich_ticket_urls_all,
    verify_fix_and_classify,
    find_event_ticket_urls,
    find_event_ticket_urls_via_search,
)
from airtable import fetch_airtable_priority_artists
from sources.ticketmaster import fetch_ticketmaster
from sources.artist_website import fetch_artist_website
from sources.claude_web_search import fetch_claude_web_search
from sources.ticket_page import fill_start_times_from_pages
from outputs.json_output import write_json
from outputs.sheets import write_google_sheets, update_sheet_ticket_urls
from outputs.doc import write_google_doc
from outputs.website import write_website
from outputs.wordpress_events import publish_events, cleanup_duplicate_events, update_event_descriptions, update_event_links, trash_events, update_event_images, resolve_media_attachment_id
from outputs.blocking_email_doc import write_blocking_email_doc
from utils import build_doc_from_sheets, read_shows_from_sheets, SheetReadError


def _preflight_act_name_tests() -> bool:
    """Run the act-name guard test suite before a full publish. Returns True to proceed.

    A regression in the matcher means wrong dates could be published (the Bohemian Queen /
    "Queen by The Bohemians" incident), so a failing suite ABORTS the run before any write.
    The tests are fast and hit no network. A missing pytest (dev-only dep) warns but does not
    block a production run.
    """
    try:
        import pytest  # noqa: F401
    except ImportError:
        log.warning("pytest not installed — skipping pre-flight act-name tests (`pip install pytest`)")
        return True
    import os
    import subprocess
    log.info("Pre-flight: running act-name guard tests...")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        log.error("Pre-flight act-name tests FAILED — aborting before any writes:\n%s",
                  (proc.stdout or "") + (proc.stderr or ""))
        return False
    summary = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "passed"
    log.info("Pre-flight tests passed (%s).", summary)
    return True


def _preflight_browser() -> bool:
    """Verify the Playwright chromium browser is installed before a full publish.

    The Bandsintown-widget and DOM-render sources silently return 0 shows when the browser
    binary is missing (this once shipped a large data loss with a green exit code). If the
    playwright *package* is absent we only warn (some minimal envs intentionally skip it), but
    a present package with a missing *browser* is the exact failure mode we must catch — abort.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        log.warning("playwright not installed — widget/render sources will be skipped.")
        return True
    import os
    try:
        with sync_playwright() as pw:
            path = pw.chromium.executable_path
        if not path or not os.path.exists(path):
            raise FileNotFoundError(path or "<no path>")
    except Exception as exc:
        log.error("Pre-flight: Playwright chromium browser is not installed (%s). "
                  "Run:  .venv/bin/playwright install chromium", exc)
        return False
    log.info("Pre-flight: Playwright chromium browser present.")
    return True


# Regression guard: an artist that had a healthy show count last publish suddenly collapsing
# usually means a source broke (a timed-out widget scrape, a rate limit), not that the shows
# vanished. Keep the last-good data for that artist instead of publishing the drop.
_REGRESSION_MIN_PREV = 5       # only guard artists that previously had a real slate
_REGRESSION_KEEP_FRACTION = 0.4  # a drop to <= 40% (min 2) of the prior count trips the guard


def _future_show_counts(shows: list[Show], today: str) -> dict[str, int]:
    """Per-artist count of real, upcoming shows — excludes past dates and 'Open' filler rows so
    a Sheet read-back compares like-for-like with a fresh (future-only) aggregation."""
    counts: dict[str, int] = {}
    for s in shows:
        if s.date < today or s.venue.strip().lower() == "open":
            continue
        counts[s.artist] = counts.get(s.artist, 0) + 1
    return counts


def _detect_regressions(prev_counts: dict[str, int], fresh_counts: dict[str, int],
                        artist_names: list[str]) -> list[str]:
    """Artists whose fresh show count collapsed vs. the previously-published (Sheet) baseline:
    had >= _REGRESSION_MIN_PREV before and now <= max(2, 40% of prior). Pure/testable."""
    regressed: list[str] = []
    for artist in artist_names:
        prev, fresh = prev_counts.get(artist, 0), fresh_counts.get(artist, 0)
        if prev >= _REGRESSION_MIN_PREV and fresh <= max(2, int(_REGRESSION_KEEP_FRACTION * prev)):
            regressed.append(artist)
    return regressed


def run() -> int:
    """Full pipeline. Returns a process exit code: 0 on a clean run, 1 if it aborted or any
    artist failed / regressed (so the scheduled GitHub Action turns red instead of silently
    publishing degraded data)."""
    if not _preflight_act_name_tests():
        log.error("Aborting run() — fix the failing act-name tests before publishing.")
        return 1
    if not _preflight_browser():
        log.error("Aborting run() — install the Playwright browser before publishing.")
        return 1

    today = _date.today().isoformat()
    # Baseline = what's currently published (the Sheet), read BEFORE we overwrite it, so we can
    # catch an artist silently collapsing this run.
    # strict=False: this is only the regression-guard baseline. A partial read here can at
    # worst under-count an artist, which disables the guard for it — it never publishes data.
    prev_counts = _future_show_counts(read_shows_from_sheets(strict=False), today)

    artist_records = fetch_airtable_priority_artists()
    if not artist_records:
        log.warning("Airtable fetch returned empty — falling back to hardcoded BAND_NAMES")
        artist_records = [{"name": n, "priority": "hardcoded"} for n in BAND_NAMES]

    artist_names = [r["name"] for r in artist_records]
    if not artist_names:
        log.error("No artists to process.")
        return 1

    source = "hardcoded fallback" if any(r["priority"] == "hardcoded" for r in artist_records) else "Airtable"
    log.info("Processing %d artists (source: %s)", len(artist_names), source)

    # Phase 1: Per-artist — APIs + website scrape (Claude) + web search (Claude, threshold-gated)
    all_shows: list[Show] = []
    failed_artists: list[str] = []
    fresh_counts: dict[str, int] = {}
    for artist in artist_names:
        log.info("=== %s ===", artist)
        try:
            shows = aggregate(artist, enrich=False)
        except Exception as exc:
            # One artist/source blowing up (e.g. an unreachable Google Sheet) must not abort
            # the whole publish — log it, skip the artist, and keep the rest of the roster.
            log.error("Aggregation failed for %s — skipping: %s", artist, exc)
            failed_artists.append(artist)
            continue
        log.info("  -> %d shows", len(shows))
        fresh_counts[artist] = len(shows)
        all_shows.extend(shows)
    if failed_artists:
        log.warning("Skipped %d artist(s) due to errors: %s", len(failed_artists), ", ".join(failed_artists))

    # Regression guard: an artist whose slate collapsed vs. what's already published almost
    # always means a source broke this run, not that the shows are gone. Drop that artist from
    # the write set so its tab is left untouched and the read-back below keeps its last-good data.
    regressed = _detect_regressions(prev_counts, fresh_counts, artist_names)
    for artist in regressed:
        log.error("Regression guard: %s dropped %d -> %d shows this run — keeping last-good "
                  "data (a source likely failed). NOT publishing the drop.",
                  artist, prev_counts.get(artist, 0), fresh_counts.get(artist, 0))
    if regressed:
        dropped = set(regressed)
        all_shows = [s for s in all_shows if s.artist not in dropped]

    skipped = failed_artists + regressed
    all_shows.sort(key=lambda s: (s.date, s.artist))

    # Phase 2: ONE batch Claude call — venue-direct ticket URLs for all artists
    enrich_ticket_urls_all(all_shows)

    # Phase 2b: fill any still-missing start times from the ticket page content (no Claude)
    fill_start_times_from_pages(all_shows)

    log.info(
        "Total Claude API calls: %d / %d  |  Est. cost: $%.4f / $%.2f cap",
        claude_state._claude_call_count, CLAUDE_CALL_LIMIT,
        claude_state._estimated_cost_usd, claude_state.COST_CAP_USD,
    )

    write_json(all_shows)
    write_google_sheets(all_shows)  # per-tab: a skipped/regressed artist's tab is left intact

    if skipped:
        # write_website() REPLACES the whole front-end and write_google_doc() rebuilds it, so
        # pushing the partial set would erase the skipped/regressed acts. Read the full set back
        # from the Sheet (their tabs are preserved above) and publish THAT instead — same
        # safeguard the --artist flow uses.
        log.warning("Some artists were skipped/regressed — reading the full set back from the "
                    "Sheet so the Doc/front-end keep their last-good data.")
        try:
            publish_set = read_shows_from_sheets()
        except SheetReadError as exc:
            # Falling back to all_shows here would publish ONLY the artists that ran clean,
            # wiping every skipped/regressed act from the front-end — the exact data loss
            # this read-back exists to prevent. The Sheet is already written per-tab, so
            # leaving the Doc/front-end untouched keeps the last-good published data.
            log.error("Sheet read-back failed (%s) — NOT pushing the Doc/front-end, as the "
                      "partial set would erase the skipped artists. Re-run to publish.", exc)
            return 1
        if not publish_set:
            log.error("Sheet read-back returned no shows — NOT pushing the Doc/front-end.")
            return 1
        publish_set.sort(key=lambda s: (s.date, s.artist))
    else:
        publish_set = all_shows
    write_google_doc(publish_set)
    write_website(publish_set)

    log.info("Done. %d total shows across %d artists (%d skipped: %s).",
             len(all_shows), len(artist_names), len(skipped),
             ", ".join(skipped) if skipped else "none")
    # Non-zero exit if anything went wrong, so the scheduled CI run turns red for a human.
    return 1 if skipped else 0


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


def verify_links_cli(use_local: bool, artist_filter: str, dry_run: bool) -> None:
    """Verify ticket links read from the Sheet and repair the broken ones.

    use_local=True uses the no-AI DuckDuckGo finder; otherwise Claude web search. Reads
    all shows so the front-end push stays complete; an --artist filter narrows only which
    shows are verified. Corrections are propagated to the Sheet + front-end unless dry_run.
    """
    finder = find_event_ticket_urls_via_search if use_local else find_event_ticket_urls
    label = "DuckDuckGo (no AI)" if use_local else "Claude web search"

    try:
        all_shows = read_shows_from_sheets()
    except SheetReadError as exc:
        log.error("Sheet read failed (%s) — aborting link verification; a partial set would "
                  "wipe the missing artists from the front-end.", exc)
        return
    if not all_shows:
        log.error("No shows read from sheets — aborting link verification.")
        return

    targets = all_shows
    if artist_filter:
        needle = artist_filter.lower()
        targets = [s for s in all_shows if needle in s.artist.lower()]
        if not targets:
            log.error("No shows match artist filter %r — aborting.", artist_filter)
            return
        log.info("Artist filter %r matched %d of %d shows.", artist_filter, len(targets), len(all_shows))

    log.info("=== Verify ticket links via %s%s ===", label, " [dry-run]" if dry_run else "")
    result = verify_fix_and_classify(targets, finder=finder)
    corrected, good = result["corrected"], result["good"]
    for s in corrected:
        log.info("  + %s | %s, %s -> %s", s.date, s.venue, s.city, s.ticket_url)

    # Event posts: overwrite the corrected (broken) links, and fill any event that has no
    # link/button from a confirmed-good show link — without clobbering existing good links.
    forced_keys = {s.dedup_key() for s in corrected}

    if dry_run:
        if corrected:
            log.info("[dry-run] %d link(s) would be updated in the Sheet/front-end (not written).", len(corrected))
        update_event_links(good, dry_run=True, forced_keys=forced_keys)
        return

    if corrected:
        update_sheet_ticket_urls(corrected)
        write_website(all_shows)
        log.info("Updated %d link(s) in the Sheet and front-end.", len(corrected))
    update_event_links(good, dry_run=False, forced_keys=forced_keys)


def audit_names_cli(artist_filter: str = "", no_web_search: bool = False) -> None:
    """Read-only audit: for each roster artist, list every aggregated show with its source,
    performer name, and a FLAG marker on any that fail the act-name guard. Writes nothing.

    Backs a by-hand audit of the roster after a cross-act contamination incident. By default
    it still runs the (threshold-gated, cost-capped) Claude web search; pass --no-web-search
    to skip those calls entirely and audit only the structured/website sources.
    """
    records = fetch_airtable_priority_artists()
    if not records:
        log.warning("Airtable fetch returned empty — falling back to hardcoded BAND_NAMES")
        records = [{"name": n} for n in BAND_NAMES]
    artists = [r["name"] for r in records]
    if artist_filter:
        artists = [a for a in artists if artist_filter.lower() in a.lower()]
        if not artists:
            log.error("No roster artist matched --artist %r", artist_filter)
            return

    total_flagged = 0
    errored: list[str] = []
    for artist in artists:
        try:
            annotations = audit_act_names(artist, claude=not no_web_search)
        except Exception as exc:
            # One source/artist failing (e.g. an unreachable Google Sheet) must not abort the
            # whole read-only audit — record it and keep going so the rest of the roster prints.
            log.error("Audit error for %s: %s", artist, exc)
            errored.append(artist)
            print(f"\n=== {artist} — ERROR (skipped): {exc} ===")
            continue
        annotations.sort(key=lambda t: (t[0].date, t[0].source))
        flagged = [a for a in annotations if not a[1]]
        total_flagged += len(flagged)
        print(f"\n=== {artist} — {len(annotations)} show(s), {len(flagged)} FLAGGED ===")
        for show, passed, reason in annotations:
            mark = "  " if passed else "FLAG"
            loc = ", ".join(p for p in (show.city, show.region) if p)
            perf = f" [performer: {show.performer}]" if show.performer else ""
            note = f"  <- {reason}" if not passed else ""
            print(f"  {mark} {show.date}  {show.source:<17} {show.venue} ({loc}){perf}{note}")
    print(f"\nAudit complete: {total_flagged} flagged show(s) across {len(artists)} artist(s). "
          "No changes written.")
    if errored:
        print(f"Skipped {len(errored)} artist(s) due to source errors: {', '.join(errored)}")


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
        try:
            shows = read_shows_from_sheets()
        except SheetReadError as exc:
            log.error("Sheet read failed (%s) — aborting blocking email doc update.", exc)
            sys.exit(1)
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
        verify_links = "--verify-links" in sys.argv
        # Residency migration knobs: publish range events live (no draft gap) and trash the
        # individual single events they replace. See CLAUDE.md → Outputs → wordpress_events.
        publish_live = "--publish-live" in sys.argv
        replace_residencies = "--replace-residencies" in sys.argv
        try:
            shows = read_shows_from_sheets()
        except SheetReadError as exc:
            log.error("Sheet read failed (%s) — aborting event publish.", exc)
            sys.exit(1)
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
            corrected = publish_events(
                shows, dry_run=dry_run, limit=limit, one_month=one_month, verify_links=verify_links,
                post_status="publish" if publish_live else "draft",
                replace_residencies=replace_residencies,
            )
            # Propagate any corrected links back to the Sheet and front-end (the corrected
            # Show objects are the same references held in `shows`).
            if corrected and not dry_run:
                update_sheet_ticket_urls(corrected)
                write_website(shows)
                update_event_links(corrected, dry_run=False)
                log.info("Propagated %d corrected link(s) to the Sheet, front-end, and event posts.", len(corrected))
            elif corrected:
                log.info("[dry-run] %d link(s) would be corrected (Sheet/front-end not written).", len(corrected))
    elif "--trash-events" in sys.argv:
        # Surgically trash specific event posts by ID (cleanup the title/date-keyed tools
        # can't target). Accepts comma/space-separated IDs after the flag or via --id.
        dry_run = "--dry-run" in sys.argv
        force = "--force-delete" in sys.argv
        raw = _cli_value("trash-events") + "," + ",".join(_cli_values("id"))
        ids = [int(tok) for tok in raw.replace(",", " ").split() if tok.isdigit()]
        if not ids:
            log.error('--trash-events needs event IDs, e.g.: --trash-events "10951,10530" [--dry-run] [--force-delete]')
        else:
            log.info("%s %d event(s): %s", "Dry-run trash for" if dry_run else "Trashing", len(ids), ids)
            trash_events(ids, dry_run=dry_run, force_delete=force)
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
    elif "--update-descriptions" in sys.argv:
        # Refresh an act's event bios from its current Drive description. --dry-run
        # previews which events would change and writes nothing.
        dry_run = "--dry-run" in sys.argv
        artist = _cli_value("artist")
        if not artist:
            log.error("--update-descriptions requires --artist, e.g.: "
                      "--update-descriptions --artist \"Bohemian Queen\" --dry-run")
        else:
            update_event_descriptions([artist], dry_run=dry_run)
    elif "--update-images" in sys.argv:
        # Swap the featured image on an act's existing events (incl. drafts) to a media-
        # library file. --image is the attachment ID or the media file URL. --dry-run
        # previews each event's old->new thumbnail and writes nothing.
        dry_run = "--dry-run" in sys.argv
        artist = _cli_value("artist")
        image_ref = _cli_value("image") or _cli_value("image-id") or _cli_value("image-url")
        if not artist or not image_ref:
            log.error("--update-images requires --artist and --image, e.g.: "
                      "--update-images --artist \"Tony Danza\" --image 10492 --dry-run "
                      "(--image also accepts the media file URL)")
        else:
            # Resolve the partial --artist to full internal roster name(s) so the plugin's
            # title match hits (e.g. "Tony Danza" -> "Tony Danza: Standards & Stories").
            needle = artist.lower()
            matches = [n for n in BAND_NAMES if needle in n.lower()]
            att_id = resolve_media_attachment_id(image_ref)
            if not matches:
                log.error("No roster artist matched --artist %r", artist)
            elif not att_id:
                log.error("Could not resolve --image %r to an attachment ID.", image_ref)
            else:
                log.info("%s featured image -> attachment %s for: %s",
                         "Dry-run" if dry_run else "Setting", att_id, ", ".join(matches))
                update_event_images({n: att_id for n in matches}, dry_run=dry_run)
    elif "--verify-links-local" in sys.argv:
        # Verify ticket links and repair broken ones WITHOUT AI (DuckDuckGo search).
        verify_links_cli(use_local=True, artist_filter=_cli_value("artist"), dry_run="--dry-run" in sys.argv)
    elif "--verify-links" in sys.argv:
        # Verify ticket links and repair broken ones using Claude web search.
        # (Checked after --publish-events, so `--publish-events --verify-links` still
        # routes to the pre-publish verify, not this standalone mode.)
        verify_links_cli(use_local=False, artist_filter=_cli_value("artist"), dry_run="--dry-run" in sys.argv)
    elif "--audit-events" in sys.argv:
        # Reconcile the Airtable Show Calendar against WP events (read-only report).
        # --all-dates includes past shows (default: upcoming only).
        from audit import audit_events
        audit_events(upcoming_only="--all-dates" not in sys.argv)
    elif "--audit-names" in sys.argv:
        # Read-only: list every artist's aggregated shows, flagging any whose performer
        # name fails the act-name guard. Backs a by-hand roster audit; writes nothing.
        audit_names_cli(artist_filter=_cli_value("artist"), no_web_search="--no-web-search" in sys.argv)
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
            # The Sheet is the source of truth for the front-end push below, so it must
            # succeed. The Doc and blocking-email Doc are secondary outputs (the team works
            # from the front-end), so a failure there must NOT prevent the front-end push.
            for _label, _fn in (("Doc", lambda: write_google_doc(shows, partial=True)),
                                ("blocking-email Doc", lambda: write_blocking_email_doc(shows))):
                try:
                    _fn()
                except Exception as exc:
                    log.error("%s write failed for %s (continuing to front-end push): %s",
                              _label, artist_arg, exc)
            # Push the front-end too, but write_website replaces the whole dataset,
            # so we can't post just this artist or every other act disappears. Read
            # all tabs back from the Sheet (now holding this artist's fresh data plus
            # everyone else's existing data) and post the merged set.
            if not OUTPUT_WEBSITE_URL:
                log.info("OUTPUT_WEBSITE_URL not set — skipping front-end push.")
            else:
                try:
                    all_shows = read_shows_from_sheets()
                except SheetReadError as exc:
                    log.error("Sheet read failed (%s) — skipping front-end push; a partial "
                              "set would wipe the artists that failed to read.", exc)
                    all_shows = []
                if not all_shows:
                    log.warning("Sheet read returned no shows — skipping front-end push to avoid wiping it.")
                else:
                    write_website(all_shows)
    else:
        sys.exit(run())
