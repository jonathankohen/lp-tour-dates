import logging
from dataclasses import asdict
from datetime import datetime, timezone

import requests

from config import (OUTPUT_WEBSITE_URL, OUTPUT_WEBSITE_SECRET, OUTPUT_MIRROR_URL,
                    OUTPUT_MIRROR_SECRET, _display_name, is_private_booking,
                    is_cruise_sailing)
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

    The identical payload also goes to OUTPUT_MIRROR_URL (the Cloudflare Worker in
    cloudflare-worker/) when that is set. Each target is pushed independently: a
    failure on one is logged and does not stop the other, so the two copies can
    drift by at most one run.
    """
    targets = [(name, url, secret) for name, url, secret in (
        ("Website", OUTPUT_WEBSITE_URL, OUTPUT_WEBSITE_SECRET),
        ("Mirror", OUTPUT_MIRROR_URL, OUTPUT_MIRROR_SECRET),
    ) if url]
    if not targets:
        return
    # The front-end is the authoritative output, so dedup the payload here — at the publish
    # boundary — so it's clean whether the caller passed freshly-aggregated shows or a Sheet
    # read-back (which can carry the same show under two venue spellings from two sources).
    from aggregation import dedup_for_publish
    shows = dedup_for_publish(shows)
    # Private parties / corporate buyouts are real booked dates, but the public must not be
    # invited to them. They stay in the Sheet and routing Doc (the date is still blocked for
    # routing) and are stripped here, at the publish boundary — so a private show can't reach
    # the front-end whether it came from aggregation or from a Sheet read-back.
    public = [s for s in shows
              if not is_private_booking(s.venue, s.city, s.title)
              and not is_cruise_sailing(s.artist, s.venue, s.city)]
    if len(public) != len(shows):
        log.info("Withheld %d private or cruise-itinerary date(s) from the front-end.",
                 len(shows) - len(public))
    shows = public
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "shows": [_payload_show(s) for s in shows],
    }
    for name, url, secret in targets:
        _post_payload(name, url, secret, payload)


def _post_payload(name: str, url: str, secret: str, payload: dict) -> bool:
    """POST one payload to one ingest endpoint. Logs and returns False on failure
    rather than raising, so a dead target can't block the others."""
    headers = {}
    if secret:
        headers["X-Tour-Secret"] = secret
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        log.info("%s: posted %d shows to %s", name, len(payload["shows"]), url)
        return True
    except Exception as exc:
        log.error("%s output error: %s", name, exc)
        return False
