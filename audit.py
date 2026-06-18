"""Reconcile the Airtable Show Calendar against the WordPress events.

A read-only diagnostic: pulls shows from the Airtable Show Calendar and events from the
WP site (via /list-events), maps both sides to a canonical act name, and reports
mismatches by (act, date):
  - in Airtable but missing from WP events
  - in WP events but not in the Airtable Show Calendar
  - WP events with no ticket link (ties into the link-fill feature)
  - rows on either side whose act couldn't be mapped to our roster (so nothing is silent)

No writes anywhere.
"""
import logging
import re

from config import BAND_NAMES, DISPLAY_NAMES
from airtable import fetch_airtable_show_calendar
from outputs.wordpress_events import fetch_wp_events

log = logging.getLogger(__name__)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s).lower())


# Canonical-name lookup: normalized full name / display name / slug -> full BAND_NAME.
def _build_norm_to_band() -> dict[str, str]:
    out: dict[str, str] = {}
    for b in BAND_NAMES:
        out[_norm(b)] = b
    for full, disp in DISPLAY_NAMES.items():
        out.setdefault(_norm(disp), full)
        out[_norm(full)] = full
    return out


_NORM_TO_BAND = _build_norm_to_band()


def _to_band(name: str) -> str:
    """Map an act name/slug to its canonical BAND_NAME, or '' if unknown."""
    return _NORM_TO_BAND.get(_norm(name), "")


def audit_events(upcoming_only: bool = True) -> dict:
    """Compare Airtable Show Calendar vs WP events. Logs a report and returns the
    structured findings (also handy for a test assertion)."""
    air = fetch_airtable_show_calendar(upcoming_only=upcoming_only)
    events = fetch_wp_events()

    # Index Airtable rows by (band, date); collect unmapped acts.
    air_by_key: dict[tuple[str, str], list[dict]] = {}
    air_unmapped: list[dict] = []
    for row in air:
        band = _to_band(row["slug"])
        if not band:
            air_unmapped.append(row)
            continue
        air_by_key.setdefault((band, row["date"]), []).append(row)

    # Index WP events by (band, date); collect unmapped titles and link-less events.
    wp_by_key: dict[tuple[str, str], list[dict]] = {}
    wp_unmapped: list[dict] = []
    no_link: list[dict] = []
    for ev in events:
        if not (ev.get("link") or "").strip():
            no_link.append(ev)
        if not ev.get("date"):
            continue
        band = _to_band(ev.get("title", ""))
        if not band:
            wp_unmapped.append(ev)
            continue
        wp_by_key.setdefault((band, ev["date"]), []).append(ev)

    missing_in_wp = sorted(k for k in air_by_key if k not in wp_by_key)
    missing_in_airtable = sorted(k for k in wp_by_key if k not in air_by_key)

    # ---- report -------------------------------------------------------------
    log.info("=== Airtable Show Calendar ↔ WP events audit%s ===",
             " (upcoming)" if upcoming_only else " (all dates)")
    log.info("Airtable shows: %d (mapped to %d act/date keys, %d unmapped act rows)",
             len(air), len(air_by_key), len(air_unmapped))
    log.info("WP events: %d (mapped to %d act/date keys, %d unmapped titles)",
             len(events), len(wp_by_key), len(wp_unmapped))

    log.info("-- In Airtable but MISSING from WP events: %d --", len(missing_in_wp))
    for band, date in missing_in_wp:
        row = air_by_key[(band, date)][0]
        log.info("  %s | %s | %s, %s", date, band, row.get("venue", ""), row.get("city", ""))

    log.info("-- In WP events but NOT in Airtable: %d --", len(missing_in_airtable))
    for band, date in missing_in_airtable:
        ev = wp_by_key[(band, date)][0]
        log.info("  %s | %s | #%s [%s] | %s", date, band, ev.get("id", ""), ev.get("status", ""), ev.get("location", ""))

    log.info("-- WP events with NO ticket link: %d --", len(no_link))
    for ev in no_link:
        log.info("  %s | #%s [%s] | %s", ev.get("date", ""), ev.get("id", ""), ev.get("status", ""), ev.get("title", ""))

    if air_unmapped:
        slugs = sorted({r["slug"] for r in air_unmapped if r.get("slug")})
        log.warning("-- Airtable rows whose act didn't map to the roster: %d (slugs: %s) --",
                    len(air_unmapped), ", ".join(slugs[:20]) + (" …" if len(slugs) > 20 else ""))
    if wp_unmapped:
        titles = sorted({e.get("title", "") for e in wp_unmapped})
        log.warning("-- WP event titles that didn't map to the roster: %d (%s) --",
                    len(wp_unmapped), ", ".join(titles[:20]) + (" …" if len(titles) > 20 else ""))

    return {
        "missing_in_wp": missing_in_wp,
        "missing_in_airtable": missing_in_airtable,
        "no_link": no_link,
        "air_unmapped": air_unmapped,
        "wp_unmapped": wp_unmapped,
    }
