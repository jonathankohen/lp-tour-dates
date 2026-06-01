import logging
import os
import re
from datetime import date as _date

from models import Show

log = logging.getLogger(__name__)

ARTIST = "Back 2 Mac: A Tribute to Fleetwood Mac"

# Skip notes that are not confirmed shows
_SKIP_PATTERNS = re.compile(
    r"\b(hold|tbd|break|advertise|pass on|check first|target)\b",
    re.IGNORECASE,
)

# Match "City Name ST" — ends with a 2-letter US state abbreviation
_CITY_STATE_RE = re.compile(r"^(.*?)\s+([A-Z]{2})$")

_MONTH_NAMES = {
    "January": 1, "February": 2, "March": 3, "April": 4,
    "May": 5, "June": 6, "July": 7, "August": 8,
    "September": 9, "October": 10, "November": 11, "December": 12,
}


def fetch_back2mac_sheets() -> list[Show]:
    sheet_id = os.environ.get("BACK_2_MAC_SHEETS_ID", "")
    if not sheet_id:
        log.warning("BACK_2_MAC_SHEETS_ID not set, skipping Back 2 Mac sheet source")
        return []

    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if not creds_path:
        log.warning("GOOGLE_APPLICATION_CREDENTIALS not set, skipping Back 2 Mac sheet source")
        return []

    try:
        from googleapiclient.discovery import build
        from google.oauth2 import service_account
    except ImportError:
        log.warning("google-api-python-client not installed, skipping Back 2 Mac sheet source")
        return []

    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds = service_account.Credentials.from_service_account_file(creds_path, scopes=scopes)
    service = build("sheets", "v4", credentials=creds)

    # Find the current-year tab (e.g. " 2026 Back 2 Mac")
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    tabs = meta.get("sheets", [])
    year = _date.today().year
    target_tab = None
    for t in tabs:
        title = t["properties"]["title"]
        if str(year) in title:
            target_tab = title
            break
    if not target_tab:
        log.warning("No %d tab found in Back 2 Mac sheet", year)
        return []

    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"'{target_tab}'!A1:AQ40",
    ).execute()
    rows = result.get("values", [])
    if len(rows) < 5:
        log.warning("Back 2 Mac sheet has too few rows to parse")
        return []

    # Row index 3 (0-based) = row 4: month names at columns 0, 4, 8, ...
    header_row = rows[3]
    months: list[tuple[int, int]] = []  # (col_offset, month_number)
    for col in range(0, len(header_row), 4):
        name = header_row[col].strip() if col < len(header_row) else ""
        if name in _MONTH_NAMES:
            months.append((col, _MONTH_NAMES[name]))

    shows: list[Show] = []
    for row in rows[4:]:
        for col_offset, month_num in months:
            note_col = col_offset + 2
            day_col = col_offset + 1
            if note_col >= len(row):
                continue
            note = row[note_col].strip() if note_col < len(row) else ""
            if not note:
                continue
            if _SKIP_PATTERNS.search(note):
                continue

            m = _CITY_STATE_RE.match(note)
            if not m:
                continue

            city, region = m.group(1).strip(), m.group(2)
            day_str = row[day_col].strip() if day_col < len(row) else ""
            try:
                day = int(day_str)
            except ValueError:
                continue

            try:
                show_date = _date(year, month_num, day).isoformat()
            except ValueError:
                log.warning("Invalid date %d/%d/%d in Back 2 Mac sheet, skipping", year, month_num, day)
                continue

            shows.append(Show(
                artist=ARTIST,
                date=show_date,
                venue="",
                city=city,
                region=region,
                country="US",
                ticket_url="",
                source="back2mac_sheets",
            ))
            log.debug("Back2Mac sheet: %s %s, %s", show_date, city, region)

    log.info("Back2Mac sheet: found %d shows", len(shows))
    return shows
