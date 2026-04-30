import hashlib
import logging
import os
from datetime import datetime as _dt, date as _date

from config import GOOGLE_SHEETS_ID, _display_name, _is_platform_url
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
    header = [["Date", "Venue", "City", "Region", "Country", "Ticket URL", "Source"]]
    rows = [
        [
            _fmt_date(show.date),
            show.venue,
            show.city,
            show.region,
            show.country,
            show.ticket_url,
            _SOURCE_LABELS.get(show.source, show.source),
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
        try:
            iso = _dt.strptime(date_val, "%m/%d/%y").date().isoformat()
        except ValueError:
            continue
        city = row[2] if len(row) > 2 else ""
        key = hashlib.md5(f"{artist}|{iso}|{venue_val}|{city}".lower().encode()).hexdigest()
        saved[key] = ticket_url
    return saved


def write_google_sheets(shows: list[Show]) -> None:
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
        for show in artist_shows:
            if show.dedup_key() in saved_urls:
                if not show.ticket_url or _is_platform_url(show.ticket_url):
                    show.ticket_url = saved_urls[show.dedup_key()]

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
