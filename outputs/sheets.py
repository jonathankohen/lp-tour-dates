import hashlib
import logging
import os
from datetime import datetime as _dt, date as _date

from config import (
    GOOGLE_SHEETS_ID, _display_name, _is_platform_url, _fmt_time_12h, _parse_time_to_24h,
    _is_bare_homepage, _is_non_ticket_url,
)
from models import Show

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


def _get_or_create_tab(
    service, spreadsheet_id: str, title: str, old_title: str | None = None
) -> None:
    """Ensure a tab with the given title exists; rename old_title→title or create."""
    title = title[:100]
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    sheets = meta.get("sheets", [])
    existing = {s["properties"]["title"]: s["properties"]["sheetId"] for s in sheets}
    if title in existing:
        return
    if old_title and old_title in existing:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"updateSheetProperties": {
                "properties": {"sheetId": existing[old_title], "title": title},
                "fields": "title",
            }}]},
        ).execute()
        log.info("Renamed sheet tab '%s' → '%s'", old_title, title)
        return
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
    ).execute()
    log.info("Created sheet tab: %s", title)


def _read_tab_ticket_urls(service, spreadsheet_id: str, tab: str, artist: str) -> dict[str, str]:
    """
    Read existing sheet tab and return {dedup_key -> ticket_url} for rows with
    venue-direct (non-platform) URLs. Used to preserve good URLs across runs.
    """
    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"'{tab}'!A1:G",
        ).execute()
    except Exception:
        return {}
    saved: dict[str, str] = {}
    for row in result.get("values", [])[1:]:
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


def _read_tab_start_times(service, spreadsheet_id: str, tab: str, artist: str) -> dict[str, str]:
    """
    Read existing sheet tab and return {dedup_key -> start_time} for rows that have
    a Start Time (column H). Used to preserve manually-entered times across runs.
    """
    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"'{tab}'!A1:H",
        ).execute()
    except Exception:
        return {}
    saved: dict[str, str] = {}
    for row in result.get("values", [])[1:]:
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

    for artist, artist_shows in by_artist.items():
        artist_shows.sort(key=lambda s: s.date)
        tab = _display_name(artist)[:100]
        old_tab = artist[:100]
        _get_or_create_tab(service, GOOGLE_SHEETS_ID, tab, old_title=old_tab if old_tab != tab else None)

        saved_urls = _read_tab_ticket_urls(service, GOOGLE_SHEETS_ID, tab, artist)
        saved_times = _read_tab_start_times(service, GOOGLE_SHEETS_ID, tab, artist)
        for show in artist_shows:
            if show.dedup_key() in saved_urls:
                if not show.ticket_url or _is_platform_url(show.ticket_url):
                    show.ticket_url = saved_urls[show.dedup_key()]
            # Preserve a manually-entered time when no source supplied one this run.
            if not show.start_time and show.dedup_key() in saved_times:
                show.start_time = saved_times[show.dedup_key()]

        rows = build_sheet_rows(artist_shows)
        service.spreadsheets().values().update(
            spreadsheetId=GOOGLE_SHEETS_ID,
            range=f"'{tab}'!A1",
            valueInputOption="RAW",
            body={"values": rows},
        ).execute()
        last_row = len(rows) + 1
        service.spreadsheets().values().clear(
            spreadsheetId=GOOGLE_SHEETS_ID,
            range=f"'{tab}'!A{last_row}:Z",
        ).execute()
        log.info("Updated tab '%s' with %d rows", tab, len(rows))

    if not reorder:
        return

    # Reorder tabs: artist tabs alphabetical first, any other tabs after. Assign an
    # explicit index to EVERY sheet (not just the artist subset) so the batch move is
    # deterministic — Google Sheets misorders a partial reorder when other tabs are
    # interspersed.
    artist_tabs = {_display_name(a)[:100] for a in by_artist}
    meta = service.spreadsheets().get(spreadsheetId=GOOGLE_SHEETS_ID).execute()
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
    meta = service.spreadsheets().get(spreadsheetId=GOOGLE_SHEETS_ID).execute()
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    tab_for_artist = _match_tabs_to_artists(titles)

    by_artist: dict[str, list[Show]] = {}
    for s in shows:
        by_artist.setdefault(s.artist, []).append(s)

    data: list[dict] = []
    for artist, group in by_artist.items():
        tab = tab_for_artist.get(artist)
        if not tab:
            log.warning("update_sheet_ticket_urls: no tab for %s — skipping %d show(s)", artist, len(group))
            continue
        safe = tab.replace("'", "''")
        try:
            rows = service.spreadsheets().values().get(
                spreadsheetId=GOOGLE_SHEETS_ID, range=f"'{safe}'!A1:H",
            ).execute().get("values", [])
        except Exception as exc:
            log.warning("update_sheet_ticket_urls: could not read tab '%s': %s", tab, exc)
            continue
        want = {(_fmt_date(s.date), s.venue): s.ticket_url for s in group}
        for ri, row in enumerate(rows[1:], start=2):
            key = (row[0] if row else "", row[1] if len(row) > 1 else "")
            if key in want:
                data.append({"range": f"'{safe}'!F{ri}", "values": [[want[key]]]})

    if data:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=GOOGLE_SHEETS_ID,
            body={"valueInputOption": "RAW", "data": data},
        ).execute()
        log.info("Updated %d ticket-URL cell(s) in the sheet.", len(data))
    else:
        log.info("update_sheet_ticket_urls: no matching rows for %d show(s).", len(shows))
