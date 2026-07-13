"""
Publish aggregated shows to the WordPress site as VS Event List `event` posts.

VS Event List exposes no usable REST API (its CPT/meta aren't registered with
show_in_rest), so creation is delegated to the Tour Calendar plugin's
/publish-events endpoint, which calls native wp_insert_post()/update_post_meta()
server-side. This module gathers each act's fallback image + description from a
Google Drive folder and POSTs everything; the server decides per show whether to
skip (act + date already exists), copy image/body from an existing event of the
same act, or use the Drive fallback. Mirrors outputs/website.py for transport.
"""
import base64
import calendar
import io
import logging
import os
import re
import time
from dataclasses import asdict
from datetime import date as _date

import requests

from config import (
    WORDPRESS_PUBLISH_EVENTS_URL,
    WORDPRESS_CLEANUP_DUPLICATES_URL,
    WORDPRESS_UPDATE_DESCRIPTIONS_URL,
    WORDPRESS_UPDATE_LINKS_URL,
    WORDPRESS_ASSETS_DRIVE_FOLDER_ID,
    WORDPRESS_DEFAULT_EVENT_TIME,
    OUTPUT_WEBSITE_SECRET,
    EVENT_CATEGORIES,
    _fmt_time_12h,
)
from models import Show

log = logging.getLogger(__name__)

_IMAGE_MIME_PREFIX = "image/"


def _markdown_emphasis_to_html(text: str) -> str:
    """Render Markdown bold/italic emphasis to HTML so it doesn't reach the published event
    as literal asterisks. The Drive bios are authored in Markdown (e.g. ``***Navidad***``,
    ``**Legends of Classic Rock**``, ``*Taxi*``) but the plugin only wraps paragraphs — it
    never parses Markdown — so the asterisks render verbatim. Convert here, before the text is
    sent: ``<strong>``/``<em>`` survive the plugin's wp_kses_post and render correctly.

    Order matters (***  before **  before *); only balanced runs convert, and the inner
    capture excludes ``*`` so adjacent emphases on one line (``**Name***Title*``) split right.
    Underscores are left alone — too easily tripped by names/URLs."""
    text = re.sub(r"\*\*\*([^*]+?)\*\*\*", r"<strong><em>\1</em></strong>", text)
    text = re.sub(r"\*\*([^*]+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<em>\1</em>", text)
    return text

# Document formats we can turn into a description. Google Docs are exported to
# text/plain through the Drive API; everything else is downloaded raw and parsed
# locally (.docx via the stdlib, .pdf via pypdf, legacy .doc/.rtf via macOS
# `textutil`).
_GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_DOC_MIME = "application/msword"
_PDF_MIME = "application/pdf"
_RTF_MIMES = ("application/rtf", "text/rtf")
_DESCRIPTION_MIMES = {
    "text/plain", "text/markdown", "text/x-markdown",
    _DOC_MIME, _DOCX_MIME, _GOOGLE_DOC_MIME, _PDF_MIME, *_RTF_MIMES,
}
_DESCRIPTION_EXTENSIONS = (".txt", ".text", ".md", ".markdown", ".rtf", ".doc", ".docx", ".pdf")

# Placeholder/private shows that should never become public-facing events.
_PRIVATE_TBA_RE = re.compile(r"private event|on hold|\btba\b", re.IGNORECASE)

# Publish in small chunks so a single request can't exceed the server's PHP
# max_execution_time (default 30s) — image sideloading + EWWW optimization is the
# bottleneck. The endpoint is idempotent (act+date already on the site is skipped),
# so sequential chunks are safe and a re-run resumes where a failure left off.
_PUBLISH_CHUNK_SIZE = int(os.environ.get("WORDPRESS_PUBLISH_CHUNK_SIZE", "6"))

# Hard ceiling on the inline base64 assets in one request, kept under the server's
# ~1MB request-body limit (over it the request 404s with rest_no_route). Chunks are
# packed to respect this AND the show count above — so however large-image artists
# cluster by date, no request goes over. Images are downscaled first, so a single
# artist never approaches this alone.
_PUBLISH_ASSET_BUDGET = int(os.environ.get("WORDPRESS_PUBLISH_ASSET_BUDGET", str(800 * 1024)))


def _asset_bytes(assets: dict, artist: str) -> int:
    """Bytes an artist's assets add to a payload (base64 image + description text)."""
    e = assets.get(artist) or {}
    return len(e.get("image_b64", "")) + len(e.get("description", ""))


def _pack_chunks(shows: list[Show], assets: dict) -> list[list[Show]]:
    """Group shows into chunks bounded by BOTH the show count (server execution time)
    and the total asset bytes of the chunk's distinct artists (server ~1MB body limit).
    Assets dedupe per artist within a chunk, so extra shows of an artist already in the
    chunk cost nothing. A lone artist over budget still ships alone (downscaling makes
    that not happen in practice) rather than being dropped."""
    chunks: list[list[Show]] = []
    cur: list[Show] = []
    cur_artists: set[str] = set()
    cur_bytes = 0
    for s in shows:
        add_bytes = 0 if s.artist in cur_artists else _asset_bytes(assets, s.artist)
        if cur and (len(cur) >= _PUBLISH_CHUNK_SIZE or cur_bytes + add_bytes > _PUBLISH_ASSET_BUDGET):
            chunks.append(cur)
            cur, cur_artists, cur_bytes = [], set(), 0
            add_bytes = _asset_bytes(assets, s.artist)
        cur.append(s)
        if s.artist not in cur_artists:
            cur_artists.add(s.artist)
            cur_bytes += add_bytes
    if cur:
        chunks.append(cur)
    return chunks


def _is_private_or_tba(show: Show) -> bool:
    """True for private holds / unannounced placeholders (venue or city)."""
    return bool(_PRIVATE_TBA_RE.search(f"{show.venue} {show.city}"))


def _one_month_cutoff() -> str:
    """ISO date one month from today, with the day clamped to the target month."""
    t = _date.today()
    year, month = (t.year + 1, 1) if t.month == 12 else (t.year, t.month + 1)
    day = min(t.day, calendar.monthrange(year, month)[1])
    return _date(year, month, day).isoformat()


# An act playing one venue at least this many times (across the upcoming shows being
# published) is treated as a residency: its shows collapse into ONE event per calendar
# month spanning a date range, instead of one event per show. Front-end/Sheet/Doc are
# unaffected — this only reshapes the event-post payload.
_RESIDENCY_MIN_SHOWS = 4


def _cluster_by_venue(area_shows: list[Show]) -> list[list[Show]]:
    """Cluster one act+city's shows into per-venue groups, merging venues whose names
    share a distinctive token so spelling variants ("South Point Casino" / "South Point
    Hotel & Casino") count as one venue. Mirrors the overlap logic in
    aggregation._collapse_by_city_venue. A venue with no distinctive token is keyed by its
    normalized full name, so identical spellings still group while distinct ones don't."""
    from aggregation import _venue_tokens  # local import avoids an import cycle

    clusters: list[tuple[set, list[Show]]] = []  # (token-set, shows)
    for s in area_shows:
        toks = _venue_tokens(s.venue) or {"venue:" + _norm(s.venue)}
        for i, (ctoks, members) in enumerate(clusters):
            if ctoks & toks:  # shares a distinctive token -> same venue
                members.append(s)
                clusters[i] = (ctoks | toks, members)
                break
        else:
            clusters.append((set(toks), [s]))
    return [members for _toks, members in clusters]


def _collapse_residencies(shows: list[Show]) -> tuple[list[Show], dict[int, dict]]:
    """Collapse residency shows into one synthetic Show per (residency venue, month).

    A group of >= _RESIDENCY_MIN_SHOWS shows at the same act+venue is a residency; its
    shows are split by calendar month and each month becomes a single date-range event
    (start = earliest, end = latest that month). Returns (shows, meta) where `meta` maps
    id(synthetic_show) -> extra publish fields (is_residency, end_date, residency_dates).
    Non-residency shows pass through untouched with no meta entry.
    """
    by_area: dict[tuple, list[Show]] = {}
    for s in shows:
        by_area.setdefault((s.artist.lower().strip(), s.city.lower().strip()), []).append(s)

    groups: list[list[Show]] = []
    for area_shows in by_area.values():
        groups.extend(_cluster_by_venue(area_shows))

    from aggregation import _venue_tokens  # local import avoids an import cycle

    out: list[Show] = []
    meta: dict[int, dict] = {}
    for group in groups:
        if len(group) < _RESIDENCY_MIN_SHOWS:
            out.extend(group)
            continue
        # A real residency needs a real venue. Shows whose "venue" has no distinctive token
        # (e.g. the mis-parsed cruise codes "ST"/"IC", which also carry no city) must never
        # collapse — clustering them by a normalized-name fallback would forge a residency out
        # of unrelated shows that merely share a meaningless code.
        if not any(_venue_tokens(s.venue) for s in group):
            out.extend(group)
            continue
        # Residency: one synthetic event per calendar month (YYYY-MM).
        by_month: dict[str, list[Show]] = {}
        for s in group:
            by_month.setdefault(s.date[:7], []).append(s)
        for month_shows in by_month.values():
            month_shows.sort(key=lambda s: (s.date, s.start_time))
            first, last = month_shows[0], month_shows[-1]
            ticket_url = next((s.ticket_url for s in month_shows if s.ticket_url.startswith("http")), "")
            synthetic = Show(
                artist=first.artist,
                date=first.date,             # start of the range
                venue=first.venue,
                city=first.city,
                region=first.region,
                country=first.country,
                ticket_url=ticket_url,
                source=first.source,
                start_time="",               # omitted on the event; per-date times go in the body
            )
            out.append(synthetic)
            meta[id(synthetic)] = {
                "is_residency": True,
                "end_date": last.date,
                "residency_dates": [
                    {"date": s.date, "start_time": _fmt_time_12h(s.start_time)} for s in month_shows
                ],
            }
    return out, meta


def publish_events(
    shows: list[Show],
    dry_run: bool = False,
    limit: int = 0,
    one_month: bool = False,
    verify_links: bool = False,
    post_status: str = "draft",
    replace_residencies: bool = False,
) -> list[Show]:
    """
    Create a draft `event` post for every show not already on the site.

    Private/placeholder shows are always excluded. When one_month is set, only
    shows dated within the next month are considered. limit caps how many events
    the server creates (0 = unlimited) — mainly for testing. When dry_run is True
    the server plans the work and writes nothing; the summary of what *would* be
    created is logged so the operator can review before going live.

    post_status ("draft"|"publish") sets the status of created/updated events — drafts
    for staff review by default, "publish" for the residency migration so the public
    calendar swaps with no gap. When replace_residencies is True, after each residency
    range event is created the server trashes that act's individual single events inside
    the month's range at the same venue (the one-event-per-show → one-event-per-month
    migration); pair it with post_status="publish".

    When verify_links is set, each publishable show's ticket link is page-verified and
    AI-corrected first (see enrichment.verify_and_fix_ticket_links). Returns the shows
    whose ticket_url was corrected (so the caller can propagate them), or [].
    """
    if not WORDPRESS_PUBLISH_EVENTS_URL:
        log.warning("WORDPRESS_PUBLISH_EVENTS_URL not set (and OUTPUT_WEBSITE_URL empty) — nothing to publish to.")
        return []
    if not shows:
        log.warning("No shows to publish.")
        return []

    # Always drop private holds and unannounced (TBA) placeholders.
    kept = [s for s in shows if not _is_private_or_tba(s)]
    if len(kept) != len(shows):
        log.info("Excluded %d private/TBA show(s).", len(shows) - len(kept))
    shows = kept

    # Only ever draft upcoming shows — never create events for past dates.
    today = _date.today().isoformat()
    upcoming = [s for s in shows if s.date >= today]
    if len(upcoming) != len(shows):
        log.info("Excluded %d past show(s) (before %s).", len(shows) - len(upcoming), today)
    shows = upcoming

    if one_month:
        cutoff = _one_month_cutoff()
        before = len(shows)
        shows = [s for s in shows if s.date <= cutoff]
        log.info("One-month window (through %s): %d of %d shows.", cutoff, len(shows), before)

    if not shows:
        log.warning("No shows left to publish after filters.")
        return []

    # Page-verify each link and AI-correct the failures, before drafting events with them.
    # (Runs on the raw per-show list so each link is checked before any residency collapse.)
    corrected: list[Show] = []
    if verify_links:
        # Bound the QA pass by --limit so link-fixing can run in small, cheap batches:
        # verify/fix (and then publish) only the earliest `limit` upcoming shows.
        if limit > 0 and len(shows) > limit:
            shows = sorted(shows, key=lambda s: (s.date, s.artist))[:limit]
            log.info("verify-links: bounded to the earliest %d show(s) (matches --limit).", limit)
        from enrichment import verify_and_fix_ticket_links
        corrected = verify_and_fix_ticket_links(shows)

    # Collapse residencies (4+ shows of an act at one venue) into one date-range event
    # per calendar month. Only reshapes the event-post payload; other outputs are untouched.
    before = len(shows)
    shows, residency_meta = _collapse_residencies(shows)
    if len(shows) != before:
        log.info("Collapsed residency shows into monthly range events: %d show(s) -> %d event(s).",
                 before, len(shows))

    artists = sorted({s.artist for s in shows})
    assets = _load_drive_assets(artists)

    headers = {}
    if OUTPUT_WEBSITE_SECRET:
        headers["X-Tour-Secret"] = OUTPUT_WEBSITE_SECRET

    # Pack into chunks bounded by show count (execution time) and asset bytes (request
    # body limit). Only the assets for the artists in a chunk ride along.
    chunks = _pack_chunks(shows, assets)
    log.info("Publishing in %d chunk(s) of up to %d shows / %d KB assets each%s.",
             len(chunks), _PUBLISH_CHUNK_SIZE, _PUBLISH_ASSET_BUDGET // 1024,
             " — dry run" if dry_run else "")

    combined = {"created": [], "skipped": [], "would_create": [], "errors": [], "trashed": [], "would_trash": []}
    made = 0  # events created (or planned, in dry-run) so far — what `limit` caps
    consecutive_failures = 0
    for idx, chunk in enumerate(chunks, 1):
        if limit and made >= limit:
            break
        chunk_artists = {s.artist for s in chunk}
        # The plugin stores start_time verbatim into the displayed `event-time` meta,
        # so format to 12-hour here (canonical Show.start_time stays 24-hour).
        show_dicts = []
        for s in chunk:
            d = asdict(s)
            d["start_time"] = _fmt_time_12h(d.get("start_time", ""))
            d.update(residency_meta.get(id(s), {}))  # is_residency/end_date/residency_dates, if any
            show_dicts.append(d)
        payload = {
            "dry_run": dry_run,
            "default_time": _fmt_time_12h(WORDPRESS_DEFAULT_EVENT_TIME),
            "limit": (limit - made) if limit else 0,
            "publish_status": "publish" if post_status == "publish" else "draft",
            "replace_residency_singles": replace_residencies,
            "shows": show_dicts,
            "assets": {a: assets[a] for a in chunk_artists if a in assets},
            "categories": {a: EVENT_CATEGORIES[a] for a in chunk_artists if a in EVENT_CATEGORIES},
        }
        result = _post_publish(payload, headers)
        if result is None:
            consecutive_failures += 1
            if consecutive_failures >= 3:
                log.error("Aborting after %d consecutive chunk failures — try a smaller "
                          "WORDPRESS_PUBLISH_CHUNK_SIZE (currently %d). Already-created "
                          "events are kept; re-run to resume.", consecutive_failures, _PUBLISH_CHUNK_SIZE)
                break
            continue
        consecutive_failures = 0
        for key in combined:
            combined[key].extend(result.get(key) or [])
        key = "would_create" if dry_run else "created"
        n = len(result.get(key) or [])
        made += n
        log.info("Chunk %d/%d: %d %s.", idx, len(chunks), n, "planned" if dry_run else "created")

    _log_summary(combined, dry_run)
    return corrected


# Transient server states worth retrying a chunk for: 408/425/429 (timeout / too-early
# / rate limit) and 5xx (overload). NOT 404 — a `rest_no_route` 404 here is deterministic:
# the request body exceeded the server's ~1MB limit (an oversized inline image) or the
# plugin route genuinely isn't registered. Retrying fixes neither, so we fail fast with a
# hint. Chunks are idempotent (existing act+date is skipped) so retries are otherwise safe.
_PUBLISH_RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}
_PUBLISH_MAX_ATTEMPTS = 4


def _post_publish(payload: dict, headers: dict) -> dict | None:
    """POST one publish-events chunk, retrying transient failures. Returns the parsed
    result, or None once exhausted (logged with the server's response body)."""
    for attempt in range(1, _PUBLISH_MAX_ATTEMPTS + 1):
        try:
            # Longer timeout than the ingest webhook: the server may sideload images.
            resp = requests.post(WORDPRESS_PUBLISH_EVENTS_URL, json=payload, headers=headers, timeout=120)
            resp.raise_for_status()
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status in _PUBLISH_RETRY_STATUSES and attempt < _PUBLISH_MAX_ATTEMPTS:
                log.warning("publish-events chunk got %s (attempt %d/%d) — retrying…",
                            status, attempt, _PUBLISH_MAX_ATTEMPTS)
                time.sleep(1.5 * attempt)
                continue
            body = exc.response.text[:2000] if exc.response is not None else ""
            hint = ""
            if status == 404:
                hint = (" [a 'rest_no_route' 404 here almost always means the request body "
                        "exceeded the server's ~1MB limit (oversized inline image), not a "
                        "missing route]")
            log.error("publish-events chunk failed (%s)%s at %s. Server response:\n%s",
                      status, hint, WORDPRESS_PUBLISH_EVENTS_URL, body or "(empty body)")
            return None
        except Exception as exc:
            if attempt < _PUBLISH_MAX_ATTEMPTS:
                log.warning("publish-events chunk error (attempt %d/%d): %s — retrying…",
                            attempt, _PUBLISH_MAX_ATTEMPTS, exc)
                time.sleep(1.5 * attempt)
                continue
            log.error("publish-events chunk request failed (%s): %s", WORDPRESS_PUBLISH_EVENTS_URL, exc)
            return None
        try:
            return resp.json()
        except ValueError:
            log.error("publish-events returned non-JSON response: %s", resp.text[:500])
            return None
    return None


def _log_summary(result: dict, dry_run: bool) -> None:
    created = result.get("created", []) or []
    skipped = result.get("skipped", []) or []
    would = result.get("would_create", []) or []
    errors = result.get("errors", []) or []
    trashed = result.get("trashed", []) or []
    would_trash = result.get("would_trash", []) or []

    n_exists = sum(1 for s in skipped if s.get("reason") == "exists")
    n_nocontent = sum(1 for s in skipped if s.get("reason") == "no_content")
    reconciled = [s for s in skipped if s.get("categorized")]

    if dry_run:
        log.info(
            "DRY RUN — %d would be created; skipped %d (already exist) + %d (no image/body).",
            len(would), n_exists, n_nocontent,
        )
        for p in would:
            # Residency events show the date range instead of a single date.
            when = p.get("date", "")
            if p.get("is_residency") and p.get("end_date"):
                verb = "update" if p.get("action") == "update" else "residency"
                when = "%s→%s (%s)" % (when, p.get("end_date"), verb)
            log.info(
                "  + %s | %s | %s | cats=%s | link=%s | body=%s image=%s",
                when, p.get("title", ""), p.get("location", ""),
                ", ".join(p.get("categories") or []) or "(none)",
                p.get("link", "") or "(none)", p.get("body_source", ""), p.get("image_source", ""),
            )
        if would_trash:
            log.info("  would TRASH %d individual single event(s) replaced by residency ranges:", len(would_trash))
            for t in would_trash:
                log.info("      - #%s %s (replaced by #%s)", t.get("id", ""), t.get("date", ""), t.get("replaced_by", ""))
    else:
        log.info(
            "Published — %d created/updated; skipped %d (already exist) + %d (no image/body); "
            "added categories to %d existing event(s); trashed %d replaced single event(s).",
            len(created), n_exists, n_nocontent, len(reconciled), len(trashed),
        )
        for c in created:
            cats = ", ".join(c.get("categories") or []) or "(none)"
            log.info("  %s #%s %s | %s | cats=%s", "~" if c.get("action") == "updated" else "+",
                     c.get("id", ""), c.get("artist", ""), c.get("date", ""), cats)
        for s in reconciled:
            log.info("  ~ added to existing %s | %s | +cats=%s",
                     s.get("date", ""), s.get("artist", ""), ", ".join(s.get("categorized") or []))
        for t in trashed:
            log.info("  x trashed #%s %s (replaced by #%s)", t.get("id", ""), t.get("date", ""), t.get("replaced_by", ""))

    for s in skipped:
        log.debug(
            "  = skip [%s] %s | %s (matched '%s')",
            s.get("reason", ""), s.get("date", ""), s.get("artist", ""), s.get("matched_title", ""),
        )
    for e in errors:
        log.error("  ! error %s | %s: %s", e.get("date", ""), e.get("artist", ""), e.get("error", ""))
    if errors:
        log.warning("%d show(s) failed to publish — see errors above.", len(errors))


def cleanup_duplicate_events(dry_run: bool = True, force_delete: bool = False) -> None:
    """Report (and, when dry_run=False, trash) duplicate events — same act + same date.
    Defaults to a report that changes nothing. With dry_run=False the server keeps one
    'best' event per group and trashes the rest (force_delete permanently deletes)."""
    if not WORDPRESS_CLEANUP_DUPLICATES_URL:
        log.warning("WORDPRESS_CLEANUP_DUPLICATES_URL not set (and OUTPUT_WEBSITE_URL empty) — nothing to do.")
        return
    headers = {}
    if OUTPUT_WEBSITE_SECRET:
        headers["X-Tour-Secret"] = OUTPUT_WEBSITE_SECRET

    payload = {"dry_run": dry_run, "force_delete": force_delete}
    try:
        resp = requests.post(WORDPRESS_CLEANUP_DUPLICATES_URL, json=payload, headers=headers, timeout=120)
        resp.raise_for_status()
        result = resp.json()
    except requests.HTTPError as exc:
        body = exc.response.text[:500] if exc.response is not None else ""
        log.error("cleanup-duplicates failed (%s): %s", getattr(exc.response, "status_code", "?"), body)
        return
    except Exception as exc:
        log.error("cleanup-duplicates request failed: %s", exc)
        return

    groups = result.get("groups") or []
    mode = "DRY RUN — reporting only" if result.get("dry_run") else (
        "DELETED permanently" if result.get("force_delete") else "TRASHED")
    log.info(
        "Duplicate scan (%s): scanned %d event(s); %d duplicate group(s); %d surplus event(s); "
        "%d with no event-date.",
        mode, result.get("scanned", 0), result.get("duplicate_groups", 0),
        result.get("duplicate_events", 0), result.get("no_event_date", 0),
    )
    for g in groups:
        keep = g.get("keep") or {}
        kc = ", ".join(keep.get("categories") or []) or "no cats"
        log.info("  %s  KEEP #%s [%s] %r (img=%s body=%s; %s)",
                 g.get("date", ""), keep.get("id"), keep.get("status"), keep.get("title", ""),
                 keep.get("has_image"), keep.get("has_body"), kc)
        for t in g.get("trash") or []:
            verb = "would remove" if result.get("dry_run") else "removed"
            log.info("      %s #%s [%s] %r (img=%s body=%s)",
                     verb, t.get("id"), t.get("status"), t.get("title", ""),
                     t.get("has_image"), t.get("has_body"))
    if not result.get("dry_run"):
        log.info("Removed %d event(s).", len(result.get("trashed") or []))
    elif groups:
        log.info("Nothing changed. Re-run with apply to trash the surplus events listed above.")


def update_event_descriptions(artists: list[str], dry_run: bool = True) -> None:
    """Rewrite the body (bio) of existing events for each act from its CURRENT Drive
    description. Refreshes a bio after the act's Drive doc changes — without recreating
    events. Featured image, event meta, categories, and status are left untouched; each
    event keeps its own ticket button.

    Acts without a Drive description are skipped with a warning (never blanks a bio). When
    dry_run is True the server plans the work and writes nothing, logging which events
    would change so the operator can review before going live.
    """
    if not WORDPRESS_UPDATE_DESCRIPTIONS_URL:
        log.warning("WORDPRESS_UPDATE_DESCRIPTIONS_URL not set (and OUTPUT_WEBSITE_URL empty) — nothing to update.")
        return
    if not artists:
        log.warning("No artists given to update.")
        return

    assets = _load_drive_assets(artists)
    descriptions = {}
    for a in artists:
        desc = (assets.get(a) or {}).get("description", "")
        if desc:
            descriptions[a] = desc
        else:
            log.warning("No Drive description found for %r — skipping (bio left unchanged).", a)
    if not descriptions:
        log.warning("No Drive descriptions resolved for any requested act — nothing to update.")
        return

    headers = {}
    if OUTPUT_WEBSITE_SECRET:
        headers["X-Tour-Secret"] = OUTPUT_WEBSITE_SECRET

    payload = {"dry_run": dry_run, "descriptions": descriptions}
    result = _post_json(WORDPRESS_UPDATE_DESCRIPTIONS_URL, payload, headers)
    if result is None:
        log.error("update-descriptions request failed — see error above.")
        return

    updated = result.get("updated") or []
    skipped = result.get("skipped") or []
    errors = result.get("errors") or []
    unmatched = result.get("unmatched_artists") or []

    verb = "would update" if dry_run else "updated"
    log.info("Descriptions %s — %d event(s)%s.", verb, len(updated), " (dry run)" if dry_run else "")
    for u in updated:
        log.info("  %s #%s [%s] %s | %r", "~" if dry_run else "+",
                 u.get("id", ""), u.get("status", ""), u.get("artist", ""), u.get("title", ""))
    for s in skipped:
        log.warning("  = skipped %r (%s)", s.get("artist", ""), s.get("reason", ""))
    if unmatched:
        log.warning("  ! no events matched: %s", ", ".join(unmatched))
    for e in errors:
        log.error("  ! error #%s %s: %s", e.get("id", ""), e.get("artist", ""), e.get("error", ""))
    if errors:
        log.warning("%d event(s) failed to update — see errors above.", len(errors))


def update_event_links(shows: list[Show], dry_run: bool = True, forced_keys: set | None = None) -> None:
    """Push ticket links onto EXISTING events (incl. drafts), matched per show by act +
    date. Sets the event-link meta and the "Venue Website" button — so an event with NO
    link/button gets one added. Nothing else is touched. dry_run plans only.

    `forced_keys` is a set of Show.dedup_key() values whose links should OVERWRITE an
    event's existing (different) link — use it for corrected/broken links. Shows not in
    that set only fill events whose link is empty, leaving any existing link alone. If
    `forced_keys` is None, every show is treated as forced (overwrite). Shows without an
    http link are skipped.
    """
    if not WORDPRESS_UPDATE_LINKS_URL:
        log.warning("WORDPRESS_UPDATE_LINKS_URL not set (and OUTPUT_WEBSITE_URL empty) — skipping event-link update.")
        return
    links = [
        {
            "artist": s.artist,
            "date": s.date,
            "ticket_url": s.ticket_url,
            "force": forced_keys is None or s.dedup_key() in forced_keys,
        }
        for s in shows
        if s.ticket_url.startswith("http")
    ]
    if not links:
        log.info("No event links to update.")
        return

    headers = {}
    if OUTPUT_WEBSITE_SECRET:
        headers["X-Tour-Secret"] = OUTPUT_WEBSITE_SECRET

    result = _post_json(WORDPRESS_UPDATE_LINKS_URL, {"dry_run": dry_run, "links": links}, headers)
    if result is None:
        log.error("update-links request failed — see error above.")
        return

    updated = result.get("updated") or []
    added = result.get("added") or []
    unchanged = result.get("unchanged") or []
    kept = result.get("kept") or []
    unmatched = result.get("unmatched") or []
    errors = result.get("errors") or []

    verb = "would change" if dry_run else "changed"
    log.info("Event links %s — added=%d, replaced=%d (unchanged=%d, kept=%d)%s.",
             verb, len(added), len(updated), len(unchanged), len(kept),
             " (dry run)" if dry_run else "")
    for u in added:
        log.info("  %s ADD  #%s [%s] -> %s", "~" if dry_run else "+", u.get("id", ""), u.get("status", ""), u.get("url", ""))
    for u in updated:
        log.info("  %s REPL #%s [%s] -> %s", "~" if dry_run else "+", u.get("id", ""), u.get("status", ""), u.get("url", ""))
    if unmatched:
        log.warning("  ! %d link(s) matched no event (act+date): %s",
                    len(unmatched), ", ".join(unmatched[:10]) + (" …" if len(unmatched) > 10 else ""))
    for e in errors:
        log.error("  ! error #%s: %s", e.get("id", ""), e.get("error", ""))


def resolve_media_attachment_id(ref: str) -> int:
    """Resolve a media reference to a WordPress attachment ID. `ref` may be the numeric
    attachment ID itself, or the media file's source URL (…/wp-content/uploads/…). URLs
    are looked up read-only via the core wp/v2/media REST endpoint (searched by filename,
    then matched on source_url). Returns 0 if it can't be resolved."""
    ref = (ref or "").strip()
    if ref.isdigit():
        return int(ref)
    if not ref.startswith("http"):
        log.error("Image reference '%s' is neither an attachment ID nor a URL.", ref)
        return 0
    from config import OUTPUT_WEBSITE_URL
    base = OUTPUT_WEBSITE_URL.split("/wp-json")[0] if OUTPUT_WEBSITE_URL else ""
    if not base:
        log.error("OUTPUT_WEBSITE_URL not set — cannot resolve media URL to an attachment ID.")
        return 0
    import os as _os
    slug = _os.path.splitext(_os.path.basename(ref.split("?")[0]))[0]
    try:
        resp = requests.get(f"{base}/wp-json/wp/v2/media", params={"search": slug, "per_page": 100}, timeout=60)
        resp.raise_for_status()
        for m in resp.json():
            if str(m.get("source_url", "")).split("?")[0] == ref.split("?")[0]:
                return int(m.get("id", 0))
    except Exception as exc:
        log.error("Could not look up media '%s': %s", ref, exc)
        return 0
    log.error("No media attachment matched URL '%s'.", ref)
    return 0


def update_event_images(images: dict, dry_run: bool = True, statuses: list[str] | None = None) -> None:
    """Set the featured image on EXISTING events (incl. drafts) for one or more acts via
    /update-images. `images` maps an act's internal name to the media-library attachment ID
    to use. Matched by act (normalized title), like update_event_descriptions. Only the
    featured image is touched. dry_run plans only, reporting each event's old→new thumbnail.
    """
    from config import WORDPRESS_UPDATE_IMAGES_URL
    if not WORDPRESS_UPDATE_IMAGES_URL:
        log.warning("WORDPRESS_UPDATE_IMAGES_URL not set (and OUTPUT_WEBSITE_URL empty) — skipping event-image update.")
        return
    images = {a: int(i) for a, i in images.items() if int(i) > 0}
    if not images:
        log.info("No event images to update.")
        return

    headers = {}
    if OUTPUT_WEBSITE_SECRET:
        headers["X-Tour-Secret"] = OUTPUT_WEBSITE_SECRET

    payload = {"dry_run": dry_run, "images": images}
    if statuses:
        payload["statuses"] = statuses
    result = _post_json(WORDPRESS_UPDATE_IMAGES_URL, payload, headers)
    if result is None:
        log.error("update-images request failed — see error above.")
        return

    updated = result.get("updated") or []
    unchanged = result.get("unchanged") or []
    skipped = result.get("skipped") or []
    errors = result.get("errors") or []
    unmatched = result.get("unmatched_artists") or []

    verb = "would change" if dry_run else "changed"
    log.info("Event images %s — %d event(s) (unchanged=%d, skipped acts=%d)%s.",
             verb, len(updated), len(unchanged), len(skipped), " (dry run)" if dry_run else "")
    for u in updated:
        log.info("  %s #%s [%s] %s -> %s", "~" if dry_run else "+",
                 u.get("id", ""), u.get("status", ""), u.get("old") or "(none)", u.get("new"))
    for s in skipped:
        log.warning("  ! skipped act '%s' (%s, attachment %s)", s.get("artist", ""), s.get("reason", ""), s.get("attachment_id", ""))
    if unmatched:
        log.warning("  ! %d act(s) matched no event: %s", len(unmatched), ", ".join(unmatched))
    for e in errors:
        log.error("  ! error #%s: %s", e.get("id", ""), e.get("error", ""))


def trash_events(ids: list[int], dry_run: bool = True, force_delete: bool = False) -> None:
    """Trash specific `event` posts by ID via /trash-events — surgical cleanup the
    title/date-keyed tools can't target (duplicates, off-roster-titled events). Only posts
    of type `event` are touched; anything else is reported as skipped. dry_run plans only;
    force_delete permanently deletes instead of moving to Trash."""
    from config import WORDPRESS_TRASH_EVENTS_URL
    if not WORDPRESS_TRASH_EVENTS_URL:
        log.warning("WORDPRESS_TRASH_EVENTS_URL not set (and OUTPUT_WEBSITE_URL empty) — cannot trash events.")
        return
    ids = [int(i) for i in ids]
    if not ids:
        log.warning("No event IDs given to trash.")
        return
    headers = {}
    if OUTPUT_WEBSITE_SECRET:
        headers["X-Tour-Secret"] = OUTPUT_WEBSITE_SECRET
    result = _post_json(WORDPRESS_TRASH_EVENTS_URL, {"ids": ids, "dry_run": dry_run, "force_delete": force_delete}, headers)
    if result is None:
        log.error("trash-events request failed — see error above.")
        return
    trashed = result.get("trashed") or []
    skipped = result.get("skipped") or []
    errors = result.get("errors") or []
    verb = "would trash" if dry_run else ("deleted" if force_delete else "trashed")
    log.info("Trash-events: %s %d event(s); skipped %d; errors %d.", verb, len(trashed), len(skipped), len(errors))
    for t in trashed:
        log.info("  %s #%s [%s] %r", "~" if dry_run else "x", t.get("id", ""), t.get("status", ""), t.get("title", ""))
    for s in skipped:
        log.warning("  = skipped #%s (%s) %r", s.get("id", ""), s.get("reason", ""), s.get("title", ""))
    for e in errors:
        log.error("  ! error #%s: %s", e.get("id", ""), e.get("error", ""))


def fetch_wp_events(statuses: list[str] | None = None) -> list[dict]:
    """Return existing WP events as {id, title, status, date, link, location} via the
    read-only /list-events endpoint. [] if the endpoint isn't configured/reachable."""
    from config import WORDPRESS_LIST_EVENTS_URL
    if not WORDPRESS_LIST_EVENTS_URL:
        log.warning("WORDPRESS_LIST_EVENTS_URL not set (and OUTPUT_WEBSITE_URL empty) — cannot list events.")
        return []
    headers = {}
    if OUTPUT_WEBSITE_SECRET:
        headers["X-Tour-Secret"] = OUTPUT_WEBSITE_SECRET
    payload = {"statuses": statuses} if statuses else {}
    result = _post_json(WORDPRESS_LIST_EVENTS_URL, payload, headers)
    if result is None:
        log.error("list-events request failed — see error above.")
        return []
    events = result.get("events") or []
    log.info("WP events: %d event(s) listed.", len(events))
    return events


def _post_json(url: str, payload: dict, headers: dict) -> dict | None:
    """POST a JSON payload, retrying transient failures (mirrors _post_publish's policy).
    Returns the parsed response, or None once exhausted (logged with the server's body)."""
    for attempt in range(1, _PUBLISH_MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=120)
            resp.raise_for_status()
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status in _PUBLISH_RETRY_STATUSES and attempt < _PUBLISH_MAX_ATTEMPTS:
                log.warning("%s got %s (attempt %d/%d) — retrying…", url, status, attempt, _PUBLISH_MAX_ATTEMPTS)
                time.sleep(1.5 * attempt)
                continue
            body = exc.response.text[:2000] if exc.response is not None else ""
            log.error("Request to %s failed (%s). Server response:\n%s", url, status, body or "(empty body)")
            return None
        except Exception as exc:
            if attempt < _PUBLISH_MAX_ATTEMPTS:
                log.warning("%s error (attempt %d/%d): %s — retrying…", url, attempt, _PUBLISH_MAX_ATTEMPTS, exc)
                time.sleep(1.5 * attempt)
                continue
            log.error("Request to %s failed: %s", url, exc)
            return None
        try:
            return resp.json()
        except ValueError:
            log.error("%s returned non-JSON response: %s", url, resp.text[:500])
            return None
    return None


# ---------------------------------------------------------------------------- #
#  Google Drive asset loading                                                  #
# ---------------------------------------------------------------------------- #


def _norm(name: str) -> str:
    """Match folder names to artist names leniently (lowercase, alnum only)."""
    return "".join(c for c in name.lower() if c.isalnum())


def _is_description_file(f: dict) -> bool:
    """True for any supported description asset: txt, md, rtf, doc, docx, Google Doc."""
    name = str(f.get("name", "")).lower()
    mime = str(f.get("mimeType", ""))
    return mime in _DESCRIPTION_MIMES or name.endswith(_DESCRIPTION_EXTENSIONS)


def _lines_to_paragraphs(text: str) -> str:
    """One line per paragraph -> blank-line-separated paragraphs.

    Used for formats whose extractors emit a single line per paragraph (legacy
    Office via textutil, Google Docs export). The PHP cleanup treats blank lines
    as paragraph breaks and joins single newlines, so we hand it that contract.
    Plain .txt is deliberately NOT run through this — it may be hard-wrapped, and
    the PHP cleanup unwraps it correctly on its own.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in text.split("\n")]
    return "\n\n".join(ln for ln in lines if ln)


# Sentence-terminating punctuation, allowing trailing quotes/brackets (… counts too).
_SENTENCE_END_RE = re.compile(r"[.!?…][\"'”’)\]]*$")


def _ends_sentence(text: str) -> bool:
    return bool(_SENTENCE_END_RE.search(text.rstrip()))


def _docx_to_text(data: bytes) -> str:
    """Extract paragraph text from a .docx (Office Open XML) using only the stdlib.

    Real paragraph breaks in these bios are marked by an *empty* paragraph; some
    docs also press Enter mid-sentence for visual line-wrapping, which shows up as
    consecutive non-empty paragraphs with no empty one between them. Treat that
    case as a soft wrap and rejoin with a space (only when the previous paragraph
    didn't already end a sentence), so wrapped lines like "...with this" + "level
    of detail." don't become two broken paragraphs. Adjacent paragraphs that each
    end a sentence stay separate.
    """
    import zipfile
    from xml.etree import ElementTree as ET

    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml = zf.read("word/document.xml")
        root = ET.fromstring(xml)
    except Exception as exc:
        log.warning("Could not parse .docx: %s", exc)
        return ""

    paragraphs: list[str] = []
    prev_empty = True  # start of doc behaves like a paragraph boundary
    for p in root.iter(f"{ns}p"):
        parts = []
        for node in p.iter():
            if node.tag == f"{ns}t":
                parts.append(node.text or "")
            elif node.tag == f"{ns}tab":
                parts.append("\t")
            elif node.tag in (f"{ns}br", f"{ns}cr"):
                parts.append(" ")
        line = "".join(parts).strip()
        if not line:
            prev_empty = True
            continue
        if not prev_empty and paragraphs and not _ends_sentence(paragraphs[-1]):
            paragraphs[-1] = f"{paragraphs[-1]} {line}"
        else:
            paragraphs.append(line)
        prev_empty = False
    return "\n\n".join(paragraphs)


def _pdf_to_text_plumber(data: bytes) -> str:
    """Extract via pdfplumber (pdfminer backend) — far better word spacing on laid-out
    EPK/bio PDFs than pypdf. '' if pdfplumber is unavailable or can't parse the file."""
    try:
        import pdfplumber  # type: ignore
    except ImportError:
        return ""
    # pdfminer logs noisy per-glyph FontBBox warnings on PDFs with malformed font
    # descriptors; they don't affect extraction, so quiet them.
    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [(pg.extract_text() or "").strip() for pg in pdf.pages]
    except Exception as exc:
        log.warning("pdfplumber could not parse PDF (%s) — falling back to pypdf.", exc)
        return ""
    return "\n\n".join(p for p in pages if p)


def _pdf_to_text_pypdf(data: bytes) -> str:
    """Fallback extractor when pdfplumber isn't installed or fails."""
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        log.warning("Cannot read PDF description — neither pdfplumber nor pypdf installed.")
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except Exception as exc:
        log.warning("Could not parse PDF description: %s", exc)
        return ""
    return "\n\n".join(p for p in pages if p)


def _pdf_to_text(data: bytes) -> str:
    """Extract text from a PDF (pdfplumber preferred, pypdf fallback). '' if neither can
    read it or it has no extractable text (e.g. a scanned/image-only PDF, which needs OCR).

    Both backends emit a newline per visual line, so the result is hard-wrapped like a
    .txt — strip trailing spaces and collapse blank-line runs, then let the PHP cleanup
    unwrap the remaining soft breaks into paragraphs.
    """
    text = _pdf_to_text_plumber(data) or _pdf_to_text_pypdf(data)
    if not text:
        return ""
    text = re.sub(r"[ \t]+\n", "\n", text)   # trailing spaces -> bare newline
    text = re.sub(r"\n{3,}", "\n\n", text)   # collapse blank-line spam
    return text.strip()


def _textutil_to_text(data: bytes, suffix: str) -> str:
    """Convert a legacy .doc / .rtf to text via macOS `textutil`. '' (with a warning)
    if textutil is unavailable (non-macOS) or conversion fails."""
    import shutil
    import subprocess
    import tempfile

    if not shutil.which("textutil"):
        log.warning("Cannot extract %s — 'textutil' (macOS) not found; convert it to .docx or .txt.", suffix)
        return ""
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
            tf.write(data)
            tmp_path = tf.name
        proc = subprocess.run(
            ["textutil", "-convert", "txt", "-stdout", tmp_path],
            capture_output=True, timeout=30,
        )
        if proc.returncode != 0:
            log.warning("textutil failed for %s: %s", suffix, proc.stderr.decode("utf-8", "replace")[:200])
            return ""
        return _lines_to_paragraphs(proc.stdout.decode("utf-8", "replace"))
    except Exception as exc:
        log.warning("textutil error for %s: %s", suffix, exc)
        return ""
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _extract_description(data: bytes, name: str, mime: str) -> str:
    """Turn raw asset bytes into plain text based on type. '' if unsupported/empty.

    Google Docs are handled by the caller (exported to text/plain via Drive), not
    here. Output is normalized so the PHP cleanup sees blank-line-separated
    paragraphs, except plain .txt/.md which pass through raw for the cleanup to
    unwrap (it may be hard-wrapped).
    """
    name_l = str(name).lower()
    if mime == _DOCX_MIME or name_l.endswith(".docx"):
        return _docx_to_text(data) or _textutil_to_text(data, ".docx")
    if mime == _PDF_MIME or name_l.endswith(".pdf"):
        return _pdf_to_text(data)
    if mime == _DOC_MIME or name_l.endswith(".doc"):
        return _textutil_to_text(data, ".doc")
    if mime in _RTF_MIMES or name_l.endswith(".rtf"):
        return _textutil_to_text(data, ".rtf")
    # text/plain, markdown, or anything else small enough to be text.
    return data.decode("utf-8", errors="replace")


# Featured images ride in the publish payload as inline base64, which a ~1MB server
# request-body limit caps (over it the request 404s with rest_no_route). Downscale
# each to a web-appropriate size so even a chunk carrying several artists stays well
# under that limit — and the site loads faster. Falls back to the original bytes if
# Pillow is missing or the data isn't a decodable image.
_IMAGE_MAX_DIM = 1280                 # px, longest side
_IMAGE_TARGET_BYTES = 220 * 1024      # step quality down until at/under this
_IMAGE_QUALITY_STEPS = (85, 80, 75, 70)


def _downscale_image(raw: bytes, filename: str) -> tuple[bytes, str]:
    """Return (jpeg_bytes, filename.jpg) shrunk to web size, or (raw, filename) if it
    can't be processed. Never upscales; flattens alpha onto white for clean JPEG."""
    try:
        from PIL import Image, ImageOps  # type: ignore
    except Exception:
        return raw, filename
    try:
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img)  # honour camera orientation before resize
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
            img = Image.alpha_composite(bg, img).convert("RGB")
        elif img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((_IMAGE_MAX_DIM, _IMAGE_MAX_DIM), Image.LANCZOS)
        best = raw
        for q in _IMAGE_QUALITY_STEPS:
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=q, optimize=True, progressive=True)
            best = out.getvalue()
            if len(best) <= _IMAGE_TARGET_BYTES:
                break
        stem = filename.rsplit(".", 1)[0] if "." in filename else filename
        return best, f"{stem}.jpg"
    except Exception as exc:
        log.warning("Could not downscale image '%s' (%s) — sending original.", filename, exc)
        return raw, filename


def _load_drive_assets(artists: list[str]) -> dict:
    """
    For each act, return {artist: {image_b64, image_filename, description}} pulled
    from a per-act subfolder of WORDPRESS_ASSETS_DRIVE_FOLDER_ID. Acts without a
    folder (or without assets) are simply omitted — the server then falls back to
    an existing event of the same act, or creates the event without them.
    """
    if not WORDPRESS_ASSETS_DRIVE_FOLDER_ID:
        log.info("WORDPRESS_ASSETS_DRIVE_FOLDER_ID not set — skipping Drive assets (server will use existing events as templates).")
        return {}

    try:
        from googleapiclient.discovery import build  # type: ignore
        from googleapiclient.http import MediaIoBaseDownload  # type: ignore
        from google.oauth2 import service_account  # type: ignore
    except ImportError:
        log.warning("google-api-python-client not installed — skipping Drive assets.")
        return {}

    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if not creds_path:
        log.warning("GOOGLE_APPLICATION_CREDENTIALS not set — skipping Drive assets.")
        return {}

    list_kwargs = {
        "supportsAllDrives": True,
        "includeItemsFromAllDrives": True,
        "pageSize": 1000,
    }

    # Build the client and list the act subfolders. Drive assets are an optional
    # fallback, so any failure here (API disabled, folder not shared, auth) must
    # degrade gracefully — the server can still template off existing events.
    try:
        scopes = ["https://www.googleapis.com/auth/drive.readonly"]
        creds = service_account.Credentials.from_service_account_file(creds_path, scopes=scopes)
        service = build("drive", "v3", credentials=creds)

        # Map every act subfolder by normalized name so internal artist names that
        # differ slightly from folder names still match.
        folders = service.files().list(
            q=f"'{WORDPRESS_ASSETS_DRIVE_FOLDER_ID}' in parents "
              "and mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields="files(id,name)",
            **list_kwargs,
        ).execute().get("files", [])
    except Exception as exc:
        log.warning("Could not access Google Drive assets (%s) — continuing without them.", exc)
        return {}
    folder_by_norm = {_norm(f["name"]): f for f in folders}

    def _download(file_id: str, export_mime: str | None = None) -> bytes:
        # Google-native files (Docs) must be exported; uploaded files use get_media.
        if export_mime:
            request = service.files().export_media(fileId=file_id, mimeType=export_mime)
        else:
            request = service.files().get_media(fileId=file_id)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _status, done = downloader.next_chunk()
        return buf.getvalue()

    assets: dict = {}
    for artist in artists:
        folder = folder_by_norm.get(_norm(artist))
        if not folder:
            continue

        try:
            files = service.files().list(
                q=f"'{folder['id']}' in parents and trashed=false",
                fields="files(id,name,mimeType)",
                **list_kwargs,
            ).execute().get("files", [])
        except Exception as exc:
            log.warning("Could not list Drive folder for %s: %s", artist, exc)
            continue

        image = next((f for f in files if str(f.get("mimeType", "")).startswith(_IMAGE_MIME_PREFIX)), None)
        # Each artist folder holds just two files — an image and a document — so take
        # the first supported document, whatever it's named (txt, md, rtf, doc, docx,
        # Google Doc).
        desc = next((f for f in files if _is_description_file(f)), None)

        entry: dict = {}
        if image:
            try:
                raw, fname = _downscale_image(_download(image["id"]), image["name"])
                entry["image_b64"] = base64.b64encode(raw).decode("ascii")
                entry["image_filename"] = fname
            except Exception as exc:
                log.warning("Could not download image for %s: %s", artist, exc)
        if desc:
            try:
                dmime = str(desc.get("mimeType", ""))
                dname = str(desc.get("name", ""))
                if dmime == _GOOGLE_DOC_MIME:
                    text = _lines_to_paragraphs(_download(desc["id"], export_mime="text/plain").decode("utf-8", errors="replace"))
                else:
                    text = _extract_description(_download(desc["id"]), dname, dmime)
                text = (text or "").strip()
                if text:
                    # Authored in Markdown — render emphasis to HTML before it reaches the
                    # plugin, which would otherwise publish the asterisks literally.
                    entry["description"] = _markdown_emphasis_to_html(text)
                else:
                    log.warning("Description file '%s' for %s produced no text.", dname, artist)
            except Exception as exc:
                log.warning("Could not read description for %s: %s", artist, exc)

        if entry:
            assets[artist] = entry
            log.info("Drive assets for %s: image=%s description=%s",
                     artist, "yes" if "image_b64" in entry else "no", "yes" if "description" in entry else "no")

    if not assets:
        log.info("No Drive assets matched any act (server will rely on existing events as templates).")
    return assets
