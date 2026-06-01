import logging
import re

import requests

from config import (
    AIRTABLE_API_KEY,
    AIRTABLE_BASE_ID,
    AIRTABLE_ARTIST_TABLE,
    AIRTABLE_PRIORITY_ORDER,
)

log = logging.getLogger(__name__)


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
