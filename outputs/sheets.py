import hashlib
import logging
import os
from datetime import datetime as _dt, date as _date

from config import (
    GOOGLE_SHEETS_ID, _display_name, _is_platform_url, _fmt_time_12h, _parse_time_to_24h,
    _is_bare_homepage, _is_non_ticket_url,
)
from models import Show
from utils import _execute_with_retry

log = logging.getLogger(__name__)

_SOURCE_LABELS = {
    "bandsintown": "Bandsintown",
    "seatgeek": "SeatGeek",
    "artist_website": "Artist Website",
    "ticketmaster": "Ticketmaster",
    "claude_web_search": "Web Search",
}


def _fmt_date(iso_date: str) -> str:
    """Convert ISO date (2026-04-05) to MM/DD/YY (04/05/26)."""
    return _date.fromisoformat(iso_date).strftime("%m/%d/%y")


def build_sheet_rows(shows: list[Show]) -> list[list[str]]:
    """Build spreadsheet rows — booked shows only, no Open/ellipsis rows."""
    header = [["Date", "Venue", "City", "Region", "Country", "Ticket URL", "Source", "Start Time"]]
    rows = [
        [
            _fmt_date(show.date),
            show.venue,
            show.city,
            show.region,
            show.country,
            show.ticket_url,
            _SOURCE_LABELS.get(show.source, show.source),
            _fmt_time_12h(show.start_time),
        ]
        for show in shows
    ]
    return header + rows


def _fetch_tab_ids(service, spreadsheet_id: str) -> dict[str, int]:
    """{tab title -> sheetId} for the spreadsheet."""
    meta = _execute_with_retry(service.spreadsheets().get(spreadsheetId=spreadsheet_id))
    return {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta.get("sheets", [])}


def _get_or_create_tab(
    service, spreadsheet_id: str, title: str, existing: dict[str, int],
    old_title: str | None = None
) -> None:
    """Ensure a tab with the given title exists; rename old_title→title or create.

    `existing` is the caller's {title -> sheetId} map, read ONCE and updated in place here.
    Re-reading the spreadsheet metadata per artist cost one request per tab against the
    Sheets 60-reads-per-minute quota, which is what tipped the write phase over the limit.
    """
    title = title[:100]
    if title in existing:
        return
    if old_title and old_title in existing:
        _execute_with_retry(service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"updateSheetProperties": {
                "properties": {"sheetId": existing[old_title], "title": title},
                "fields": "title",
            }}]},
        ))
        existing[title] = existing.pop(old_title)
        log.info("Renamed sheet tab '%s' → '%s'", old_title, title)
        return
    resp = _execute_with_retry(service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
    ))
    try:
        existing[title] = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
    except (KeyError, IndexError, TypeError):
        existing[title] = -1  # id unknown; presence is what the callers check
    log.info("Created sheet tab: %s", title)


def _read_tabs_rows(service, spreadsheet_id: str, tabs: list[str]) -> dict[str, list[list[str]]]:
    """
    Read every tab's existing rows (header included) in ONE batchGet, for the URL/time
    preservation passes below. Both passes used to issue their own read of each tab —
    2 requests per artist against the Sheets 60-reads-per-minute quota.

    Failures are NOT swallowed: returning {} from the preservation readers on a 429
    silently discards manually-entered ticket URLs and start times, which the subsequent
    full-tab write then erases from the Sheet.
    """
    if not tabs:
        return {}
    ranges = []
    for tab in tabs:
        safe = tab.replace("'", "''")  # escape single quotes for A1 notation
        ranges.append(f"'{safe}'!A1:H")
    batch = _execute_with_retry(
        service.spreadsheets().values().batchGet(spreadsheetId=spreadsheet_id, ranges=ranges)
    )
    value_ranges = batch.get("valueRanges", [])
    if len(value_ranges) != len(tabs):
        raise RuntimeError(f"read {len(value_ranges)} of {len(tabs)} tabs")
    return {tab: vr.get("values", []) for tab, vr in zip(tabs, value_ranges)}


def _ticket_urls_from_rows(rows: list[list[str]], artist: str) -> dict[str, str]:
    """
    Return {dedup_key -> ticket_url} for rows with venue-direct (non-platform) URLs.
    Used to preserve good URLs across runs.
    """
    saved: dict[str, str] = {}
    for row in rows[1:]:
        date_val = row[0] if row else ""
        venue_val = row[1] if len(row) > 1 else ""
        ticket_url = row[5] if len(row) > 5 else ""
        if not date_val or not venue_val or not ticket_url:
            continue
        if _is_platform_url(ticket_url):
            continue
        # Don't preserve links that are never a real ticket page — a bare venue homepage or a
        # non-ticket section (rooms/dining). The old enrichment wrote these into the sheet, and
        # preserving them would resurrect exactly the low-quality links we now avoid over a fresh
        # Bandsintown event page. (A genuine human-curated venue link on an odd domain is left
        # untouched — we only drop the unambiguous non-links.)
        if _is_bare_homepage(ticket_url) or _is_non_ticket_url(ticket_url):
            continue
        try:
            iso = _dt.strptime(date_val, "%m/%d/%y").date().isoformat()
        except ValueError:
            continue
        city = row[2] if len(row) > 2 else ""
        key = hashlib.md5(f"{artist}|{iso}|{venue_val}|{city}".lower().encode()).hexdigest()
        saved[key] = ticket_url
    return saved


def _start_times_from_rows(rows: list[list[str]], artist: str) -> dict[str, str]:
    """
    Return {dedup_key -> start_time} for rows that have a Start Time (column H).
    Used to preserve manually-entered times across runs.
    """
    saved: dict[str, str] = {}
    for row in rows[1:]:
        date_val = row[0] if row else ""
        venue_val = row[1] if len(row) > 1 else ""
        # Normalize back to canonical 24-hour, accepting either the 12-hour value we
        # now write or a time a human typed into the cell in any common format.
        start_time = _parse_time_to_24h(row[7] if len(row) > 7 else "")
        if not date_val or not venue_val or not start_time:
            continue
        try:
            iso = _dt.strptime(date_val, "%m/%d/%y").date().isoformat()
        except ValueError:
            continue
        city = row[2] if len(row) > 2 else ""
        key = hashlib.md5(f"{artist}|{iso}|{venue_val}|{city}".lower().encode()).hexdigest()
        saved[key] = start_time
    return saved


def _desired_sheet_order(all_sheets: list[dict], artist_tabs: set[str]) -> list[tuple[int, int]]:
    """Compute (sheetId, target_index) for every sheet.

    Artist tabs come first in case-insensitive alphabetical order; all other tabs trail
    in their current relative order. Assigning an explicit index to every sheet (rather
    than only the artist subset) is what makes the batch reorder deterministic.
    """
    sheets = sorted(all_sheets, key=lambda s: s["properties"].get("index", 0))
    artist = [s for s in sheets if s["properties"]["title"] in artist_tabs]
    other = [s for s in sheets if s["properties"]["title"] not in artist_tabs]
    artist.sort(key=lambda s: s["properties"]["title"].lower())
    ordered = artist + other
    return [(s["properties"]["sheetId"], i) for i, s in enumerate(ordered)]


def write_google_sheets(shows: list[Show], reorder: bool = True) -> None:
    """
    Push shows to a Google Sheet, one tab per artist. Requires:
      - google-auth, google-api-python-client packages
      - GOOGLE_APPLICATION_CREDENTIALS env var pointing to a service account JSON
      - The sheet shared with the service account email
    """
    if not GOOGLE_SHEETS_ID:
        return
    try:
        from googleapiclient.discovery import build  # type: ignore
        from google.oauth2 import service_account  # type: ignore
    except ImportError:
        log.warning("google-api-python-client not installed, skipping Sheets output")
        return

    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if not creds_path:
        log.warning("GOOGLE_APPLICATION_CREDENTIALS not set, skipping Sheets output")
        return

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = service_account.Credentials.from_service_account_file(
        creds_path, scopes=scopes
    )
    service = build("sheets", "v4", credentials=creds)

    by_artist: dict[str, list[Show]] = {}
    for show in shows:
        by_artist.setdefault(show.artist, []).append(show)

    # Read the tab list once and keep it current as tabs are created/renamed below, rather
    # than re-reading the spreadsheet metadata for every artist.
    existing_tabs = _fetch_tab_ids(service, GOOGLE_SHEETS_ID)

    # Pass 1: make sure every tab exists, so pass 2 can fetch them all in one batchGet.
    tabs: dict[str, str] = {}  # artist -> tab title
    for artist in by_artist:
        tab = _display_name(artist)[:100]
        old_tab = artist[:100]
        _get_or_create_tab(service, GOOGLE_SHEETS_ID, tab, existing_tabs,
                           old_title=old_tab if old_tab != tab else None)
        tabs[artist] = tab

    # Pass 2: ONE read of all tabs, serving both preservation passes (ticket URLs and start
    # times). If it fails we must NOT write — each write overwrites a whole tab, and writing
    # without the preserved values would erase manually-entered URLs / start times.
    try:
        existing_rows_by_artist = _read_tabs_rows(service, GOOGLE_SHEETS_ID,
                                                 [tabs[a] for a in by_artist])
    except Exception as exc:
        log.error("Could not read the existing sheet (%s) — skipping the Sheet write so "
                  "manually-entered ticket URLs / start times aren't erased.", exc)
        return

    for artist, artist_shows in by_artist.items():
        artist_shows.sort(key=lambda s: s.date)
        tab = tabs[artist]
        existing_rows = existing_rows_by_artist.get(tab, [])
        saved_urls = _ticket_urls_from_rows(existing_rows, artist)
        saved_times = _start_times_from_rows(existing_rows, artist)
        for show in artist_shows:
            if show.dedup_key() in saved_urls:
                if not show.ticket_url or _is_platform_url(show.ticket_url):
                    show.ticket_url = saved_urls[show.dedup_key()]
            # Preserve a manually-entered time when no source supplied one this run.
            if not show.start_time and show.dedup_key() in saved_times:
                show.start_time = saved_times[show.dedup_key()]

        rows = build_sheet_rows(artist_shows)
        _execute_with_retry(service.spreadsheets().values().update(
            spreadsheetId=GOOGLE_SHEETS_ID,
            range=f"'{tab}'!A1",
            valueInputOption="RAW",
            body={"values": rows},
        ))
        last_row = len(rows) + 1
        _execute_with_retry(service.spreadsheets().values().clear(
            spreadsheetId=GOOGLE_SHEETS_ID,
            range=f"'{tab}'!A{last_row}:Z",
        ))
        log.info("Updated tab '%s' with %d rows", tab, len(rows))

    if not reorder:
        return

    # Reorder tabs: artist tabs alphabetical first, any other tabs after. Assign an
    # explicit index to EVERY sheet (not just the artist subset) so the batch move is
    # deterministic — Google Sheets misorders a partial reorder when other tabs are
    # interspersed.
    artist_tabs = {_display_name(a)[:100] for a in by_artist}
    meta = _execute_with_retry(service.spreadsheets().get(spreadsheetId=GOOGLE_SHEETS_ID))
    all_sheets = meta.get("sheets", [])
    targets = _desired_sheet_order(all_sheets, artist_tabs)
    reorder_reqs = [
        {"updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "index": index},
            "fields": "index",
        }}
        # Apply in ascending target order: each sheet is moved to an index at or below
        # its current position, which avoids Google's move-to-higher-index off-by-one.
        for sheet_id, index in sorted(targets, key=lambda t: t[1])
    ]
    if reorder_reqs:
        service.spreadsheets().batchUpdate(
            spreadsheetId=GOOGLE_SHEETS_ID,
            body={"requests": reorder_reqs},
        ).execute()
        log.info("Reordered %d sheet tabs alphabetically", len(reorder_reqs))


def update_sheet_ticket_urls(shows: list[Show]) -> None:
    """Surgically update only the Ticket URL (column F) cell of each given show's row.

    Matches a show to its row by formatted date + venue. Touches nothing else — no
    full-tab clear+write, no reorder. Used to persist corrected links back to the sheet.
    """
    if not shows or not GOOGLE_SHEETS_ID:
        return
    try:
        from googleapiclient.discovery import build  # type: ignore
        from google.oauth2 import service_account  # type: ignore
    except ImportError:
        log.warning("google-api-python-client not installed, cannot update sheet links")
        return
    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if not creds_path:
        return
    from utils import _match_tabs_to_artists

    creds = service_account.Credentials.from_service_account_file(
        creds_path, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    service = build("sheets", "v4", credentials=creds)
    meta = _execute_with_retry(service.spreadsheets().get(spreadsheetId=GOOGLE_SHEETS_ID))
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    tab_for_artist = _match_tabs_to_artists(titles)

    by_artist: dict[str, list[Show]] = {}
    for s in shows:
        by_artist.setdefault(s.artist, []).append(s)

    # One batchGet for every tab, not one read per artist (Sheets caps reads at 60/min/user).
    targets: list[tuple[str, str, list[Show]]] = []  # (artist, escaped tab, shows)
    for artist, group in by_artist.items():
        tab = tab_for_artist.get(artist)
        if not tab:
            log.warning("update_sheet_ticket_urls: no tab for %s — skipping %d show(s)", artist, len(group))
            continue
        targets.append((artist, tab.replace("'", "''"), group))

    if not targets:
        log.info("update_sheet_ticket_urls: no matching tabs for %d show(s).", len(shows))
        return

    try:
        batch = _execute_with_retry(service.spreadsheets().values().batchGet(
            spreadsheetId=GOOGLE_SHEETS_ID,
            ranges=[f"'{safe}'!A1:H" for _, safe, _ in targets],
        ))
    except Exception as exc:
        log.warning("update_sheet_ticket_urls: could not read the sheet: %s", exc)
        return

    data: list[dict] = []
    for (artist, safe, group), vr in zip(targets, batch.get("valueRanges", [])):
        rows = vr.get("values", [])
        want = {(_fmt_date(s.date), s.venue): s.ticket_url for s in group}
        for ri, row in enumerate(rows[1:], start=2):
            key = (row[0] if row else "", row[1] if len(row) > 1 else "")
            if key in want:
                data.append({"range": f"'{safe}'!F{ri}", "values": [[want[key]]]})

    if data:
        _execute_with_retry(service.spreadsheets().values().batchUpdate(
            spreadsheetId=GOOGLE_SHEETS_ID,
            body={"valueInputOption": "RAW", "data": data},
        ))
        log.info("Updated %d ticket-URL cell(s) in the sheet.", len(data))
    else:
        log.info("update_sheet_ticket_urls: no matching rows for %d show(s).", len(shows))
