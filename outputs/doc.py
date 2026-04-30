import logging
import os
import time
import uuid
from datetime import date as _date, timedelta

from config import GOOGLE_DOC_ID, _display_name
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
        date_str = d.strftime("%A, %B %-d, %Y")
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
    by_month: dict[str, list[Show]],
) -> tuple[str, list[tuple[int, int]], list[tuple[int, int]]]:
    """
    Assemble multi-month content from a dict of {YYYY-MM: [Show]}.
    Returns (text, heading_ranges, open_ranges) as 0-based char offsets within text.
    Caller adds the doc insert index (usually 1) to get actual doc positions.
    """
    parts: list[str] = []
    heading_ranges: list[tuple[int, int]] = []
    open_ranges: list[tuple[int, int]] = []
    pos = 0

    for i, (month_key, month_shows) in enumerate(sorted(by_month.items())):
        month_label = _date.fromisoformat(month_key + "-01").strftime("%B %Y")
        month_text, m_open = _build_doc_month_text(month_shows)

        if i > 0:
            parts.append("\n\n")
            pos += 2

        heading_ranges.append((pos, pos + len(month_label)))
        parts.append(month_label + "\n")
        pos += len(month_label) + 1

        for s, e in m_open:
            open_ranges.append((pos + s, pos + e))

        parts.append(month_text)
        pos += len(month_text)

    return "".join(parts), heading_ranges, open_ranges


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


def write_google_doc(shows: list[Show]) -> None:
    """
    Write all shows to a Google Doc — one tab per artist, one subtab per month.
    Each subtab contains plain text: date, venue, city/region, with up to 2 open
    dates before and after each booked show. No ticket links.
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

    # Can't delete the last remaining tab, so we create a short-lived placeholder
    # first, then re-query the doc for currently-live tab IDs, delete those
    # (clearing any stale name conflicts), then build real artist tabs, and
    # finally remove the placeholder.
    placeholder_resp = _docs_batchupdate(service, GOOGLE_DOC_ID, [{"addDocumentTab": {
        "tabProperties": {"title": f"_tmp_{uuid.uuid4().hex[:8]}"},
    }}])
    placeholder_id = placeholder_resp["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]

    doc = service.documents().get(
        documentId=GOOGLE_DOC_ID, includeTabsContent=True
    ).execute()
    old_tab_ids = [
        t["tabProperties"]["tabId"]
        for t in doc.get("tabs", [])
        if t["tabProperties"]["tabId"] != placeholder_id
    ]
    if old_tab_ids:
        _docs_batchupdate(service, GOOGLE_DOC_ID,
                          [{"deleteTab": {"tabId": tid}} for tid in old_tab_ids])

    by_artist: dict[str, list[Show]] = {}
    for show in shows:
        by_artist.setdefault(show.artist, []).append(show)

    artist_list = [(a, sorted(s, key=lambda x: x.date)) for a, s in by_artist.items()]

    for artist, artist_shows in artist_list:
        dname = _display_name(artist)
        by_month: dict[str, list[Show]] = {}
        for show in artist_shows:
            by_month.setdefault(show.date[:7], []).append(show)

        # --- Artist parent tab ---
        resp = _docs_batchupdate(service, GOOGLE_DOC_ID, [{"addDocumentTab": {
            "tabProperties": {"title": dname[:50]},
        }}])
        artist_tab_id = resp["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]

        full_text, heading_ranges, open_ranges = _assemble_doc_sections(by_month)
        all_states = sorted({s.region for s in artist_shows if s.region})
        if all_states:
            full_text += f"\n\nStates: {', '.join(all_states)}"
        style_reqs = _build_style_requests(artist_tab_id, 1, heading_ranges, open_ranges)
        _docs_batchupdate(service, GOOGLE_DOC_ID, [
            {"insertText": {"location": {"index": 1, "tabId": artist_tab_id}, "text": full_text}},
            *style_reqs,
        ])

        # --- Month subtabs ---
        for month_key, month_shows in sorted(by_month.items()):
            month_label = _date.fromisoformat(month_key + "-01").strftime("%B %Y")
            subtab_title = f"{dname} {month_label}"[:50]
            resp = _docs_batchupdate(service, GOOGLE_DOC_ID, [{"addDocumentTab": {
                "tabProperties": {"title": subtab_title, "parentTabId": artist_tab_id},
            }}])
            subtab_id = resp["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]

            month_text, m_open = _build_doc_month_text(month_shows)
            body_text = f"{month_label}\n{month_text}"
            offset = len(month_label) + 1
            style_reqs = _build_style_requests(
                subtab_id, 1,
                heading_ranges=[(0, len(month_label))],
                open_ranges=[(offset + s, offset + e) for s, e in m_open],
            )
            _docs_batchupdate(service, GOOGLE_DOC_ID, [
                {"insertText": {"location": {"index": 1, "tabId": subtab_id}, "text": body_text}},
                *style_reqs,
            ])
            log.info("Doc: wrote %s / %s (%d shows)", dname, month_label, len(month_shows))

        # --- Email zone subtabs ---
        zone_num = 0
        for zone_name, zone_states in EMAIL_ZONES:
            zone_shows = [s for s in artist_shows if s.region in zone_states]
            if not zone_shows:
                continue
            zone_num += 1

            zone_by_month: dict[str, list[Show]] = {}
            for show in zone_shows:
                zone_by_month.setdefault(show.date[:7], []).append(show)

            zone_states_present = sorted({s.region for s in zone_shows})
            if len(zone_states_present) < 2:
                continue
            zone_header = (
                f"Email Zone {zone_num}: {zone_name}\n"
                f"States: {', '.join(zone_states_present)}\n\n"
            )
            zone_body, z_heading_ranges, z_open_ranges = _assemble_doc_sections(zone_by_month)
            full_zone_text = zone_header + zone_body
            header_len = len(zone_header)

            subtab_title = f"{dname} Zone: {zone_name}"[:50]
            resp = _docs_batchupdate(service, GOOGLE_DOC_ID, [{"addDocumentTab": {
                "tabProperties": {"title": subtab_title, "parentTabId": artist_tab_id},
            }}])
            zone_tab_id = resp["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]

            style_reqs = _build_style_requests(
                zone_tab_id, 1,
                heading_ranges=[(header_len + s, header_len + e) for s, e in z_heading_ranges],
                open_ranges=[(header_len + s, header_len + e) for s, e in z_open_ranges],
                extra_headings=[(0, len(f"Email Zone {zone_num}: {zone_name}"), "HEADING_1")],
            )
            _docs_batchupdate(service, GOOGLE_DOC_ID, [
                {"insertText": {"location": {"index": 1, "tabId": zone_tab_id}, "text": full_zone_text}},
                *style_reqs,
            ])
            log.info("Doc: wrote %s / Zone: %s (%d shows)", dname, zone_name, len(zone_shows))

    _docs_batchupdate(service, GOOGLE_DOC_ID, [{"deleteTab": {"tabId": placeholder_id}}])
