import logging
from datetime import datetime as _dt

import anthropic

import claude_state
from config import (
    extract_json,
    ANTHROPIC_API_KEY,
    CLAUDE_MODEL,
    CLAUDE_MAX_TOKENS,
    CLAUDE_CALL_LIMIT,
    _key_set,
    _is_platform_url,
    _is_non_ticket_url,
    _is_bare_homepage,
    _acceptable_venue_result,
    _display_name,
)
from models import Show
from sources.ticket_page import verify_ticket_links, fetch_page_text, page_confirms_event, url_event_slug_ok
from sources.web_search_ddg import ddg_search
from sources.deep_crawl import dig_for_event, deepen_to_specific

log = logging.getLogger(__name__)


def _should_adopt_enrichment_url(url: str, show: Show) -> bool:
    """Decide whether a Claude-suggested ticket URL should replace `show.ticket_url`.

    Enrichment runs on every show whose current link is a platform URL — which includes the
    stable Bandsintown *event page* (`bandsintown.com/e/<id>`) we store for Bandsintown-sourced
    shows. That event page (act + date + venue + a ticket button) is more useful than a bare
    venue homepage, so when a link already exists we only replace it with an EVENT-SPECIFIC
    venue page: reject platform URLs, non-ticket sections (rooms/dining), off-venue pages (the
    act's own EPK, blogs, socials — caught by `_acceptable_venue_result`), and bare homepages.
    When the show has NO link at all, any non-platform link beats nothing, so accept it."""
    if not url or not url.startswith("http") or _is_platform_url(url):
        return False
    if not show.ticket_url:
        return True  # no existing link — even a homepage is better than nothing
    if _is_non_ticket_url(url) or _is_bare_homepage(url):
        return False
    return _acceptable_venue_result(url, show.venue)


def enrich_ticket_urls_for_artist(shows: list[Show], fallbacks: dict[str, str]) -> None:
    """
    Find venue-direct ticket URLs for all of an artist's shows in one Claude call.
    Mutates shows in place. Falls back to platform URLs if Claude can't find venue-direct links.
    """
    # Apply fallbacks first; we'll overwrite with venue-direct URLs where Claude finds them
    for show in shows:
        if not show.ticket_url:
            show.ticket_url = fallbacks.get(show.dedup_key(), "")

    # Filter to shows that still need enrichment (missing or platform URL)
    to_enrich = [s for s in shows if not s.ticket_url or _is_platform_url(s.ticket_url)]
    if not to_enrich:
        return

    if claude_state._claude_call_count >= CLAUDE_CALL_LIMIT:
        return
    if not _key_set(ANTHROPIC_API_KEY):
        return

    show_lines = "\n".join(
        f"{i}: {s.venue}, {s.city} — {s.date}" for i, s in enumerate(to_enrich)
    )
    artist = to_enrich[0].artist
    prompt = (
        f"For the artist '{artist}', find the direct ticket purchase URL from the venue's own website "
        f"for each of the following shows. Do NOT return Ticketmaster, LiveNation, AXS, Eventbrite, "
        f"or SeatGeek links. Return ONLY a JSON object mapping each index to a URL string "
        f"(empty string if not found). No other text.\n\n{show_lines}"
    )

    claude_state._claude_throttle()
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        raw = client.messages.with_raw_response.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}],
        )
        resp = raw.parse()
        claude_state._claude_call_count += 1
        claude_state._claude_call_done(dict(raw.headers))
        claude_state._track_cost(resp)
    except Exception as exc:
        log.error("Claude ticket enrichment error for %s: %s", artist, exc)
        return

    text = ""
    for block in resp.content:
        if hasattr(block, "text"):
            text += block.text

    url_map = extract_json(text, "{")
    if not isinstance(url_map, dict):
        log.error(
            "Claude enrichment parse error for %s: no JSON object found\nRaw: %s",
            artist,
            text[:1000],
        )
        return

    for idx_str, url in url_map.items():
        try:
            i = int(idx_str)
            show = to_enrich[i]
        except (ValueError, IndexError):
            continue
        if _should_adopt_enrichment_url(url, show):
            log.info("Venue-direct URL found for %s on %s: %s", artist, show.date, url)
            show.ticket_url = url


def enrich_ticket_urls_all(shows: list[Show]) -> None:
    """
    ONE Claude web-search call to find venue-direct ticket URLs across ALL artists.
    Replaces 12 per-artist calls in full runs, reducing web searches from ~36 to ~5.
    Mutates shows in place.
    """
    if not _key_set(ANTHROPIC_API_KEY) or not claude_state._under_cost_cap("enrich_all"):
        return

    to_enrich = [s for s in shows if not s.ticket_url or _is_platform_url(s.ticket_url)]
    if not to_enrich:
        log.info("Batch enrichment: all shows already have venue-direct URLs")
        return

    show_lines = "\n".join(
        f"{i}: [{s.artist}] {s.venue}, {s.city} — {s.date}"
        for i, s in enumerate(to_enrich)
    )
    prompt = (
        "For each show below, find the direct ticket purchase URL from the VENUE'S OWN website. "
        "Do NOT return Ticketmaster, LiveNation, AXS, Eventbrite, or SeatGeek links. "
        "Prioritize venues that appear multiple times — they are worth a dedicated search. "
        "Skip one-off venues if you are running low on searches. "
        "Return ONLY a JSON object mapping each index number to a URL string "
        "(empty string if not found). No other text.\n\n"
        + show_lines
    )

    claude_state._claude_throttle()
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        raw = client.messages.with_raw_response.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}],
        )
        resp = raw.parse()
        claude_state._claude_call_count += 1
        claude_state._claude_call_done(dict(raw.headers))
        claude_state._track_cost(resp)
    except Exception as exc:
        log.error("Batch ticket enrichment error: %s", exc)
        return

    text = ""
    for block in resp.content:
        if hasattr(block, "text"):
            text += block.text

    url_map = extract_json(text, "{")
    if not isinstance(url_map, dict):
        log.error("Batch enrichment parse error: no JSON object found\nRaw: %s", text[:1000])
        return

    found = 0
    for idx_str, url in url_map.items():
        try:
            i = int(idx_str)
            show = to_enrich[i]
        except (ValueError, IndexError):
            continue
        if _should_adopt_enrichment_url(url, show):
            show.ticket_url = url
            found += 1
    log.info("Batch enrichment: %d venue-direct URLs found across %d shows", found, len(to_enrich))


_VERIFY_CHUNK = 35  # shows per web_search call — keep the JSON map within output token limits


def find_event_ticket_urls(failed: list[Show]) -> dict[int, list[str]]:
    """Batched Claude web_search for event-specific ticket pages.

    Returns {index_into_failed: [url]} (a single best candidate per show, wrapped in a
    list to match the finder contract used by verify_and_fix_ticket_links). Chunked to
    keep each JSON response small; honors CLAUDE_CALL_LIMIT and the cost cap. No per-show
    calls — one web_search per chunk.
    """
    out: dict[int, list[str]] = {}
    if not failed or not _key_set(ANTHROPIC_API_KEY):
        return out

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    for start in range(0, len(failed), _VERIFY_CHUNK):
        if claude_state._claude_call_count >= CLAUDE_CALL_LIMIT:
            log.warning("find_event_ticket_urls: hit CLAUDE_CALL_LIMIT — stopping.")
            break
        if not claude_state._under_cost_cap("verify_links"):
            break
        chunk = failed[start:start + _VERIFY_CHUNK]
        lines = "\n".join(
            f"{start + i}: [{s.artist}] {s.venue}, {s.city} — {s.date}"
            + (f" {s.start_time}" if s.start_time else "")
            for i, s in enumerate(chunk)
        )
        prompt = (
            "Find the official ticket page for EACH specific show below — the page for that "
            "exact act on that exact date (an event/performance page, NOT a venue homepage or "
            "a generic 'upcoming shows' listing). Prefer the venue's own ticketing page. "
            "Return ONLY a JSON object mapping each index number to a URL string (empty string "
            "if you can't find a confident match). No other text.\n\n" + lines
        )

        claude_state._claude_throttle()
        try:
            raw = client.messages.with_raw_response.create(
                model=CLAUDE_MODEL,
                max_tokens=CLAUDE_MAX_TOKENS,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": prompt}],
            )
            resp = raw.parse()
            claude_state._claude_call_count += 1
            claude_state._claude_call_done(dict(raw.headers))
            claude_state._track_cost(resp)
        except Exception as exc:
            log.error("find_event_ticket_urls web_search error: %s", exc)
            continue

        text = "".join(b.text for b in resp.content if hasattr(b, "text"))
        url_map = extract_json(text, "{")
        if not isinstance(url_map, dict):
            continue
        for k, v in url_map.items():
            try:
                idx = int(k)
            except (ValueError, TypeError):
                continue
            if isinstance(v, str) and v.startswith("http"):
                out[idx] = [v]
    return out


_SEARCH_MAX_RESULTS = 8


def _search_query(show: Show) -> str:
    """Build a DuckDuckGo query for a show: '<act> <venue> <city> <Month D, YYYY> tickets'."""
    try:
        human_date = _dt.fromisoformat(show.date).strftime("%B %-d, %Y")
    except ValueError:
        human_date = show.date
    parts = [_display_name(show.artist), show.venue, show.city, human_date, "tickets"]
    return " ".join(p for p in parts if p)


def find_event_ticket_urls_via_search(failed: list[Show]) -> dict[int, list[str]]:
    """Programmatic (no-AI) counterpart to find_event_ticket_urls.

    For each failed show, run a DuckDuckGo search and return the candidate result URLs
    ({index_into_failed: [url, ...]}). The caller confirms each candidate against the
    page (page_confirms_event), so no AI is needed to pick the right one.
    """
    out: dict[int, list[str]] = {}
    for idx, show in enumerate(failed):
        urls = ddg_search(_search_query(show), max_results=_SEARCH_MAX_RESULTS)
        if urls:
            out[idx] = urls
    return out


def _pick_confirmed_url(candidates: list[str], show: Show) -> str:
    """Return the best candidate whose page confirms the act + date, preferring a
    venue-direct URL over a platform one. '' if none confirm. Search results must also
    pass _acceptable_venue_result so off-venue aggregator/social/blog pages are rejected."""
    platform_fallback = ""
    for url in candidates:
        if not url.startswith("http") or url == show.ticket_url or _is_non_ticket_url(url):
            continue
        if not _acceptable_venue_result(url, show.venue):
            continue
        if not url_event_slug_ok(url, show.artist, show.date):
            continue
        # render=True only spins Playwright when the static text is too thin, so normal
        # pages stay cheap while JS-rendered ticketing pages still get confirmed.
        if page_confirms_event(fetch_page_text(url, render=True), show.artist, show.date, show.start_time):
            if not _is_platform_url(url):
                # Drill a listing/series result down to this show's own page when possible.
                return deepen_to_specific(url, show)
            platform_fallback = platform_fallback or url
    return platform_fallback


def verify_fix_and_classify(shows: list[Show], finder=find_event_ticket_urls) -> dict[str, list[Show]]:
    """Verify each show's ticket link by page content, fix the failures, and classify.

    Mutates `shows` in place. Returns {"corrected", "good", "unresolved"}:
      - corrected:  shows whose ticket_url was changed (fixed).
      - good:       shows that now have a confirmed-good link (verified + rescued + corrected).
      - unresolved: shows still without a confirmed link.
    `finder` locates replacement candidates for the failures and is pluggable: the default
    uses Claude web search (find_event_ticket_urls); pass find_event_ticket_urls_via_search
    for the no-AI DuckDuckGo path. A candidate is adopted only if its page confirms act+date.
    """
    verified, failed = verify_ticket_links(shows)

    # verify_ticket_links fetches statically, so JS-rendered ticket pages can fail even
    # when correct. Re-confirm failures with a full render before searching, so a valid
    # link is rescued instead of needlessly replaced.
    rescued: list[Show] = []
    still_failed: list[Show] = []
    for show in failed:
        if show.ticket_url.startswith("http") and page_confirms_event(
            fetch_page_text(show.ticket_url, render=True), show.artist, show.date, show.start_time
        ):
            rescued.append(show)
        else:
            still_failed.append(show)

    corrected: list[Show] = []
    unresolved: list[Show] = []

    # Dig into the existing venue link first (no AI): follow on-site Events/Calendar links
    # and, if needed, drive a headless browser through a JS calendar to the show's date.
    # The current link is the best lead, so exhaust it before falling back to search.
    search_failed: list[Show] = []
    for show in still_failed:
        deep = dig_for_event(show.ticket_url, show)
        if deep and deep != show.ticket_url:
            show.ticket_url = deep
            corrected.append(show)
        else:
            search_failed.append(show)
    dug = len(corrected)

    # Whatever digging couldn't resolve goes to the finder (web search / Claude), and each
    # candidate is adopted only if its page also confirms the act + date.
    if search_failed:
        cand_map = finder(search_failed)
        fixed_idx: set[int] = set()
        for idx, candidates in cand_map.items():
            if idx >= len(search_failed):
                continue
            show = search_failed[idx]
            best = _pick_confirmed_url(candidates, show)
            if best and best != show.ticket_url:
                show.ticket_url = best
                corrected.append(show)
                fixed_idx.add(idx)
        unresolved = [s for i, s in enumerate(search_failed) if i not in fixed_idx]
    else:
        unresolved = []

    log.info(
        "Link QA: verified=%d (incl. %d via render), dug=%d, replaced-via-search=%d, unresolved=%d",
        len(verified) + len(rescued), len(rescued), dug, len(corrected) - dug, len(unresolved),
    )
    return {"corrected": corrected, "good": verified + rescued + corrected, "unresolved": unresolved}


def verify_and_fix_ticket_links(shows: list[Show], finder=find_event_ticket_urls) -> list[Show]:
    """Verify each show's ticket link and fix failures; return the shows whose url changed.
    Thin wrapper over verify_fix_and_classify for callers that only need the corrections."""
    return verify_fix_and_classify(shows, finder)["corrected"]
