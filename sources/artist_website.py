import json
import logging
import re
from urllib.parse import urljoin
from datetime import date as _date

import requests
import anthropic

import claude_state
from config import (
    ARTIST_WEBSITES,
    ANTHROPIC_API_KEY,
    CLAUDE_MODEL,
    CLAUDE_MAX_TOKENS,
    _key_set,
)
from models import Show

log = logging.getLogger(__name__)


def fetch_artist_website(artist: str) -> list[Show]:
    """Scrape the artist's tour page and use Claude to parse dates and ticket links."""
    url = ARTIST_WEBSITES.get(artist, "")
    if not url:
        return []
    if not _key_set(ANTHROPIC_API_KEY):
        return []

    try:
        from bs4 import BeautifulSoup  # type: ignore

        page_resp = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        })
        page_resp.raise_for_status()
        soup = BeautifulSoup(page_resp.text, "html.parser")

        # Replace <a href="..."> with "link text (full_url)" so Claude sees actual URLs
        for a in soup.find_all("a", href=True):
            full_href = urljoin(url, a["href"])
            link_text = a.get_text(strip=True)
            a.replace_with(f"{link_text} ({full_href})" if link_text else full_href)

        page_text = soup.get_text(separator="\n")
        page_text = re.sub(r"\n{3,}", "\n\n", page_text).strip()[:32000]
    except Exception as exc:
        log.error("Artist website fetch error for %s: %s", artist, exc)
        return []

    # Skip if the page appears to be JS-rendered with no useful content
    if len(page_text.strip()) < 200:
        log.warning("Artist website for %s appears JS-rendered or empty, skipping", artist)
        return []

    today = _date.today().isoformat()
    prompt = (
        f"Extract all upcoming show dates for '{artist}' from the following text scraped from their official tour page. "
        f"Only include shows on or after {today}. "
        "Return ONLY a JSON array with these exact keys: date (YYYY-MM-DD), venue, city, region, country, ticket_url. "
        "For ticket_url: use the full URL (must start with 'http') found next to the show listing. "
        "NEVER use link text like 'Buy Tickets', 'Buy Now', or 'Tickets' as the ticket_url value — only use actual URLs. "
        "Use an empty string for ticket_url if no real URL is found for that show. "
        "Do not include any text outside the JSON array.\n\n"
        f"{page_text}"
    )

    claude_state._claude_throttle()
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        raw = client.messages.with_raw_response.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        resp_msg = raw.parse()
        claude_state._claude_call_count += 1
        claude_state._claude_call_done(dict(raw.headers))
        claude_state._track_cost(resp_msg)
    except Exception as exc:
        log.error("Artist website Claude parse error for %s: %s", artist, exc)
        return []

    text = ""
    for block in resp_msg.content:
        if hasattr(block, "text"):
            text += block.text

    text = re.sub(r"```(?:json)?\s*", "", text)
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        log.error(
            "Artist website parse error for %s: no JSON array found\nRaw: %s",
            artist,
            text[:500],
        )
        return []
    try:
        events = json.loads(match.group())
    except json.JSONDecodeError as exc:
        log.error("Artist website JSON error for %s: %s", artist, exc)
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
                source="artist_website",
            )
        )
    log.info("Artist website: %d shows for %s", len(shows), artist)
    return shows
