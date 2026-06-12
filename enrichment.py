import json
import logging
import re

import anthropic

import claude_state
from config import (
    ANTHROPIC_API_KEY,
    CLAUDE_MODEL,
    CLAUDE_MAX_TOKENS,
    CLAUDE_CALL_LIMIT,
    _key_set,
    _is_platform_url,
)
from models import Show
from sources.ticket_page import verify_ticket_links, fetch_page_text, page_confirms_event

log = logging.getLogger(__name__)


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

    text = re.sub(r"```(?:json)?\s*", "", text)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        log.error(
            "Claude enrichment parse error for %s: no JSON object found\nRaw: %s",
            artist,
            text[:1000],
        )
        return

    try:
        url_map: dict[str, str] = json.loads(match.group())
    except json.JSONDecodeError as exc:
        log.error("Claude enrichment JSON error for %s: %s", artist, exc)
        return

    for idx_str, url in url_map.items():
        try:
            i = int(idx_str)
            show = to_enrich[i]
        except (ValueError, IndexError):
            continue
        if url and url.startswith("http") and not _is_platform_url(url):
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

    text = re.sub(r"```(?:json)?\s*", "", text)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        log.error("Batch enrichment parse error: no JSON object found\nRaw: %s", text[:1000])
        return

    try:
        url_map: dict[str, str] = json.loads(match.group())
    except json.JSONDecodeError as exc:
        log.error("Batch enrichment JSON error: %s", exc)
        return

    found = 0
    for idx_str, url in url_map.items():
        try:
            i = int(idx_str)
            show = to_enrich[i]
        except (ValueError, IndexError):
            continue
        if url and url.startswith("http") and not _is_platform_url(url):
            show.ticket_url = url
            found += 1
    log.info("Batch enrichment: %d venue-direct URLs found across %d shows", found, len(to_enrich))


_VERIFY_CHUNK = 35  # shows per web_search call — keep the JSON map within output token limits


def find_event_ticket_urls(failed: list[Show]) -> dict[int, str]:
    """Batched Claude web_search for event-specific ticket pages.

    Returns {index_into_failed: url}. Chunked to keep each JSON response small; honors
    CLAUDE_CALL_LIMIT and the cost cap. No per-show calls — one web_search per chunk.
    """
    out: dict[int, str] = {}
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
        text = re.sub(r"```(?:json)?\s*", "", text)
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            continue
        try:
            url_map: dict[str, str] = json.loads(match.group())
        except json.JSONDecodeError:
            continue
        for k, v in url_map.items():
            try:
                idx = int(k)
            except (ValueError, TypeError):
                continue
            if isinstance(v, str) and v.startswith("http"):
                out[idx] = v
    return out


def verify_and_fix_ticket_links(shows: list[Show]) -> list[Show]:
    """Verify each show's ticket link by page content; AI-search + re-verify the failures.

    Mutates `shows` in place and returns the shows whose ticket_url was changed. The AI step
    is batched; an AI-found URL is only adopted if its page also confirms the act + date.
    """
    verified, failed = verify_ticket_links(shows)
    if not failed:
        log.info("Link QA: all %d link(s) verified.", len(verified))
        return []

    url_map = find_event_ticket_urls(failed)
    corrected: list[Show] = []
    for idx, url in url_map.items():
        if idx >= len(failed):
            continue
        show = failed[idx]
        if url == show.ticket_url:
            continue
        # render=True: AI links are often JS-rendered ticketing pages; render to confirm.
        if page_confirms_event(fetch_page_text(url, render=True), show.artist, show.date, show.start_time):
            show.ticket_url = url
            corrected.append(show)

    log.info(
        "Link QA: verified=%d, replaced=%d, unresolved=%d",
        len(verified), len(corrected), len(failed) - len(corrected),
    )
    return corrected
