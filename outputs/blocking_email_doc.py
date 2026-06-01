import logging
import os
import re
from datetime import date as _date

from config import BLOCKING_DOC_ID, _display_name
from models import Show
from outputs.doc import EMAIL_ZONES, _assemble_doc_sections, _build_style_requests

log = logging.getLogger(__name__)

# Maps our artist names to the title of the existing parent tab in the blocking doc.
# Artists not in this dict will get a new parent tab created.
BLOCKING_DOC_PARENT_TAB_TITLES: dict[str, str] = {
    "Back 2 Mac: A Tribute to Fleetwood Mac": "BACK 2 MAC Dates",
    "Arrival From Sweden: The Music of ABBA": "AFS NEW 2025-26",
    "Bohemian Queen": "Bohemian Queen Dates",
    "The Dolly Show": "The Dolly Show Blocking 2026",
    "The Rocket Man Show": "TRMS Dates",
    "Free Fallin: The Tom Petty Concert Experience": "Free Fallin 2025 Dates",
    "Tony Danza: Standards & Stories": "Tony Danza 2025 Dates",
    "Vitaly: An Evening of Wonders!": "Vitaly 2025 Dates",
    "Legends of Classic Rock": "LOCR 2025 Dates",
}

ARTIST_ACRONYMS: dict[str, str] = {
    "Arrival From Sweden: The Music of ABBA": "AFS",
    "The Dolly Show": "TDS",
    "Kyle Martin's Piano Man": "PM",
    "The Rocket Man Show": "TRMS",
    "A1A: The Original Jimmy Buffett Tribute": "A1A",
    "Bohemian Queen": "BQ",
    "Elvis: The Concert of Kings": "ECOK",
    "Free Fallin: The Tom Petty Concert Experience": "FF",
    "Kiss The Sky: A Jimi Hendrix Tribute": "KTS",
    "Legends of Classic Rock": "LOCR",
    "Monkee Men": "MM",
    "Vitaly: An Evening of Wonders!": "V",
    "Back 2 Mac: A Tribute to Fleetwood Mac": "B2M",
    "Tony Danza: Standards & Stories": "TD",
}

_IGNORE_ACRONYMS = {"AMNC", "AMCC"}


def _subtab_title(acronym: str, suffix: str) -> str:
    today = _date.today().isoformat()
    return f"{today} {acronym} {suffix}"


def _build_routes_text(shows: list[Show]) -> str:
    lines = []
    for show in sorted(shows, key=lambda s: s.date):
        d = _date.fromisoformat(show.date)
        date_str = d.strftime("%A, %B %-d")
        loc_parts = [p for p in [show.city, show.region] if p]
        location = ", ".join(loc_parts) if loc_parts else ""
        venue_str = show.venue or ""
        if venue_str and location:
            line = f"{date_str} - {venue_str}, {location}"
        elif venue_str:
            line = f"{date_str} - {venue_str}"
        elif location:
            line = f"{date_str} - {location}"
        else:
            line = date_str
        lines.append(line)
    return "\n".join(lines)


def _collect_tabs(tabs: list) -> dict[str, dict]:
    """Flatten nested tab tree into {tab_id: {title, parent_id, child_ids}} dict."""
    result: dict[str, dict] = {}

    def _walk(tab_list: list, parent_id: str | None) -> None:
        for tab in tab_list:
            tid = tab["tabProperties"]["tabId"]
            result[tid] = {
                "title": tab["tabProperties"].get("title", ""),
                "parent_id": parent_id,
                "child_ids": [c["tabProperties"]["tabId"] for c in tab.get("childTabs", [])],
            }
            _walk(tab.get("childTabs", []), parent_id=tid)

    _walk(tabs, parent_id=None)
    return result


def _find_subtab(tabs_by_id: dict, parent_id: str, acronym: str, suffix: str) -> str | None:
    """Return the tab_id of a child of parent_id whose title matches `* (ACRONYM) suffix`."""
    parent = tabs_by_id.get(parent_id)
    if not parent:
        return None
    pattern = re.compile(
        rf"\b{re.escape(acronym)}\s+{re.escape(suffix)}$", re.IGNORECASE
    )
    for child_id in parent["child_ids"]:
        child = tabs_by_id.get(child_id)
        if child and pattern.search(child["title"]):
            return child_id
    return None


def _clear_and_write_tab(service, doc_id: str, tab_id: str, text: str) -> None:
    """Replace all content in tab_id with text."""
    from outputs.doc import _docs_batchupdate

    doc = service.documents().get(documentId=doc_id, includeTabsContent=True).execute()

    def _find_tab(tab_list: list):
        for t in tab_list:
            if t["tabProperties"]["tabId"] == tab_id:
                return t
            found = _find_tab(t.get("childTabs", []))
            if found:
                return found
        return None

    tab = _find_tab(doc.get("tabs", []))
    if not tab:
        log.warning("Tab %s not found when trying to clear content", tab_id)
        return

    body_content = tab.get("documentTab", {}).get("body", {}).get("content", [])
    body_end = body_content[-1]["endIndex"] if body_content else 1

    reqs = []
    if body_end > 1:
        reqs.append({
            "deleteContentRange": {
                "range": {"startIndex": 1, "endIndex": body_end - 1, "tabId": tab_id}
            }
        })
    if text:
        reqs.append({
            "insertText": {"location": {"index": 1, "tabId": tab_id}, "text": text}
        })
    if reqs:
        _docs_batchupdate(service, doc_id, reqs)


def _write_subtab(service, doc_id: str, parent_id: str, title: str, text: str,
                  tabs_by_id: dict, acronym: str, suffix: str) -> str:
    """Create or update a subtab; return its tab_id."""
    from outputs.doc import _docs_batchupdate

    existing_id = _find_subtab(tabs_by_id, parent_id, acronym, suffix)
    if existing_id:
        log.info("Blocking doc: updating '%s'", tabs_by_id[existing_id]["title"])
        _clear_and_write_tab(service, doc_id, existing_id, text)
        return existing_id

    log.info("Blocking doc: creating subtab '%s'", title)
    resp = _docs_batchupdate(service, doc_id, [{"addDocumentTab": {
        "tabProperties": {"title": title[:50], "parentTabId": parent_id},
    }}])
    new_id = resp["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]
    _docs_batchupdate(service, doc_id, [{"insertText": {
        "location": {"index": 1, "tabId": new_id},
        "text": text,
    }}])
    return new_id


def _write_zone_subtab(service, doc_id: str, parent_id: str, acronym: str,
                       zone_name: str, zone_shows: list[Show], zone_num: int,
                       tabs_by_id: dict) -> None:
    """Write a single email zone subtab with month-organized content."""
    from outputs.doc import _docs_batchupdate

    zone_states_present = sorted({s.region for s in zone_shows})
    zone_by_month: dict[str, list[Show]] = {}
    for show in zone_shows:
        zone_by_month.setdefault(show.date[:7], []).append(show)

    zone_header = (
        f"Email Zone {zone_num}: {zone_name}\n"
        f"States: {', '.join(zone_states_present)}\n\n"
    )
    zone_body, z_heading_ranges, z_open_ranges = _assemble_doc_sections(zone_by_month)
    full_zone_text = zone_header + zone_body
    header_len = len(zone_header)

    title = _subtab_title(acronym, zone_name)
    existing_id = _find_subtab(tabs_by_id, parent_id, acronym, zone_name)
    if existing_id:
        log.info("Blocking doc: updating zone tab '%s'", tabs_by_id[existing_id]["title"])
        _clear_and_write_tab(service, doc_id, existing_id, full_zone_text)
        tab_id = existing_id
        # Re-apply styles after clear+write
        style_reqs = _build_style_requests(
            tab_id, 1,
            heading_ranges=[(header_len + s, header_len + e) for s, e in z_heading_ranges],
            open_ranges=[(header_len + s, header_len + e) for s, e in z_open_ranges],
            extra_headings=[(0, len(f"Email Zone {zone_num}: {zone_name}"), "HEADING_1")],
        )
        if style_reqs:
            _docs_batchupdate(service, doc_id, style_reqs)
    else:
        log.info("Blocking doc: creating zone subtab '%s'", title)
        resp = _docs_batchupdate(service, doc_id, [{"addDocumentTab": {
            "tabProperties": {"title": title[:50], "parentTabId": parent_id},
        }}])
        tab_id = resp["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]
        style_reqs = _build_style_requests(
            tab_id, 1,
            heading_ranges=[(header_len + s, header_len + e) for s, e in z_heading_ranges],
            open_ranges=[(header_len + s, header_len + e) for s, e in z_open_ranges],
            extra_headings=[(0, len(f"Email Zone {zone_num}: {zone_name}"), "HEADING_1")],
        )
        _docs_batchupdate(service, doc_id, [
            {"insertText": {"location": {"index": 1, "tabId": tab_id}, "text": full_zone_text}},
            *style_reqs,
        ])

    log.info("Blocking doc: wrote zone '%s' (%d shows)", zone_name, len(zone_shows))


def write_blocking_email_doc(shows: list[Show]) -> None:
    """
    Update the blocking email doc (BLOCKING_TEST_ID env var) with tour dates for each of our artists.
    For each artist:
      - Finds or creates a parent tab
      - Creates/updates a flat Routes subtab (chronological)
      - Creates/updates email zone subtabs (matching our main Google Doc zone structure)
    """
    if not BLOCKING_DOC_ID:
        log.warning("BLOCKING_TEST_ID not set, skipping blocking email doc")
        return

    try:
        from googleapiclient.discovery import build
        from google.oauth2 import service_account
    except ImportError:
        log.warning("google-api-python-client not installed, skipping blocking email doc")
        return

    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if not creds_path:
        log.warning("GOOGLE_APPLICATION_CREDENTIALS not set, skipping blocking email doc")
        return

    scopes = [
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = service_account.Credentials.from_service_account_file(creds_path, scopes=scopes)
    service = build("docs", "v1", credentials=creds)

    from outputs.doc import _docs_batchupdate

    # includeTabsContent=True is required to get the tabs[] field in the response.
    # With False (the default), the API returns the legacy single-tab body, not tabs[].
    doc = service.documents().get(
        documentId=BLOCKING_DOC_ID, includeTabsContent=True
    ).execute()
    tabs_by_id = _collect_tabs(doc.get("tabs", []))

    title_to_id: dict[str, str] = {
        info["title"]: tid
        for tid, info in tabs_by_id.items()
        if info["parent_id"] is None
    }
    log.info("Blocking doc: found %d top-level tabs", len(title_to_id))

    by_artist: dict[str, list[Show]] = {}
    for show in shows:
        by_artist.setdefault(show.artist, []).append(show)

    for artist, artist_shows in sorted(by_artist.items()):
        acronym = ARTIST_ACRONYMS.get(artist)
        if not acronym:
            log.info("Blocking doc: no acronym configured for '%s', skipping", artist)
            continue
        if acronym in _IGNORE_ACRONYMS:
            continue
        if not artist_shows:
            continue

        routes_text = _build_routes_text(artist_shows)
        routes_title = _subtab_title(acronym, "Routes")

        target_parent_title = BLOCKING_DOC_PARENT_TAB_TITLES.get(artist)
        parent_id = title_to_id.get(target_parent_title) if target_parent_title else None

        if not parent_id:
            dname = _display_name(artist)
            log.info("Blocking doc: creating new parent tab '%s' for %s", dname, artist)
            resp = _docs_batchupdate(service, BLOCKING_DOC_ID, [{"addDocumentTab": {
                "tabProperties": {"title": dname[:50]},
            }}])
            parent_id = resp["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]
            # Add the new parent to our in-memory maps so zone subtabs can find it
            tabs_by_id[parent_id] = {
                "title": dname[:50],
                "parent_id": None,
                "child_ids": [],
            }
            title_to_id[dname[:50]] = parent_id

        # Routes subtab (flat chronological list)
        _write_subtab(
            service, BLOCKING_DOC_ID, parent_id, routes_title,
            routes_text, tabs_by_id, acronym, "Routes"
        )

        # Email zone subtabs (same zones as main Google Doc)
        zone_num = 0
        for zone_name, zone_states in EMAIL_ZONES:
            zone_shows = [s for s in artist_shows if s.region in zone_states]
            if not zone_shows:
                continue
            zone_states_present = sorted({s.region for s in zone_shows})
            if len(zone_states_present) < 2:
                continue
            zone_num += 1
            _write_zone_subtab(
                service, BLOCKING_DOC_ID, parent_id, acronym,
                zone_name, zone_shows, zone_num, tabs_by_id
            )

        log.info("Blocking doc: processed %d shows for %s", len(artist_shows), artist)

    log.info("Blocking email doc update complete.")
