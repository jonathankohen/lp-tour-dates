import logging
import os
from datetime import datetime as _dt

from config import GOOGLE_SHEETS_ID, BAND_NAMES, DISPLAY_NAMES, _display_name
from models import Show
from outputs.doc import write_google_doc

log = logging.getLogger(__name__)


def read_shows_from_sheets() -> list[Show]:
    """
    Read all artist tabs from the Google Sheet and return reconstructed Show objects.
    Reads tabs by display name for each artist in BAND_NAMES.
    Skips the header row and any row missing a date or venue.
    No Claude or AI API calls.
    """
    if not GOOGLE_SHEETS_ID:
        log.warning("GOOGLE_SHEETS_ID not set, cannot read from sheets")
        return []
    try:
        from googleapiclient.discovery import build  # type: ignore
        from google.oauth2 import service_account  # type: ignore
    except ImportError:
        log.warning("google-api-python-client not installed, cannot read from sheets")
        return []

    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if not creds_path:
        log.warning("GOOGLE_APPLICATION_CREDENTIALS not set, cannot read from sheets")
        return []

    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds = service_account.Credentials.from_service_account_file(creds_path, scopes=scopes)
    service = build("sheets", "v4", credentials=creds)

    display_to_artist = {v: k for k, v in DISPLAY_NAMES.items()}

    all_shows: list[Show] = []
    for artist in BAND_NAMES:
        tab = _display_name(artist)[:100]
        try:
            result = service.spreadsheets().values().get(
                spreadsheetId=GOOGLE_SHEETS_ID,
                range=f"'{tab}'!A1:G",
            ).execute()
        except Exception as exc:
            log.warning("Could not read tab '%s': %s", tab, exc)
            continue

        rows = result.get("values", [])
        if not rows:
            continue

        for row in rows[1:]:
            date_val = row[0] if len(row) > 0 else ""
            venue_val = row[1] if len(row) > 1 else ""
            if not date_val or not venue_val:
                continue
            try:
                iso_date = _dt.strptime(date_val, "%m/%d/%y").date().isoformat()
            except ValueError:
                log.warning("Skipping unrecognised date '%s' in tab '%s'", date_val, tab)
                continue

            full_artist = display_to_artist.get(tab, artist)
            all_shows.append(Show(
                artist=full_artist,
                date=iso_date,
                venue=venue_val,
                city=row[2] if len(row) > 2 else "",
                region=row[3] if len(row) > 3 else "",
                country=row[4] if len(row) > 4 else "",
                ticket_url=row[5] if len(row) > 5 else "",
                source=row[6] if len(row) > 6 else "sheet",
            ))

        log.info("Read %d shows for %s", sum(1 for s in all_shows if s.artist == display_to_artist.get(tab, artist)), tab)

    log.info("Total: %d shows read from Google Sheets", len(all_shows))
    return all_shows


def build_doc_from_sheets() -> None:
    """Read the current Google Sheet and write the Google Doc from it. No AI API calls."""
    shows = read_shows_from_sheets()
    if not shows:
        log.error("No shows read from sheets — aborting doc build.")
        return
    shows.sort(key=lambda s: (s.date, s.artist))
    write_google_doc(shows)
    log.info("Doc build complete.")
