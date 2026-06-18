import logging
import re
from datetime import date as _date

import requests

from config import (
    AIRTABLE_API_KEY,
    AIRTABLE_BASE_ID,
    AIRTABLE_ARTIST_TABLE,
    AIRTABLE_PRIORITY_ORDER,
    AIRTABLE_SHOW_CALENDAR_BASE_ID,
    AIRTABLE_SHOW_CALENDAR_TABLE,
)

log = logging.getLogger(__name__)


def _first(val):
    """Airtable lookup fields come back as single-element lists; unwrap to a scalar."""
    if isinstance(val, list):
        return val[0] if val else ""
    return val


_TITLE_ITEM_SLUG_RE = re.compile(r"/title-item/([^/]+)/?")


def _slug_from_web_link(link: str) -> str:
    """Pull the act slug from an LPI web link, e.g. '.../title-item/free-fallin/' -> 'free-fallin'."""
    m = _TITLE_ITEM_SLUG_RE.search(str(link or ""))
    return m.group(1).lower() if m else ""


def fetch_airtable_show_calendar(upcoming_only: bool = True) -> list[dict]:
    """Return shows from the Airtable Show Calendar as
    {date, venue, city, region, slug, web_link, record_id}. `slug` is the act slug pulled
    from the 'LPI Web Link (from Show Title)' lookup. Rows without a Show Date are skipped
    (they can't be reconciled by date). Pages through all records.
    """
    if not AIRTABLE_API_KEY:
        log.warning("AIRTABLE_API_KEY not set, skipping Show Calendar fetch")
        return []
    url = f"https://api.airtable.com/v0/{AIRTABLE_SHOW_CALENDAR_BASE_ID}/{AIRTABLE_SHOW_CALENDAR_TABLE}"
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
    fields = ["Show Date", "Venue", "City", "State", "LPI Web Link (from Show Title)"]
    today = _date.today().isoformat()

    out: list[dict] = []
    offset = ""
    try:
        while True:
            params = [("fields[]", f) for f in fields] + [("pageSize", "100")]
            if offset:
                params.append(("offset", offset))
            resp = requests.get(url, headers=headers, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            for rec in data.get("records", []):
                f = rec.get("fields", {})
                date = str(_first(f.get("Show Date", "")))[:10]
                if not date:
                    continue
                if upcoming_only and date < today:
                    continue
                web_link = str(_first(f.get("LPI Web Link (from Show Title)", "")))
                out.append({
                    "date": date,
                    "venue": str(_first(f.get("Venue", ""))),
                    "city": str(_first(f.get("City", ""))),
                    "region": str(_first(f.get("State", ""))),
                    "slug": _slug_from_web_link(web_link),
                    "web_link": web_link,
                    "record_id": rec.get("id", ""),
                })
            offset = data.get("offset", "")
            if not offset:
                break
    except Exception as exc:
        log.error("Airtable Show Calendar fetch error: %s", exc)
        return []
    log.info("Airtable Show Calendar: %d show(s)%s.", len(out), " (upcoming)" if upcoming_only else "")
    return out


def fetch_airtable_priority_artists() -> list[dict]:
    """
    Return artists from Airtable with Marketing Priority in AIRTABLE_PRIORITY_ORDER,
    sorted by priority then name. Each dict has 'name' and 'priority' keys.
    """
    if not AIRTABLE_API_KEY:
        log.warning("AIRTABLE_API_KEY not set, skipping Airtable fetch")
        return []
    priority_filter = ", ".join(
        f"{{Marketing Priority}}='{p}'" for p in AIRTABLE_PRIORITY_ORDER
    )
    params = {
        "fields[]": ["Artist / Show Name", "Marketing Priority"],
        "filterByFormula": f"OR({priority_filter})",
    }
    try:
        resp = requests.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_ARTIST_TABLE}",
            headers={"Authorization": f"Bearer {AIRTABLE_API_KEY}"},
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as exc:
        log.error("Airtable fetch error: %s", exc)
        return []

    def _priority_key(record: dict) -> int:
        p = record["fields"].get("Marketing Priority", "")
        try:
            return AIRTABLE_PRIORITY_ORDER.index(p)
        except ValueError:
            return len(AIRTABLE_PRIORITY_ORDER)

    def _normalize_name(name: str) -> str:
        """Convert 'X, The' → 'The X' (Airtable moves articles to end for sorting)."""
        m = re.match(r"^(.+),\s*(The|A|An)$", name, re.IGNORECASE)
        if m:
            return f"{m.group(2).capitalize()} {m.group(1)}"
        return name

    records = sorted(resp.json().get("records", []), key=_priority_key)
    return [
        {
            "name": _normalize_name(r["fields"].get("Artist / Show Name", "")),
            "priority": r["fields"].get("Marketing Priority", ""),
        }
        for r in records
        if r["fields"].get("Artist / Show Name")
    ]
