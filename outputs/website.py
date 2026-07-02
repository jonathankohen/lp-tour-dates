import logging
from dataclasses import asdict
from datetime import datetime, timezone

import requests

from config import OUTPUT_WEBSITE_URL, OUTPUT_WEBSITE_SECRET, _display_name
from models import Show


def _payload_show(show: Show) -> dict:
    """Serialize a Show for the front-end, presenting the user-facing display name (e.g.
    "Concert of Kings", "Kiss The Sky") rather than the internal roster name. The plugin
    renders `artist` verbatim and groups/filters on it, so this is the label users see."""
    d = asdict(show)
    d["artist"] = _display_name(show.artist)
    return d

log = logging.getLogger(__name__)


def write_website(shows: list[Show]) -> None:
    """
    POST the shows JSON to the WordPress tour-calendar plugin's ingest endpoint
    (or any webhook). Payload shape matches outputs/json_output.py::write_json:
        { "generated_at": ISO-8601, "shows": [...] }
    The shared secret is sent as the X-Tour-Secret header so the plugin can
    reject unauthenticated writes. Expects a 2xx response.
    """
    if not OUTPUT_WEBSITE_URL:
        return
    # The front-end is the authoritative output, so dedup the payload here — at the publish
    # boundary — so it's clean whether the caller passed freshly-aggregated shows or a Sheet
    # read-back (which can carry the same show under two venue spellings from two sources).
    from aggregation import dedup_for_publish
    shows = dedup_for_publish(shows)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "shows": [_payload_show(s) for s in shows],
    }
    headers = {}
    if OUTPUT_WEBSITE_SECRET:
        headers["X-Tour-Secret"] = OUTPUT_WEBSITE_SECRET
    try:
        resp = requests.post(OUTPUT_WEBSITE_URL, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        log.info("Posted %d shows to %s", len(shows), OUTPUT_WEBSITE_URL)
    except Exception as exc:
        log.error("Website output error: %s", exc)
