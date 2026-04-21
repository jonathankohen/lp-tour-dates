"""
Tour Date Automation Tool
Aggregates tour dates from multiple sources and publishes to output destinations.
"""

import os
import json
import logging
import hashlib
import time
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass, field, asdict

import requests
import anthropic
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SEATGEEK_CLIENT_ID = os.environ.get("SEATGEEK_CLIENT_ID", "")
TICKETMASTER_API_KEY = os.environ.get("TICKETMASTER_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


def _key_set(val: str) -> bool:
    """Return True only if val is a real key (not empty or a placeholder)."""
    return bool(val) and val != "pending_approval"


_PLATFORM_DOMAINS = (
    "ticketmaster.",  # matches ticketmaster.com, ticketmaster.ie, ticketmaster.co.uk, etc.
    "livenation.com",
    "axs.com",
    "eventbrite.com",
    "seatgeek.com",
    "bandsintown.com",
)


def _is_platform_url(url: str) -> bool:
    return any(d in url for d in _PLATFORM_DOMAINS)


CLAUDE_MODEL = "claude-haiku-4-5"
CLAUDE_MAX_TOKENS = 4096  # per call — needs room for full JSON list of tour dates
CLAUDE_CALL_LIMIT = 50  # max Claude calls per run (token/cost cap)

BAND_NAMES: list[str] = [
    "Arrival From Sweden: The Music of ABBA",
    "The Dolly Show",
    "Kyle Martin's Piano Man",
    "The Rocket Man Show",
    "A1A: The Original Jimmy Buffett Tribute",
    "Bohemian Queen",
    "Elvis: The Concert of Kings",
    "Free Fallin: The Tom Petty Concert Experience",
    "Kiss The Sky: A Jimi Hendrix Tribute",
    "Legends of Classic Rock",
    "Monkee Men",
    "Vitaly: An Evening of Wonders!",
]
ARTIST_WEBSITES: dict[str, str] = {
    "Arrival From Sweden: The Music of ABBA": "https://www.themusicofabba.com/tourtickets/",
    "The Dolly Show": "https://thedollyshow.com/show-dates-2026-tour/",
    "Kyle Martin's Piano Man": "https://www.pianomantheshow.com/touring-and-events",
    "The Rocket Man Show": "https://www.rocketmanshow.com/dates",
    "A1A: The Original Jimmy Buffett Tribute": "https://www.a1a-live.com/live.html#/",
    "Bohemian Queen": "https://www.zennentertainment.com/shows",
    "Elvis: The Concert of Kings": "https://elvisconcertofkings.com/tour-dates/",
    "Free Fallin: The Tom Petty Concert Experience": "https://www.freefallin.us/live",
    "Kiss The Sky: A Jimi Hendrix Tribute": "https://www.kisstheskytribute.com/tour.html",
    "Legends of Classic Rock": "https://www.locrband.com/tour",
    "Monkee Men": "https://monkeemen.com/#tour",
    "Vitaly: An Evening of Wonders!": "https://www.eveningofwonders.com/tickets/",
}
# Bandsintown profile names differ from our internal names for some artists.
# Only needed for artists in BANDSINTOWN_APP_IDS or BANDSINTOWN_WIDGET_PAGES.
BANDSINTOWN_ARTIST_NAMES: dict[str, str] = {
    "A1A: The Original Jimmy Buffett Tribute": "A1A Official Jimmy Buffett Tribute Band",
    "Free Fallin: The Tom Petty Concert Experience": "Free Fallin - The Tom Petty Concert Experience",
    "Kiss The Sky: A Jimi Hendrix Tribute": "id_15607366",
}

# Some artists' Bandsintown events are only accessible using their own app_id
# (extracted from the data-app-id attribute of the Bandsintown widget on their site).
# Maps internal name -> app_id string.
BANDSINTOWN_APP_IDS: dict[str, str] = {
    "Kiss The Sky: A Jimi Hendrix Tribute": "9e91d98985d7c2eadfca1dcba0337f06",
}

# Artists whose tour pages are purely a Bandsintown JS widget (no static HTML dates).
# When the REST API returns 0, we load the page in a headless browser and intercept
# the Bandsintown API response the widget makes internally.
BANDSINTOWN_WIDGET_PAGES: dict[str, str] = {
    "A1A: The Original Jimmy Buffett Tribute": "https://www.a1a-live.com/live.html",
    "Bohemian Queen": "https://www.zennentertainment.com/shows",
    "Free Fallin: The Tom Petty Concert Experience": "https://www.freefallin.us/live",
}

# Output
GOOGLE_SHEETS_ID = os.environ.get("GOOGLE_SHEETS_ID", "")  # leave blank to skip
GOOGLE_DOC_ID = os.environ.get("GOOGLE_DOC_ID", "")         # leave blank to skip
OUTPUT_WEBSITE_URL = os.environ.get("OUTPUT_WEBSITE_URL", "")  # leave blank to skip
OUTPUT_JSON_PATH = os.environ.get("OUTPUT_JSON_PATH", "/tmp/tour_dates.json")

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Show:
    artist: str
    date: str  # ISO 8601 date string, e.g. "2026-08-15"
    venue: str
    city: str
    region: str
    country: str
    ticket_url: str
    source: str  # which service provided this record
    raw_id: str = ""  # source-specific identifier for deduplication

    def dedup_key(self) -> str:
        """Stable hash used to deduplicate across sources."""
        raw = f"{self.artist}|{self.date}|{self.venue}|{self.city}"
        return hashlib.md5(raw.lower().encode()).hexdigest()


# ---------------------------------------------------------------------------
# Source: Bandsintown
# ---------------------------------------------------------------------------


def _fetch_bandsintown_via_widget(artist: str, page_url: str) -> list[Show]:
    """
    Load an artist's tour page in a headless browser and intercept the Bandsintown
    API response the widget makes internally. Used when the REST API returns 0.
    Requires: playwright install chromium
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        log.warning("playwright not installed — skipping widget scrape for %s", artist)
        return []

    captured: list[dict] = []

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()

            # Wait specifically for the Bandsintown events API call the widget makes,
            # rather than waiting for all network activity to stop (which never happens).
            with page.expect_response(
                lambda r: "rest.bandsintown.com" in r.url and "/events" in r.url,
                timeout=20000,
            ) as response_info:
                page.goto(page_url, wait_until="domcontentloaded", timeout=30000)

            resp = response_info.value
            log.info("Bandsintown widget intercepted: %s", resp.url)
            body = resp.text().strip()
            # Response may be JSONP: callbackName([...]) — strip the wrapper
            import re as _re
            jsonp_match = _re.match(r"^\w+\((.+)\)\s*$", body, _re.DOTALL)
            if jsonp_match:
                body = jsonp_match.group(1)
            data = json.loads(body)
            if isinstance(data, list):
                captured.extend(data)
            browser.close()
    except Exception as exc:
        log.error("Playwright widget scrape error for %s: %s", artist, exc)
        return []

    from datetime import date as _date
    today = _date.today().isoformat()
    shows = []
    for ev in captured:
        dt = ev.get("datetime", "")[:10]
        if dt < today:
            continue
        venue = ev.get("venue", {})
        offers = ev.get("offers", [])
        ticket_url = offers[0].get("url", "") if offers else ""
        shows.append(Show(
            artist=artist,
            date=dt,
            venue=venue.get("name", ""),
            city=venue.get("city", ""),
            region=venue.get("region", ""),
            country=venue.get("country", ""),
            ticket_url=ticket_url,
            source="bandsintown",
        ))
    log.info("Bandsintown widget: %d shows for %s", len(shows), artist)
    return shows


def fetch_bandsintown(artist: str) -> list[Show]:
    """Fetch upcoming events from Bandsintown REST API or JS widget interception.

    Only runs for artists we have a confirmed working method for:
    - BANDSINTOWN_APP_IDS: artists with their own app_id (Kiss the Sky)
    - BANDSINTOWN_WIDGET_PAGES: artists whose site embeds a Bandsintown JS widget
    All other artists are skipped to avoid noisy 404s / useless zero-result calls.
    """
    has_app_id = artist in BANDSINTOWN_APP_IDS
    has_widget = artist in BANDSINTOWN_WIDGET_PAGES
    if not has_app_id and not has_widget:
        return []

    shows: list[Show] = []

    if has_app_id:
        bandsintown_name = BANDSINTOWN_ARTIST_NAMES.get(artist, artist)
        log.info("Bandsintown REST lookup: %s", bandsintown_name)
        url = f"https://rest.bandsintown.com/artists/{requests.utils.quote(bandsintown_name)}/events"
        params = {"app_id": BANDSINTOWN_APP_IDS[artist], "date": "upcoming"}
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            events = resp.json()
        except Exception as exc:
            log.error("Bandsintown error for %s: %s", artist, exc)
            events = []

        for ev in events:
            venue = ev.get("venue", {})
            offers = ev.get("offers", [])
            ticket_url = offers[0].get("url", "") if offers else ""
            shows.append(
                Show(
                    artist=artist,
                    date=ev.get("datetime", "")[:10],
                    venue=venue.get("name", ""),
                    city=venue.get("city", ""),
                    region=venue.get("region", ""),
                    country=venue.get("country", ""),
                    ticket_url=ticket_url,
                    source="bandsintown",
                    raw_id=str(ev.get("id", "")),
                )
            )
        log.info("Bandsintown REST: %d shows for %s", len(shows), artist)

    if not shows and has_widget:
        log.info("Trying Bandsintown widget scrape for %s", artist)
        shows = _fetch_bandsintown_via_widget(artist, BANDSINTOWN_WIDGET_PAGES[artist])

    return shows


# ---------------------------------------------------------------------------
# Source: SeatGeek
# ---------------------------------------------------------------------------


def fetch_seatgeek(artist: str) -> list[Show]:
    """Fetch upcoming events from SeatGeek API."""
    if not _key_set(SEATGEEK_CLIENT_ID):
        log.warning("SEATGEEK_CLIENT_ID not set, skipping SeatGeek")
        return []
    url = "https://api.seatgeek.com/2/events"
    params = {
        "performers.slug": artist.lower().replace(" ", "-"),
        "client_id": SEATGEEK_CLIENT_ID,
        "per_page": 100,
        "sort": "datetime_local.asc",
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.error("SeatGeek error for %s: %s", artist, exc)
        return []

    shows = []
    for ev in data.get("events", []):
        venue = ev.get("venue", {})
        shows.append(
            Show(
                artist=artist,
                date=ev.get("datetime_local", "")[:10],
                venue=venue.get("name", ""),
                city=venue.get("city", ""),
                region=venue.get("state", ""),
                country=venue.get("country", ""),
                ticket_url=ev.get("url", ""),
                source="seatgeek",
                raw_id=str(ev.get("id", "")),
            )
        )
    log.info("SeatGeek: %d shows for %s", len(shows), artist)
    return shows


# ---------------------------------------------------------------------------
# Source: Artist website (scrape + Claude parse)
# ---------------------------------------------------------------------------


def fetch_artist_website(artist: str) -> list[Show]:
    """Scrape the artist's tour page and use Claude to parse dates and ticket links."""
    import re as _re
    from urllib.parse import urljoin
    from datetime import date as _date

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
        page_text = _re.sub(r"\n{3,}", "\n\n", page_text).strip()[:32000]
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

    global _claude_call_count
    _claude_throttle()
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        raw = client.messages.with_raw_response.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        resp_msg = raw.parse()
        _claude_call_count += 1
        _claude_call_done(dict(raw.headers))
    except Exception as exc:
        log.error("Artist website Claude parse error for %s: %s", artist, exc)
        return []

    text = ""
    for block in resp_msg.content:
        if hasattr(block, "text"):
            text += block.text

    text = _re.sub(r"```(?:json)?\s*", "", text)
    match = _re.search(r"\[.*\]", text, _re.DOTALL)
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


# ---------------------------------------------------------------------------
# Source: Ticketmaster Discovery
# ---------------------------------------------------------------------------


def fetch_ticketmaster(artist: str) -> list[Show]:
    """Fetch upcoming events from Ticketmaster Discovery API."""
    if not _key_set(TICKETMASTER_API_KEY):
        log.warning("TICKETMASTER_API_KEY not set, skipping Ticketmaster")
        return []
    url = "https://app.ticketmaster.com/discovery/v2/events.json"
    params = {
        "keyword": artist,
        "apikey": TICKETMASTER_API_KEY,
        "classificationName": "music",
        "sort": "date,asc",
        "size": 100,
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.error("Ticketmaster error for %s: %s", artist, exc)
        return []

    shows = []
    for ev in data.get("_embedded", {}).get("events", []):
        venues = ev.get("_embedded", {}).get("venues", [{}])
        v = venues[0] if venues else {}
        city = v.get("city", {}).get("name", "")
        region = v.get("state", {}).get("stateCode", "")
        country = v.get("country", {}).get("countryCode", "")
        date_str = ev.get("dates", {}).get("start", {}).get("localDate", "")
        ticket_url = ev.get("url", "")
        shows.append(
            Show(
                artist=artist,
                date=date_str,
                venue=v.get("name", ""),
                city=city,
                region=region,
                country=country,
                ticket_url=ticket_url,
                source="ticketmaster",
                raw_id=str(ev.get("id", "")),
            )
        )
    log.info("Ticketmaster: %d shows for %s", len(shows), artist)
    return shows


# ---------------------------------------------------------------------------
# Source: Claude web search
# ---------------------------------------------------------------------------

_claude_call_count = 0
_THROTTLE_FILE = "/tmp/tour_dates_throttle.txt"
CLAUDE_RATE_LIMIT_BUFFER = 2  # extra seconds of padding after the API's reset timestamp


def _load_throttle() -> float:
    """Read persisted throttle timestamp from disk (survives process restarts)."""
    try:
        with open(_THROTTLE_FILE) as f:
            return float(f.read().strip())
    except Exception:
        return 0.0


def _save_throttle(t: float) -> None:
    try:
        with open(_THROTTLE_FILE, "w") as f:
            f.write(str(t))
    except Exception:
        pass


def _claude_throttle() -> None:
    """Sleep until the API's own rate-limit reset time (persisted across restarts)."""
    next_at = _load_throttle()
    wait = next_at - time.time()
    if wait > 0:
        log.info("Rate limit throttle: waiting %.0fs (from API reset header)...", wait)
        time.sleep(wait)


def _claude_call_done(headers: dict) -> None:
    """Parse rate-limit headers from a successful response and persist the next-call time."""
    reset_str = headers.get("anthropic-ratelimit-input-tokens-reset") or headers.get(
        "anthropic-ratelimit-tokens-reset"
    )
    if reset_str:
        try:
            reset_dt = datetime.fromisoformat(reset_str.replace("Z", "+00:00"))
            reset_epoch = reset_dt.timestamp()
            next_at = reset_epoch + CLAUDE_RATE_LIMIT_BUFFER
            _save_throttle(next_at)
            log.info(
                "Token reset at %s — next call allowed in %.0fs",
                reset_str,
                max(0, next_at - time.time()),
            )
            return
        except Exception:
            pass
    # Fallback if header missing: wait 90s from now
    _save_throttle(time.time() + 90)


def fetch_claude_web_search(artist: str) -> list[Show]:
    """Use Claude with web_search tool to find tour dates, including artist website."""
    import re
    from datetime import date as _date

    global _claude_call_count
    if _claude_call_count >= CLAUDE_CALL_LIMIT:
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
        "Return ONLY a JSON array of objects with these exact keys: "
        "date (YYYY-MM-DD), venue, city, region, country, ticket_url. "
        "If ticket_url is unknown use an empty string. "
        "Do not include any text outside the JSON array."
    )

    _claude_throttle()
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        raw = client.messages.with_raw_response.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}],
        )
        resp = raw.parse()
        _claude_call_count += 1
        _claude_call_done(dict(raw.headers))
    except Exception as exc:
        log.error("Claude web search error for %s: %s", artist, exc)
        return []

    # Extract text content from the response (web_search tool may produce multiple blocks)
    import re

    text = ""
    for block in resp.content:
        if hasattr(block, "text"):
            text += block.text
        elif hasattr(block, "type") and block.type == "tool_result":
            pass  # skip raw search result blocks

    # Strip markdown code fences if present, then find the JSON array
    text = re.sub(r"```(?:json)?\s*", "", text)
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        log.error(
            "Claude JSON parse error for %s: no JSON array found\nRaw: %s",
            artist,
            text[:500],
        )
        return []
    try:
        events = json.loads(match.group())
    except json.JSONDecodeError as exc:
        log.error(
            "Claude JSON parse error for %s: %s\nRaw: %s", artist, exc, text[:500]
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
            )
        )
    log.info("Claude web search: %d shows for %s", len(shows), artist)
    return shows


# ---------------------------------------------------------------------------
# Ticket link enrichment via Claude
# ---------------------------------------------------------------------------


def enrich_ticket_urls_for_artist(shows: list[Show], fallbacks: dict[str, str]) -> None:
    """
    Find venue-direct ticket URLs for all of an artist's shows in one Claude call.
    Mutates shows in place. Falls back to platform URLs if Claude can't find venue-direct links.
    """
    global _claude_call_count

    # Apply fallbacks first; we'll overwrite with venue-direct URLs where Claude finds them
    for show in shows:
        if not show.ticket_url:
            show.ticket_url = fallbacks.get(show.dedup_key(), "")

    # Filter to shows that still need enrichment (missing or platform URL)
    to_enrich = [s for s in shows if not s.ticket_url or _is_platform_url(s.ticket_url)]
    if not to_enrich:
        return

    if _claude_call_count >= CLAUDE_CALL_LIMIT:
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

    _claude_throttle()
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        raw = client.messages.with_raw_response.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}],
        )
        resp = raw.parse()
        _claude_call_count += 1
        _claude_call_done(dict(raw.headers))
    except Exception as exc:
        log.error("Claude ticket enrichment error for %s: %s", artist, exc)
        return

    import re

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


# ---------------------------------------------------------------------------
# Aggregation and deduplication
# ---------------------------------------------------------------------------


def aggregate(artist: str) -> list[Show]:
    """
    Collect shows from all sources, deduplicate, then enrich ticket links.
    Show dedup priority: Bandsintown > SeatGeek > Ticketmaster > Claude web search.
    Ticket URL priority: venue-direct (via Claude) > Ticketmaster/platform fallback.
    """
    all_shows: list[Show] = []
    all_shows.extend(fetch_bandsintown(artist))
    all_shows.extend(fetch_seatgeek(artist))
    all_shows.extend(fetch_artist_website(artist))
    all_shows.extend(fetch_ticketmaster(artist))
    all_shows.extend(fetch_claude_web_search(artist))

    # Deduplicate: keep highest-priority source for each show
    seen: dict[str, Show] = {}
    source_priority = {
        "bandsintown": 0,
        "seatgeek": 1,
        "artist_website": 2,
        "ticketmaster": 3,
        "claude_web_search": 4,
    }
    for show in all_shows:
        key = show.dedup_key()
        if key not in seen:
            seen[key] = show
        else:
            existing = seen[key]
            if source_priority.get(show.source, 99) < source_priority.get(
                existing.source, 99
            ):
                seen[key] = show

    # Enrich ticket URLs: Claude scrapes venue site, falls back to best available URL
    # Find a fallback URL from any source for each deduped show
    fallbacks: dict[str, str] = {}
    for show in all_shows:
        key = show.dedup_key()
        if show.ticket_url and key not in fallbacks:
            fallbacks[key] = show.ticket_url

    from datetime import date as _date
    today = _date.today().isoformat()
    deduped = sorted(
        (s for s in seen.values() if s.date >= today),
        key=lambda s: s.date,
    )
    enrich_ticket_urls_for_artist(deduped, fallbacks)

    return deduped


# ---------------------------------------------------------------------------
# Output: local JSON
# ---------------------------------------------------------------------------


def write_json(shows: list[Show]) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "shows": [asdict(s) for s in shows],
    }
    with open(OUTPUT_JSON_PATH, "w") as f:
        json.dump(payload, f, indent=2)
    log.info("Wrote %d shows to %s", len(shows), OUTPUT_JSON_PATH)


# ---------------------------------------------------------------------------
# Output: Google Sheets (optional)
# ---------------------------------------------------------------------------

_SOURCE_LABELS = {
    "bandsintown": "Bandsintown",
    "seatgeek": "SeatGeek",
    "artist_website": "Artist Website",
    "ticketmaster": "Ticketmaster",
    "claude_web_search": "Web Search",
}


def _fmt_date(iso_date: str) -> str:
    """Convert ISO date (2026-04-05) to MM/DD/YY (04/05/26)."""
    from datetime import date as _date
    return _date.fromisoformat(iso_date).strftime("%m/%d/%y")


def build_sheet_rows(shows: list[Show]) -> list[list[str]]:
    """Build spreadsheet rows — booked shows only, no Open/ellipsis rows."""
    header = [["Date", "Venue", "City", "Region", "Country", "Ticket URL", "Source"]]
    rows = [
        [
            _fmt_date(show.date),
            show.venue,
            show.city,
            show.region,
            show.country,
            show.ticket_url,
            _SOURCE_LABELS.get(show.source, show.source),
        ]
        for show in shows
    ]
    return header + rows


def _get_or_create_tab(service, spreadsheet_id: str, title: str) -> None:
    """Ensure a tab with the given title exists; create it if not."""
    title = title[:100]  # Sheets API limit
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    existing = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if title not in existing:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
        ).execute()
        log.info("Created sheet tab: %s", title)


def _read_tab_ticket_urls(service, spreadsheet_id: str, tab: str, artist: str) -> dict[str, str]:
    """
    Read existing sheet tab and return {dedup_key -> ticket_url} for rows with
    venue-direct (non-platform) URLs. Used to preserve good URLs across runs.
    """
    from datetime import datetime as _dt
    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"'{tab}'!A1:G",
        ).execute()
    except Exception:
        return {}
    saved: dict[str, str] = {}
    for row in result.get("values", [])[1:]:
        date_val = row[0] if row else ""
        venue_val = row[1] if len(row) > 1 else ""
        ticket_url = row[5] if len(row) > 5 else ""
        if not date_val or not venue_val or not ticket_url:
            continue
        if _is_platform_url(ticket_url):
            continue
        try:
            iso = _dt.strptime(date_val, "%m/%d/%y").date().isoformat()
        except ValueError:
            continue
        city = row[2] if len(row) > 2 else ""
        key = hashlib.md5(f"{artist}|{iso}|{venue_val}|{city}".lower().encode()).hexdigest()
        saved[key] = ticket_url
    return saved


def write_google_sheets(shows: list[Show]) -> None:
    """
    Push shows to a Google Sheet, one tab per artist. Requires:
      - google-auth, google-api-python-client packages
      - GOOGLE_APPLICATION_CREDENTIALS env var pointing to a service account JSON
      - The sheet shared with the service account email
    """
    if not GOOGLE_SHEETS_ID:
        return
    try:
        from googleapiclient.discovery import build  # type: ignore
        from google.oauth2 import service_account  # type: ignore
    except ImportError:
        log.warning("google-api-python-client not installed, skipping Sheets output")
        return

    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if not creds_path:
        log.warning("GOOGLE_APPLICATION_CREDENTIALS not set, skipping Sheets output")
        return

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = service_account.Credentials.from_service_account_file(
        creds_path, scopes=scopes
    )
    service = build("sheets", "v4", credentials=creds)

    # Group shows by artist, preserving date sort within each group
    by_artist: dict[str, list[Show]] = {}
    for show in shows:
        by_artist.setdefault(show.artist, []).append(show)

    for artist, artist_shows in by_artist.items():
        artist_shows.sort(key=lambda s: s.date)
        tab = artist[:100]
        _get_or_create_tab(service, GOOGLE_SHEETS_ID, tab)

        # Carry forward venue-direct ticket URLs from previous run
        saved_urls = _read_tab_ticket_urls(service, GOOGLE_SHEETS_ID, tab, artist)
        for show in artist_shows:
            if show.dedup_key() in saved_urls:
                if not show.ticket_url or _is_platform_url(show.ticket_url):
                    show.ticket_url = saved_urls[show.dedup_key()]

        service.spreadsheets().values().clear(
            spreadsheetId=GOOGLE_SHEETS_ID,
            range=f"'{tab}'!A1:Z",
        ).execute()
        rows = build_sheet_rows(artist_shows)
        service.spreadsheets().values().update(
            spreadsheetId=GOOGLE_SHEETS_ID,
            range=f"'{tab}'!A1",
            valueInputOption="RAW",
            body={"values": rows},
        ).execute()
        log.info("Updated tab '%s' with %d rows", tab, len(rows))


# ---------------------------------------------------------------------------
# Output: Google Doc (optional)
# ---------------------------------------------------------------------------

# Geographic email zones — only zones with ≥1 show for an artist are written.
EMAIL_ZONES: list[tuple[str, list[str]]] = [
    ("New England",       ["CT", "MA", "ME", "NH", "RI", "VT"]),
    ("Mid-Atlantic",      ["DC", "DE", "MD", "NJ", "NY", "PA"]),
    ("Southeast",         ["AL", "FL", "GA", "MS", "NC", "SC", "TN", "VA", "WV"]),
    ("South Central",     ["AR", "KY", "LA", "MO", "OK", "TX"]),
    ("Great Lakes",       ["IL", "IN", "MI", "OH", "WI"]),
    ("Plains",            ["IA", "KS", "MN", "NE", "ND", "SD"]),
    ("Mountain",          ["CO", "ID", "MT", "NM", "UT", "WY"]),
    ("Southwest",         ["AZ", "CA", "NV"]),
    ("Pacific Northwest", ["OR", "WA"]),
]


def _build_doc_month_text(shows: list[Show]) -> tuple[str, list[tuple[int, int]]]:
    """
    Build plain-text content for one month.
    Returns (text, open_ranges) where open_ranges are (start, end) char offsets
    within text marking each OPEN line (for bold formatting).
    Each booked show is surrounded by up to 2 open dates on each side,
    filtered to the same calendar month and deduplicated.
    """
    from datetime import date as _date, timedelta

    month_year = _date.fromisoformat(shows[0].date).replace(day=1)
    date_map: dict[_date, Show | None] = {}

    for show in shows:
        d = _date.fromisoformat(show.date)
        date_map[d] = show
        for i in range(1, 3):
            for open_d in (d - timedelta(days=i), d + timedelta(days=i)):
                if open_d.year == month_year.year and open_d.month == month_year.month:
                    if open_d not in date_map:
                        date_map[open_d] = None

    lines: list[str] = []
    open_ranges: list[tuple[int, int]] = []
    pos = 0
    for d in sorted(date_map.keys()):
        show = date_map[d]
        date_str = d.strftime("%A, %B %-d, %Y")
        if show is None:
            line = f"{date_str} - OPEN"
            open_ranges.append((pos, pos + len(line)))
        else:
            parts = [p for p in [show.city, show.region] if p]
            location = ", ".join(parts) if parts else (show.venue or "")
            line = f"{date_str} - {location}"
        lines.append(line)
        pos += len(line) + 1  # +1 for the \n separator

    return "\n".join(lines), open_ranges


def _assemble_doc_sections(
    by_month: dict[str, list[Show]],
) -> tuple[str, list[tuple[int, int]], list[tuple[int, int]]]:
    """
    Assemble multi-month content from a dict of {YYYY-MM: [Show]}.
    Returns (text, heading_ranges, open_ranges) as 0-based char offsets within text.
    Caller adds the doc insert index (usually 1) to get actual doc positions.
    """
    from datetime import date as _date

    parts: list[str] = []
    heading_ranges: list[tuple[int, int]] = []
    open_ranges: list[tuple[int, int]] = []
    pos = 0

    for i, (month_key, month_shows) in enumerate(sorted(by_month.items())):
        month_label = _date.fromisoformat(month_key + "-01").strftime("%B %Y")
        month_text, m_open = _build_doc_month_text(month_shows)

        if i > 0:
            parts.append("\n\n")
            pos += 2

        heading_ranges.append((pos, pos + len(month_label)))
        parts.append(month_label + "\n")
        pos += len(month_label) + 1

        for s, e in m_open:
            open_ranges.append((pos + s, pos + e))

        parts.append(month_text)
        pos += len(month_text)

    return "".join(parts), heading_ranges, open_ranges


def _apply_doc_styles(
    service,
    doc_id: str,
    tab_id: str,
    insert_offset: int,
    heading_ranges: list[tuple[int, int]],
    open_ranges: list[tuple[int, int]],
    heading_level: str = "HEADING_2",
    extra_headings: list[tuple[int, int, str]] | None = None,
) -> None:
    """Batch-apply heading and bold styles after inserting text."""
    reqs: list[dict] = []
    for s, e in heading_ranges:
        reqs.append({"updateParagraphStyle": {
            "range": {"startIndex": insert_offset + s, "endIndex": insert_offset + e, "tabId": tab_id},
            "paragraphStyle": {"namedStyleType": heading_level},
            "fields": "namedStyleType",
        }})
    for s, e, level in (extra_headings or []):
        reqs.append({"updateParagraphStyle": {
            "range": {"startIndex": insert_offset + s, "endIndex": insert_offset + e, "tabId": tab_id},
            "paragraphStyle": {"namedStyleType": level},
            "fields": "namedStyleType",
        }})
    for s, e in open_ranges:
        reqs.append({"updateTextStyle": {
            "range": {"startIndex": insert_offset + s, "endIndex": insert_offset + e, "tabId": tab_id},
            "textStyle": {"bold": True},
            "fields": "bold",
        }})
    if reqs:
        service.documents().batchUpdate(documentId=doc_id, body={"requests": reqs}).execute()


def write_google_doc(shows: list[Show]) -> None:
    """
    Write all shows to a Google Doc — one tab per artist, one subtab per month.
    Each subtab contains plain text: date, venue, city/region, with up to 2 open
    dates before and after each booked show. No ticket links.
    """
    if not GOOGLE_DOC_ID:
        return
    try:
        from googleapiclient.discovery import build  # type: ignore
        from google.oauth2 import service_account  # type: ignore
    except ImportError:
        log.warning("google-api-python-client not installed, skipping Doc output")
        return

    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if not creds_path:
        log.warning("GOOGLE_APPLICATION_CREDENTIALS not set, skipping Doc output")
        return

    scopes = [
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = service_account.Credentials.from_service_account_file(
        creds_path, scopes=scopes
    )
    service = build("docs", "v1", credentials=creds)

    # Can't delete the last remaining tab, so we create a short-lived placeholder
    # first, then re-query the doc for currently-live tab IDs, delete those
    # (clearing any stale name conflicts), then build real artist tabs, and
    # finally remove the placeholder.
    import uuid as _uuid
    placeholder_resp = service.documents().batchUpdate(
        documentId=GOOGLE_DOC_ID,
        body={"requests": [{"addDocumentTab": {
            "tabProperties": {"title": f"_tmp_{_uuid.uuid4().hex[:8]}"},
        }}]},
    ).execute()
    placeholder_id = placeholder_resp["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]

    # Re-query AFTER placeholder creation so tab IDs are current
    doc = service.documents().get(
        documentId=GOOGLE_DOC_ID, includeTabsContent=True
    ).execute()
    old_tab_ids = [
        t["tabProperties"]["tabId"]
        for t in doc.get("tabs", [])
        if t["tabProperties"]["tabId"] != placeholder_id
    ]
    if old_tab_ids:
        service.documents().batchUpdate(
            documentId=GOOGLE_DOC_ID,
            body={"requests": [{"deleteTab": {"tabId": tid}} for tid in old_tab_ids]},
        ).execute()

    # Group shows by artist then by month
    by_artist: dict[str, list[Show]] = {}
    for show in shows:
        by_artist.setdefault(show.artist, []).append(show)

    artist_list = [(a, sorted(s, key=lambda x: x.date)) for a, s in by_artist.items()]

    # Docs API requires unique tab titles document-wide, so subtabs are prefixed
    # with the artist name to avoid collisions (e.g. "Dolly Show June 2026").
    from datetime import date as _date
    for artist, artist_shows in artist_list:
        by_month: dict[str, list[Show]] = {}
        for show in artist_shows:
            by_month.setdefault(show.date[:7], []).append(show)

        # --- Artist parent tab: full year overview + states list ---
        resp = service.documents().batchUpdate(
            documentId=GOOGLE_DOC_ID,
            body={"requests": [{"addDocumentTab": {
                "tabProperties": {"title": artist[:100]},
            }}]},
        ).execute()
        artist_tab_id = resp["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]

        full_text, heading_ranges, open_ranges = _assemble_doc_sections(by_month)
        all_states = sorted({s.region for s in artist_shows if s.region})
        if all_states:
            full_text += f"\n\nStates: {', '.join(all_states)}"
        service.documents().batchUpdate(
            documentId=GOOGLE_DOC_ID,
            body={"requests": [{"insertText": {
                "location": {"index": 1, "tabId": artist_tab_id},
                "text": full_text,
            }}]},
        ).execute()
        _apply_doc_styles(service, GOOGLE_DOC_ID, artist_tab_id, 1, heading_ranges, open_ranges)

        # --- Month subtabs ---
        for month_key, month_shows in sorted(by_month.items()):
            month_label = _date.fromisoformat(month_key + "-01").strftime("%B %Y")
            subtab_title = f"{artist[:80]} {month_label}"
            resp = service.documents().batchUpdate(
                documentId=GOOGLE_DOC_ID,
                body={"requests": [{"addDocumentTab": {
                    "tabProperties": {"title": subtab_title, "parentTabId": artist_tab_id},
                }}]},
            ).execute()
            subtab_id = resp["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]

            month_text, m_open = _build_doc_month_text(month_shows)
            body_text = f"{month_label}\n{month_text}"
            service.documents().batchUpdate(
                documentId=GOOGLE_DOC_ID,
                body={"requests": [{"insertText": {
                    "location": {"index": 1, "tabId": subtab_id},
                    "text": body_text,
                }}]},
            ).execute()
            # month heading starts at doc index 1; open lines are offset by len(month_label)+1+1
            offset = len(month_label) + 1
            _apply_doc_styles(
                service, GOOGLE_DOC_ID, subtab_id, 1,
                heading_ranges=[(0, len(month_label))],
                open_ranges=[(offset + s, offset + e) for s, e in m_open],
            )
            log.info("Doc: wrote %s / %s (%d shows)", artist, month_label, len(month_shows))

        # --- Email zone subtabs ---
        zone_num = 0
        for zone_name, zone_states in EMAIL_ZONES:
            zone_shows = [s for s in artist_shows if s.region in zone_states]
            if not zone_shows:
                continue
            zone_num += 1

            zone_by_month: dict[str, list[Show]] = {}
            for show in zone_shows:
                zone_by_month.setdefault(show.date[:7], []).append(show)

            zone_states_present = sorted({s.region for s in zone_shows})
            if len(zone_states_present) < 2:
                continue
            zone_header = (
                f"Email Zone {zone_num}: {zone_name}\n"
                f"States: {', '.join(zone_states_present)}\n\n"
            )
            zone_body, z_heading_ranges, z_open_ranges = _assemble_doc_sections(zone_by_month)
            full_zone_text = zone_header + zone_body
            header_len = len(zone_header)

            subtab_title = f"{artist[:65]} Zone: {zone_name}"
            resp = service.documents().batchUpdate(
                documentId=GOOGLE_DOC_ID,
                body={"requests": [{"addDocumentTab": {
                    "tabProperties": {"title": subtab_title, "parentTabId": artist_tab_id},
                }}]},
            ).execute()
            zone_tab_id = resp["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]

            service.documents().batchUpdate(
                documentId=GOOGLE_DOC_ID,
                body={"requests": [{"insertText": {
                    "location": {"index": 1, "tabId": zone_tab_id},
                    "text": full_zone_text,
                }}]},
            ).execute()
            _apply_doc_styles(
                service, GOOGLE_DOC_ID, zone_tab_id, 1,
                heading_ranges=[(header_len + s, header_len + e) for s, e in z_heading_ranges],
                open_ranges=[(header_len + s, header_len + e) for s, e in z_open_ranges],
                extra_headings=[(0, len(f"Email Zone {zone_num}: {zone_name}"), "HEADING_1")],
            )
            log.info("Doc: wrote %s / Zone: %s (%d shows)", artist, zone_name, len(zone_shows))

    # Remove the placeholder now that real artist tabs exist
    service.documents().batchUpdate(
        documentId=GOOGLE_DOC_ID,
        body={"requests": [{"deleteTab": {"tabId": placeholder_id}}]},
    ).execute()


# ---------------------------------------------------------------------------
# Output: website POST (optional)
# ---------------------------------------------------------------------------


def write_website(shows: list[Show]) -> None:
    """
    POST the shows JSON to a webhook/API endpoint on the destination website.
    Expects the endpoint to accept { "shows": [...] } and return 2xx.
    """
    if not OUTPUT_WEBSITE_URL:
        return
    payload = {"shows": [asdict(s) for s in shows]}
    try:
        resp = requests.post(OUTPUT_WEBSITE_URL, json=payload, timeout=15)
        resp.raise_for_status()
        log.info("Posted %d shows to %s", len(shows), OUTPUT_WEBSITE_URL)
    except Exception as exc:
        log.error("Website output error: %s", exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run() -> None:
    if not BAND_NAMES:
        log.error("BAND_NAMES is empty — add artist names to the config and re-run.")
        return

    all_shows: list[Show] = []
    for artist in BAND_NAMES:
        log.info("=== Fetching shows for: %s ===", artist)
        shows = aggregate(artist)
        log.info("  -> %d unique shows after dedup", len(shows))
        all_shows.extend(shows)

    all_shows.sort(key=lambda s: (s.date, s.artist))

    log.info(
        "Total Claude API calls this run: %d / %d",
        _claude_call_count,
        CLAUDE_CALL_LIMIT,
    )

    write_json(all_shows)
    write_google_sheets(all_shows)
    write_google_doc(all_shows)
    write_website(all_shows)

    log.info("Done. %d total shows across %d artists.", len(all_shows), len(BAND_NAMES))


def test_sheets() -> None:
    dummy = [
        Show(
            artist="Test Artist",
            date="2026-06-01",
            venue="Venue A",
            city="Nashville",
            region="TN",
            country="US",
            ticket_url="https://example.com/1",
            source="test",
        ),
        Show(
            artist="Test Artist",
            date="2026-06-04",
            venue="Venue B",
            city="Atlanta",
            region="GA",
            country="US",
            ticket_url="https://example.com/2",
            source="test",
        ),
        Show(
            artist="Test Artist",
            date="2026-06-20",
            venue="Venue C",
            city="Chicago",
            region="IL",
            country="US",
            ticket_url="https://example.com/3",
            source="test",
        ),
        Show(
            artist="Test Artist 2",
            date="2026-07-10",
            venue="Venue D",
            city="London",
            region="",
            country="GB",
            ticket_url="",
            source="test",
        ),
        Show(
            artist="Test Artist 2",
            date="2026-07-11",
            venue="Venue E",
            city="Manchester",
            region="",
            country="GB",
            ticket_url="",
            source="test",
        ),
        Show(
            artist="Test Artist 2",
            date="2026-07-14",
            venue="Venue F",
            city="Edinburgh",
            region="",
            country="GB",
            ticket_url="",
            source="test",
        ),
    ]
    log.info("Writing %d dummy shows to Google Sheets...", len(dummy))
    write_google_sheets(dummy)
    log.info("Test complete.")


def test_ticketmaster() -> None:
    all_shows: list[Show] = []
    for artist in BAND_NAMES:
        log.info("Fetching Ticketmaster shows for: %s", artist)
        shows = fetch_ticketmaster(artist)
        log.info("  -> %d shows", len(shows))
        all_shows.extend(shows)
    all_shows.sort(key=lambda s: (s.date, s.artist))
    log.info("Total: %d shows across %d artists", len(all_shows), len(BAND_NAMES))
    write_google_sheets(all_shows)
    log.info("Test complete.")


def test_claude() -> None:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=64,
        messages=[{"role": "user", "content": "Reply with only the word PONG."}],
    )
    log.info("Claude ping response: %s", resp.content[0].text.strip())


def test_claude_artist() -> None:
    artist = BAND_NAMES[0]
    log.info("Testing Claude web search for: %s", artist)
    shows = fetch_claude_web_search(artist)
    log.info("Found %d shows", len(shows))
    write_google_sheets(shows)
    log.info("Test complete.")


def test_claude_calls() -> None:
    """Test artist website scrape + web search + enrichment for the first two artists, log results only — no Sheets write."""
    for artist in BAND_NAMES[:2]:
        log.info("=== Claude call test for: %s ===", artist)

        log.info("Step 1: Artist website scrape...")
        website_shows = fetch_artist_website(artist)
        log.info("  -> %d shows from artist website", len(website_shows))

        log.info("Step 2: Web search...")
        shows = fetch_claude_web_search(artist)
        log.info("  -> %d shows from web search", len(shows))

        # Merge, dedup, prefer artist_website
        from hashlib import md5
        seen: dict[str, Show] = {}
        for s in website_shows + shows:
            k = s.dedup_key()
            if k not in seen or (s.source == "artist_website" and seen[k].source != "artist_website"):
                seen[k] = s
        shows = sorted(seen.values(), key=lambda s: s.date)
        log.info("  -> %d shows after merge+dedup", len(shows))

        log.info("Step 2: Enrichment (venue-direct ticket URLs)...")
        fallbacks: dict[str, str] = {
            s.dedup_key(): s.ticket_url for s in shows if s.ticket_url
        }
        enrich_ticket_urls_for_artist(shows, fallbacks)

        log.info("Results after enrichment:")
        venue_direct = 0
        for s in sorted(shows, key=lambda s: s.date):
            is_direct = s.ticket_url and not _is_platform_url(s.ticket_url)
            tag = "VENUE" if is_direct else "platform" if s.ticket_url else "none"
            if is_direct:
                venue_direct += 1
            log.info(
                "  [%s] %s | %s, %s | %s",
                tag,
                s.date,
                s.venue,
                s.city,
                s.ticket_url or "",
            )

        log.info(
            "Summary: %d/%d shows have venue-direct URLs", venue_direct, len(shows)
        )

    log.info("Total Claude calls this test: %d", _claude_call_count)
    log.info("Test complete.")


def test_doc() -> None:
    """Write dummy shows to the Google Doc to verify tab/subtab structure."""
    dummy = [
        Show(artist="Test Artist", date="2026-06-01", venue="Venue A", city="Nashville", region="TN", country="US", ticket_url="", source="test"),
        Show(artist="Test Artist", date="2026-06-05", venue="Venue B", city="Atlanta", region="GA", country="US", ticket_url="", source="test"),
        Show(artist="Test Artist", date="2026-07-10", venue="Venue C", city="Chicago", region="IL", country="US", ticket_url="", source="test"),
        Show(artist="Test Artist 2", date="2026-06-15", venue="Venue D", city="Austin", region="TX", country="US", ticket_url="", source="test"),
        Show(artist="Test Artist 2", date="2026-06-20", venue="Venue E", city="Dallas", region="TX", country="US", ticket_url="", source="test"),
    ]
    log.info("Writing %d dummy shows to Google Doc...", len(dummy))
    write_google_doc(dummy)
    log.info("Test complete.")


if __name__ == "__main__":
    import sys

    if "--test-doc" in sys.argv:
        test_doc()
    elif "--test-sheets" in sys.argv:
        test_sheets()
    elif "--test-ticketmaster" in sys.argv:
        test_ticketmaster()
    elif "--test-claude" in sys.argv:
        test_claude()
    elif "--test-claude-artist" in sys.argv:
        test_claude_artist()
    elif "--test-claude-calls" in sys.argv:
        test_claude_calls()
    elif "--artist" in sys.argv:
        idx = sys.argv.index("--artist")
        artist_arg = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
        if not artist_arg:
            log.error("--artist requires a name, e.g.: --artist \"Kiss The Sky: A Jimi Hendrix Tribute\"")
        else:
            log.info("=== Single-artist run: %s ===", artist_arg)
            shows = aggregate(artist_arg)
            log.info("  -> %d shows", len(shows))
            for s in shows:
                tag = "VENUE" if s.ticket_url and not _is_platform_url(s.ticket_url) else "platform" if s.ticket_url else "none"
                log.info("  [%s] %s | %s, %s | %s", tag, s.date, s.venue, s.city, s.ticket_url or "")
            write_google_sheets(shows)
    else:
        run()
