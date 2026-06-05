import logging
import os
import time
from datetime import datetime as _dt

from config import GOOGLE_SHEETS_ID, BAND_NAMES, _display_name
from models import Show
from outputs.doc import write_google_doc

log = logging.getLogger(__name__)


def _execute_with_retry(request, attempts: int = 3, base_delay: float = 1.0):
    """
    Execute a googleapiclient request, retrying transient errors (notably the
    Sheets API's occasional HTTP 500 "Internal error encountered"). Re-raises the
    last error if all attempts fail.
    """
    for i in range(attempts):
        try:
            return request.execute()
        except Exception as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            transient = status in (429, 500, 502, 503, 504) or status is None
            if not transient or i == attempts - 1:
                raise
            delay = base_delay * (2 ** i)
            log.warning("Sheets request failed (%s); retry %d/%d in %.0fs", exc, i + 1, attempts - 1, delay)
            time.sleep(delay)


def _norm_tab(s: str) -> str:
    """Loose key for matching tab titles to artist names: lowercase, alnum only."""
    return "".join(c for c in s.lower() if c.isalnum())


def _match_tabs_to_artists(titles: list[str]) -> dict[str, str]:
    """
    Map each BAND_NAMES artist to the actual sheet tab title holding its shows.

    Tab names have drifted from _display_name() over time (trailing spaces,
    dropped prefixes, full-name tabs), so we match against the real titles
    instead of reconstructing them. Two passes:
      1. exact normalized match on the display name or the full artist name,
      2. containment — a tab whose normalized name is a shortened form of the
         artist (e.g. 'Concert of Kings' ⊂ 'Elvis: The Concert of Kings').
    Each tab is claimed at most once. Artists with no tab (no shows in the
    sheet) are simply absent from the result.
    """
    norm_to_title: dict[str, str] = {}
    for t in titles:
        norm_to_title.setdefault(_norm_tab(t), t)

    result: dict[str, str] = {}
    used: set[str] = set()

    # Pass 1: exact normalized match on display or full name.
    for artist in BAND_NAMES:
        for cand in (_display_name(artist), artist):
            title = norm_to_title.get(_norm_tab(cand))
            if title and title not in used:
                result[artist] = title
                used.add(title)
                break

    # Pass 2: containment for shortened/prefix-dropped tab names.
    for artist in BAND_NAMES:
        if artist in result:
            continue
        afull = _norm_tab(artist)
        best = None
        for t in titles:
            if t in used:
                continue
            nt = _norm_tab(t)
            if len(nt) >= 6 and (nt in afull or afull in nt):
                if best is None or len(nt) > len(_norm_tab(best)):
                    best = t
        if best:
            result[artist] = best
            used.add(best)

    return result


def read_shows_from_sheets() -> list[Show]:
    """
    Read all artist tabs from the Google Sheet and return reconstructed Show objects.
    Discovers the actual tab titles and matches them to BAND_NAMES artists, so it
    tolerates tab names that drift from _display_name(). Skips the header row and
    any row missing a date or venue. No Claude or AI API calls.
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

    # Discover real tab titles and match them to artists.
    try:
        meta = _execute_with_retry(service.spreadsheets().get(spreadsheetId=GOOGLE_SHEETS_ID))
        titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    except Exception as exc:
        log.error("Could not read spreadsheet metadata: %s", exc)
        return []

    tab_for_artist = _match_tabs_to_artists(titles)

    all_shows: list[Show] = []
    for artist in BAND_NAMES:
        tab = tab_for_artist.get(artist)
        if not tab:
            log.debug("No sheet tab found for %s — skipping", artist)
            continue

        safe = tab.replace("'", "''")  # escape single quotes for A1 notation
        try:
            result = _execute_with_retry(
                service.spreadsheets().values().get(
                    spreadsheetId=GOOGLE_SHEETS_ID,
                    range=f"'{safe}'!A1:H",
                )
            )
        except Exception as exc:
            log.warning("Could not read tab '%s': %s", tab, exc)
            continue

        rows = result.get("values", [])
        if not rows:
            continue

        count = 0
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

            all_shows.append(Show(
                artist=artist,
                date=iso_date,
                venue=venue_val,
                city=row[2] if len(row) > 2 else "",
                region=row[3] if len(row) > 3 else "",
                country=row[4] if len(row) > 4 else "",
                ticket_url=row[5] if len(row) > 5 else "",
                source=row[6] if len(row) > 6 else "sheet",
                start_time=row[7] if len(row) > 7 else "",
            ))
            count += 1

        log.info("Read %d shows for %s (tab '%s')", count, _display_name(artist), tab)

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
