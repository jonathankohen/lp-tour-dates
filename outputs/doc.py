import logging
import os
import time
import uuid
from datetime import date as _date, timedelta

from config import GOOGLE_DOC_ID, _display_name, _subtab_prefix
from models import Show

log = logging.getLogger(__name__)

# Geographic email zones — only zones with ≥1 show for an artist are written.
EMAIL_ZONES: list[tuple[str, list[str]]] = [
    ("New England",       ["CT", "MA", "ME", "NH", "RI", "VT"]),
    ("Mid-Atlantic",      ["DC", "DE", "MD", "NJ", "NY", "PA"]),
    ("Southeast",         ["AL", "FL", "GA", "MS", "NC", "SC", "TN", "VA", "WV"]),
    ("South Central",     ["AR", "KY", "LA", "MO", "OK", "TX"]),
    ("Great Lakes",       ["IL", "IN", "MI", "OH", "WI"]),
    ("Plains",            ["IA", "KS", "MN", "NE", "ND", "SD"]),
    ("Mountain",          ["CO", "ID", "MT", "NM", "UT", "WY"]),
    ("Southwest",         ["AZ", "CA", "NV"]),
    ("Pacific Northwest", ["OR", "WA"]),
]


def _season_key(date_str: str) -> tuple[str, str]:
    """Return (sort_key, label) for a date string. Keys sort correctly across years."""
    d = _date.fromisoformat(date_str)
    m, y = d.month, d.year
    if m in (3, 4, 5):
        return (f"{y}-0", f"Spring {y}")
    elif m in (6, 7, 8):
        return (f"{y}-1", f"Summer {y}")
    elif m in (9, 10, 11):
        return (f"{y}-2", f"Fall {y}")
    else:  # Dec=12 belongs to current year's Winter; Jan/Feb belong to prior year's Winter
        season_year = y if m == 12 else y - 1
        return (f"{season_year}-3", f"Winter {season_year}")


def _build_doc_month_text(shows: list[Show]) -> tuple[str, list[tuple[int, int]]]:
    """
    Build plain-text content for one month.
    Returns (text, open_ranges) where open_ranges are (start, end) char offsets
    within text marking each OPEN line (for bold formatting).
    Each booked show is surrounded by up to 2 open dates on each side,
    filtered to the same calendar month and deduplicated.
    """
    month_year = _date.fromisoformat(shows[0].date).replace(day=1)
    date_map: dict[_date, Show | None] = {}

    for show in shows:
        d = _date.fromisoformat(show.date)
        date_map[d] = show
        for i in range(1, 3):
            for open_d in (d - timedelta(days=i), d + timedelta(days=i)):
                if open_d.year == month_year.year and open_d.month == month_year.month:
                    if open_d not in date_map:
                        date_map[open_d] = None

    lines: list[str] = []
    open_ranges: list[tuple[int, int]] = []
    pos = 0
    for d in sorted(date_map.keys()):
        show = date_map[d]
        date_str = d.strftime("%A, %B %-d")
        if show is None:
            line = f"{date_str} - OPEN"
            open_ranges.append((pos, pos + len(line)))
        else:
            loc_parts = [p for p in [show.city, show.region] if p]
            location = ", ".join(loc_parts) if loc_parts else ""
            venue_str = show.venue or ""
            if venue_str and location:
                line = f"{date_str} - {venue_str}, {location}"
            elif venue_str:
                line = f"{date_str} - {venue_str}"
            else:
                line = f"{date_str} - {location}"
        lines.append(line)
        pos += len(line) + 1  # +1 for the \n separator

    return "\n".join(lines), open_ranges


def _assemble_doc_sections(
    sections: list[tuple[str, list[Show]]],
) -> tuple[str, list[tuple[int, int]], list[tuple[int, int]]]:
    """
    Assemble content from a list of (label, shows) pairs (already sorted).
    Each section gets one heading; shows are grouped by month internally so
    OPEN date logic stays calendar-month-accurate.
    Returns (text, heading_ranges, open_ranges) as 0-based char offsets within text.
    Caller adds the doc insert index (usually 1) to get actual doc positions.
    """
    parts: list[str] = []
    heading_ranges: list[tuple[int, int]] = []
    open_ranges: list[tuple[int, int]] = []
    pos = 0

    for i, (label, section_shows) in enumerate(sections):
        # Group shows by calendar month so _build_doc_month_text gets same-month input
        by_month: dict[str, list[Show]] = {}
        for show in section_shows:
            by_month.setdefault(show.date[:7], []).append(show)

        # Concatenate all months' text into one section body
        section_text_parts: list[str] = []
        section_open_ranges: list[tuple[int, int]] = []
        body_pos = 0
        for month_key in sorted(by_month):
            month_text, m_open = _build_doc_month_text(by_month[month_key])
            if section_text_parts:
                section_text_parts.append("\n")
                body_pos += 1
            for s, e in m_open:
                section_open_ranges.append((body_pos + s, body_pos + e))
            section_text_parts.append(month_text)
            body_pos += len(month_text)
        section_body = "".join(section_text_parts)

        if i > 0:
            parts.append("\n\n")
            pos += 2

        heading_ranges.append((pos, pos + len(label)))
        parts.append(label + "\n")
        pos += len(label) + 1

        for s, e in section_open_ranges:
            open_ranges.append((pos + s, pos + e))

        parts.append(section_body)
        pos += len(section_body)

    return "".join(parts), heading_ranges, open_ranges


def _build_email_text(shows: list[Show]) -> tuple[str, list[tuple[int, int]]]:
    """
    Build copy-paste email text: booked shows only, no OPEN rows, with month headings.
    Returns (text, heading_ranges) where heading_ranges are char offsets of month header lines.
    Format:
        July 2026

        Friday, July 10, 2026
        Saint Michael, MN

        Saturday, July 11, 2026
        Nashville, TN
    """
    sorted_shows = sorted(shows, key=lambda s: s.date)
    parts: list[str] = []
    heading_ranges: list[tuple[int, int]] = []
    pos = 0
    current_month: str | None = None

    for show in sorted_shows:
        d = _date.fromisoformat(show.date)
        month_key = d.strftime("%Y-%m")
        month_label = d.strftime("%B %Y")

        if month_key != current_month:
            if parts:
                parts.append("\n\n")
                pos += 2
            heading_ranges.append((pos, pos + len(month_label)))
            parts.append(month_label + "\n\n")
            pos += len(month_label) + 2
            current_month = month_key
        else:
            parts.append("\n\n")
            pos += 2

        date_line = d.strftime("%A, %B %-d")
        loc_parts = [p for p in [show.city, show.region] if p]
        loc_line = ", ".join(loc_parts) if loc_parts else show.venue or ""
        entry = f"{date_line}\n{loc_line}"
        parts.append(entry)
        pos += len(entry)

    return "".join(parts), heading_ranges


def _build_style_requests(
    tab_id: str,
    insert_offset: int,
    heading_ranges: list[tuple[int, int]],
    open_ranges: list[tuple[int, int]],
    heading_level: str = "HEADING_2",
    extra_headings: list[tuple[int, int, str]] | None = None,
) -> list[dict]:
    """Build (but do not apply) heading and bold style requests."""
    reqs: list[dict] = []
    for s, e in heading_ranges:
        reqs.append({"updateParagraphStyle": {
            "range": {"startIndex": insert_offset + s, "endIndex": insert_offset + e, "tabId": tab_id},
            "paragraphStyle": {"namedStyleType": heading_level},
            "fields": "namedStyleType",
        }})
    for s, e, level in (extra_headings or []):
        reqs.append({"updateParagraphStyle": {
            "range": {"startIndex": insert_offset + s, "endIndex": insert_offset + e, "tabId": tab_id},
            "paragraphStyle": {"namedStyleType": level},
            "fields": "namedStyleType",
        }})
    for s, e in open_ranges:
        reqs.append({"updateTextStyle": {
            "range": {"startIndex": insert_offset + s, "endIndex": insert_offset + e, "tabId": tab_id},
            "textStyle": {"bold": True},
            "fields": "bold",
        }})
    return reqs


def _docs_batchupdate(service, doc_id: str, requests: list) -> dict:
    """Wrapper around Docs batchUpdate with pacing and exponential backoff on 429.
    Sleeps 1s after each successful call to stay under the 60 writes/minute quota.
    """
    from googleapiclient.errors import HttpError  # type: ignore
    for attempt in range(8):
        try:
            result = service.documents().batchUpdate(
                documentId=doc_id, body={"requests": requests}
            ).execute()
            time.sleep(1)  # pace to stay under 60 writes/minute quota
            return result
        except HttpError as exc:
            if exc.resp.status == 429 and attempt < 7:
                wait = 2 ** attempt
                log.warning("Docs API rate limit — retrying in %ds (attempt %d)", wait, attempt + 1)
                time.sleep(wait)
            else:
                raise
    return {}


def write_google_doc(shows: list[Show], partial: bool = False) -> None:
    """
    Write shows to a Google Doc.
    partial=False (default): wipe all tabs and rebuild from shows.
    partial=True: update only the tabs for artists present in shows,
                  leaving all other artists' tabs untouched.
    """
    if not GOOGLE_DOC_ID:
        return
    try:
        from googleapiclient.discovery import build  # type: ignore
        from google.oauth2 import service_account  # type: ignore
    except ImportError:
        log.warning("google-api-python-client not installed, skipping Doc output")
        return

    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if not creds_path:
        log.warning("GOOGLE_APPLICATION_CREDENTIALS not set, skipping Doc output")
        return

    scopes = [
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = service_account.Credentials.from_service_account_file(
        creds_path, scopes=scopes
    )
    service = build("docs", "v1", credentials=creds)

    def _top_level_tab_ids(tabs: list) -> list[str]:
        return [t["tabProperties"]["tabId"] for t in tabs]

    def _collect_all_tab_props(tabs: list) -> list[dict]:
        """Recursively collect tabProperties dicts for every tab in the tree."""
        result = []
        for t in tabs:
            result.append(t["tabProperties"])
            result.extend(_collect_all_tab_props(t.get("childTabs", [])))
        return result

    def _write_artist_tabs(svc, artist: str, artist_shows: list[Show], dname: str) -> None:
        by_season: dict[str, list[Show]] = {}
        season_labels: dict[str, str] = {}
        for show in artist_shows:
            sk, label = _season_key(show.date)
            by_season.setdefault(sk, []).append(show)
            season_labels[sk] = label
        sections = [(season_labels[sk], by_season[sk]) for sk in sorted(by_season)]

        resp = _docs_batchupdate(svc, GOOGLE_DOC_ID, [{"addDocumentTab": {
            "tabProperties": {"title": dname[:50]},
        }}])
        artist_tab_id = resp["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]

        full_text, heading_ranges, open_ranges = _assemble_doc_sections(sections)
        all_states = sorted({s.region for s in artist_shows if s.region})
        if all_states:
            full_text += f"\n\nStates: {', '.join(all_states)}"
        style_reqs = _build_style_requests(artist_tab_id, 1, heading_ranges, open_ranges)
        _docs_batchupdate(svc, GOOGLE_DOC_ID, [
            {"insertText": {"location": {"index": 1, "tabId": artist_tab_id}, "text": full_text}},
            *style_reqs,
        ])

        email_text, email_heading_ranges = _build_email_text(artist_shows)
        if email_text:
            resp = _docs_batchupdate(svc, GOOGLE_DOC_ID, [{"addDocumentTab": {
                "tabProperties": {"title": f"{_subtab_prefix(artist)} Email Dates"[:50], "parentTabId": artist_tab_id},
            }}])
            email_tab_id = resp["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]
            email_style_reqs = _build_style_requests(
                email_tab_id, 1,
                heading_ranges=email_heading_ranges,
                open_ranges=[],
                heading_level="HEADING_3",
            )
            _docs_batchupdate(svc, GOOGLE_DOC_ID, [
                {"insertText": {"location": {"index": 1, "tabId": email_tab_id}, "text": email_text}},
                *email_style_reqs,
            ])
            log.info("Doc: wrote %s / Email Dates (%d shows)", dname, len(artist_shows))

        zone_num = 0
        for zone_name, zone_states in EMAIL_ZONES:
            zone_shows = [s for s in artist_shows if s.region in zone_states]
            if not zone_shows:
                continue
            zone_num += 1
            zone_by_season: dict[str, list[Show]] = {}
            zone_season_labels: dict[str, str] = {}
            for show in zone_shows:
                sk, label = _season_key(show.date)
                zone_by_season.setdefault(sk, []).append(show)
                zone_season_labels[sk] = label
            zone_sections = [(zone_season_labels[sk], zone_by_season[sk]) for sk in sorted(zone_by_season)]
            zone_states_present = sorted({s.region for s in zone_shows})
            if len(zone_states_present) < 2:
                continue
            zone_header = (
                f"Email Zone {zone_num}: {zone_name}\n"
                f"States: {', '.join(zone_states_present)}\n\n"
            )
            zone_body, z_heading_ranges, z_open_ranges = _assemble_doc_sections(zone_sections)
            full_zone_text = zone_header + zone_body
            header_len = len(zone_header)
            subtab_title = f"{_subtab_prefix(artist)} Zone: {zone_name}"[:50]
            resp = _docs_batchupdate(svc, GOOGLE_DOC_ID, [{"addDocumentTab": {
                "tabProperties": {"title": subtab_title, "parentTabId": artist_tab_id},
            }}])
            zone_tab_id = resp["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]
            style_reqs = _build_style_requests(
                zone_tab_id, 1,
                heading_ranges=[(header_len + s, header_len + e) for s, e in z_heading_ranges],
                open_ranges=[(header_len + s, header_len + e) for s, e in z_open_ranges],
                extra_headings=[(0, len(f"Email Zone {zone_num}: {zone_name}"), "HEADING_1")],
            )
            _docs_batchupdate(svc, GOOGLE_DOC_ID, [
                {"insertText": {"location": {"index": 1, "tabId": zone_tab_id}, "text": full_zone_text}},
                *style_reqs,
            ])
            log.info("Doc: wrote %s / Zone: %s (%d shows)", dname, zone_name, len(zone_shows))

    doc = service.documents().get(
        documentId=GOOGLE_DOC_ID, includeTabsContent=True
    ).execute()
    top_level_tabs = doc.get("tabs", [])

    # --- Partial mode: update only the artists present in `shows` ---
    if partial:
        all_props = _collect_all_tab_props(top_level_tabs)
        title_to_id = {p["title"]: p["tabId"] for p in all_props}
        total_tab_count = len(all_props)

        by_artist: dict[str, list[Show]] = {}
        for show in shows:
            by_artist.setdefault(show.artist, []).append(show)
        artist_list = sorted(
            [(a, sorted(s, key=lambda x: x.date)) for a, s in by_artist.items()],
            key=lambda x: _display_name(x[0]).lower(),
        )
        for artist, artist_shows in artist_list:
            dname = _display_name(artist)
            tab_title = dname[:50]
            if tab_title in title_to_id:
                if total_tab_count == 1:
                    # Can't delete the last tab — create a placeholder first
                    _docs_batchupdate(service, GOOGLE_DOC_ID, [{"addDocumentTab": {
                        "tabProperties": {"title": f"_tmp_{uuid.uuid4().hex[:8]}"},
                    }}])
                    total_tab_count += 1
                _docs_batchupdate(service, GOOGLE_DOC_ID,
                                  [{"deleteTab": {"tabId": title_to_id[tab_title]}}])
                total_tab_count -= 1
            _write_artist_tabs(service, artist, artist_shows, dname)
        return

    # --- Full rebuild: wipe all tabs and recreate ---
    # Add a fresh _tmp_ placeholder first (guarantees a unique title), then delete
    # all existing top-level tabs.  Parent deletion cascades to children, so we
    # only need to delete top-level tabs.
    resp = _docs_batchupdate(service, GOOGLE_DOC_ID, [{"addDocumentTab": {
        "tabProperties": {"title": f"_tmp_{uuid.uuid4().hex[:8]}"},
    }}])
    placeholder_id = resp["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]
    existing_top_level = _top_level_tab_ids(top_level_tabs)
    if existing_top_level:
        _docs_batchupdate(service, GOOGLE_DOC_ID,
                          [{"deleteTab": {"tabId": tid}} for tid in existing_top_level])

    by_artist: dict[str, list[Show]] = {}
    for show in shows:
        by_artist.setdefault(show.artist, []).append(show)

    artist_list = sorted(
        [(a, sorted(s, key=lambda x: x.date)) for a, s in by_artist.items()],
        key=lambda x: _display_name(x[0]).lower(),
    )

    for artist, artist_shows in artist_list:
        dname = _display_name(artist)
        _write_artist_tabs(service, artist, artist_shows, dname)

    _docs_batchupdate(service, GOOGLE_DOC_ID, [{"deleteTab": {"tabId": placeholder_id}}])
