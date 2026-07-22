import logging
from datetime import date as _date

import anthropic

import claude_state
from config import (
    extract_json,
    ARTIST_WEBSITES,
    ANTHROPIC_API_KEY,
    CLAUDE_MODEL,
    CLAUDE_MAX_TOKENS,
    CLAUDE_CALL_LIMIT,
    _key_set,
)
from models import Show

log = logging.getLogger(__name__)


def fetch_claude_web_search(artist: str) -> list[Show]:
    """Use Claude with web_search tool to find tour dates, including artist website."""
    if claude_state._claude_call_count >= CLAUDE_CALL_LIMIT:
        log.warning(
            "Claude call limit reached (%d), skipping web search for %s",
            CLAUDE_CALL_LIMIT,
            artist,
        )
        return []
    if not _key_set(ANTHROPIC_API_KEY):
        log.warning("ANTHROPIC_API_KEY not set, skipping Claude web search")
        return []

    artist_site = ARTIST_WEBSITES.get(artist, "")
    site_hint = (
        f" Check the official artist website first: {artist_site}."
        if artist_site
        else ""
    )
    today = _date.today().isoformat()

    prompt = (
        f"Find all upcoming tour/show dates for '{artist}' on or after {today}.{site_hint} "
        f"Note: '{artist}' is a live tribute/show act, NOT the original artist. Search specifically for this show. "
        "Do 1-2 targeted searches, then immediately output your answer as JSON. "
        "Return ONLY a JSON array of objects using standard JSON syntax — curly braces { and } for objects, square brackets for the array. "
        "Each object must have exactly these keys: date (YYYY-MM-DD), start_time, venue, city, region, country, ticket_url. "
        "For start_time: the show's start time as 'HH:MM' in 24-hour format if you find it; use an empty string if the time is unknown. "
        "For venue use the theater/arena/venue name (e.g. 'Renfro Valley Entertainment Center'), NOT a street address. "
        "If ticket_url is unknown use an empty string. "
        "Do not use markdown, asterisks, or any non-JSON formatting. Do not include any text outside the JSON array."
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
        log.error("Claude web search error for %s: %s", artist, exc)
        return []

    text = ""
    for block in resp.content:
        if hasattr(block, "text"):
            text += block.text
        elif hasattr(block, "type") and block.type == "tool_result":
            pass  # skip raw search result blocks

    events = extract_json(text, "[")
    if not isinstance(events, list):
        log.error(
            "Claude JSON parse error for %s: no JSON array found\nRaw: %s",
            artist,
            text[:500],
        )
        return []

    shows = []
    for ev in events:
        shows.append(
            Show(
                artist=artist,
                date=ev.get("date", ""),
                venue=ev.get("venue", ""),
                city=ev.get("city", ""),
                region=ev.get("region", ""),
                country=ev.get("country", ""),
                ticket_url=ev.get("ticket_url", ""),
                source="claude_web_search",
                start_time=str(ev.get("start_time", "") or ""),
            )
        )
    log.info("Claude web search: %d shows for %s", len(shows), artist)
    return shows
