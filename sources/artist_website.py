import base64
import json
import logging
import re
from urllib.parse import urljoin
from datetime import date as _date

import requests
import anthropic

import claude_state
from config import (
    extract_json,
    ARTIST_WEBSITES,
    PLAYWRIGHT_RENDER_PAGES,
    VISION_TOUR_PAGES,
    TRIBE_EVENTS_MAP_PAGES,
    ANTHROPIC_API_KEY,
    CLAUDE_MODEL,
    CLAUDE_WEBSITE_MAX_TOKENS,
    _US_STATE_CODES,
    _key_set,
    _iso_time,
    _parse_time_to_24h,
)
from models import Show
from sources.browser import browser_page

_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

log = logging.getLogger(__name__)


def _city_state_from_address(location_name: str) -> tuple[str, str]:
    """Extract (city, state) from a US address like 'Venue, 123 St, City, NY 10001, USA'."""
    m = re.search(r',\s*([A-Za-z][A-Za-z .-]*[A-Za-z]),\s*([A-Z]{2})\s+\d{5}', location_name)
    if m:
        return m.group(1).strip(), m.group(2)
    return "", ""


def _venue_from_location_name(location_name: str) -> str:
    """Extract venue name from an address string (first segment if not a street number)."""
    parts = [p.strip() for p in location_name.split(",")]
    if not parts or not parts[0]:
        return ""
    if parts[0][0].isdigit():
        return ""
    if len(parts) > 1 and parts[1].strip() and parts[1].strip()[0].isdigit():
        return parts[0]
    return parts[0]


def _rendered_html(url: str, artist: str) -> str:
    """The page's HTML after JS has run, or "" if Playwright is unavailable/fails.

    Needed for tour pages that build their listing client-side: the static fetch returns the
    shell only (The Platters' /tour-dates yields 0 dates statically, its full schedule once
    rendered). Returns HTML rather than text so the caller keeps <a href> links to rewrite.
    """
    try:
        with browser_page() as page:
            if page is None:
                return ""
            page.goto(url, wait_until="load", timeout=30000)
            page.wait_for_timeout(3000)  # let the client-side listing paint
            return page.content()
    except Exception as exc:
        log.warning("Rendered fetch failed for %s: %s", artist, exc)
        return ""


def _ld_json_entries(data) -> list[dict]:
    """Flatten one ld+json payload into the Event dicts it contains.

    schema.org allows a single object, a bare array, or a {"@graph": [...]} wrapper, and
    calendar widgets use all three. Assuming a dict raised AttributeError on The Platters'
    array-shaped calendar and aborted that artist's entire aggregation, so normalize instead.
    """
    if isinstance(data, dict):
        graph = data.get("@graph")
        candidates = graph if isinstance(graph, list) else [data]
    elif isinstance(data, list):
        candidates = data
    else:
        return []
    return [c for c in candidates if isinstance(c, dict) and c.get("@type") == "Event"]


def _fetch_elfsight_shows(url: str, artist: str) -> list[Show] | None:
    """
    Playwright scraper for pages with an Elfsight Events Calendar widget.
    Reads all pages by clicking 'Next Events', parses JSON-LD schema.org Event
    data from each page — no Claude call needed.
    Returns None if Playwright is unavailable.
    """
    today = _date.today().isoformat()
    collected_json: list[str] = []

    try:
        with browser_page() as page:
            if page is None:
                return None
            page.goto(url, wait_until="load", timeout=30000)
            # Wait for the Elfsight widget to render its first batch of events
            try:
                page.wait_for_selector(".eapp-events-calendar-list-item-component", timeout=15000)
            except Exception:
                log.warning("Elfsight calendar did not render in time for %s", artist)
            # Collect all pages by clicking Next Events
            while True:
                collected_json.append(page.content())
                try:
                    next_btn = page.locator("button", has_text="Next Events").first
                    if next_btn.is_visible():
                        next_btn.click()
                        page.wait_for_timeout(2000)
                    else:
                        break
                except Exception:
                    break
    except Exception as exc:
        log.error("Playwright Elfsight scrape error for %s: %s", artist, exc)
        return None

    shows: list[Show] = []
    seen_keys: set[str] = set()
    for html_chunk in collected_json:
        from bs4 import BeautifulSoup  # type: ignore
        soup = BeautifulSoup(html_chunk, "html.parser")
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
            except (json.JSONDecodeError, TypeError):
                continue
            # A ld+json block legitimately holds a single object, an ARRAY of them, or a
            # {"@graph": [...]} wrapper. The Platters' calendar emits an array, which used to
            # raise AttributeError here and kill the whole artist's aggregation.
            for entry in _ld_json_entries(data):
                start_date = entry.get("startDate", "")
                date_str = start_date[:10]
                if not date_str or date_str < today:
                    continue
                loc = entry.get("location", {})
                if not isinstance(loc, dict):
                    loc = {}
                address = loc.get("address", {})
                if not isinstance(address, dict):
                    address = {}
                loc_name = loc.get("name", "") or address.get("name", "")
                # Unescape HTML entities that may appear in JSON-LD (e.g. &apos; → ')
                import html as _html
                loc_name = _html.unescape(loc_name)
                city, region = _city_state_from_address(loc_name)
                venue = _venue_from_location_name(loc_name)
                if not venue:
                    # Some events leave `location` blank but name the venue/festival in the event
                    # title, e.g. "Calpulli performs at Kaatsbaan 2026 Annual Festival". Fall back
                    # to the text after the last ' at ' so the show isn't dropped as unlocatable.
                    name = _html.unescape(entry.get("name", "") or "")
                    m = re.search(r"\bat\s+(.+)$", name)
                    if m:
                        venue = m.group(1).strip()
                key = f"{date_str}|{venue}|{city}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                shows.append(Show(
                    artist=artist,
                    date=date_str,
                    venue=venue,
                    city=city,
                    region=region,
                    country="US" if region else "",
                    ticket_url="",
                    source="artist_website",
                    start_time=_iso_time(start_date),
                ))

    shows.sort(key=lambda s: s.date)
    log.info("Elfsight calendar: %d shows for %s", len(shows), artist)
    return shows


def _parse_tribe_title_location(title: str) -> tuple[str, str, str, str]:
    """Parse an Events-Calendar-Pro card title into (venue, city, region, country).

    Titles read '<venue>, <city>, <ST>, <country> - <date>' (the city segment is
    sometimes absent, e.g. 'Des Plaines Theatre, IL, USA - ...'). The trailing date
    (after the en/em dash or hyphen) is ignored here — the date comes from the card's
    <time> element.
    """
    # Strip the trailing date ('... - 31 July 2026'); some titles ALSO separate the venue
    # from the city with a spaced dash ('The Ingersoll - Des Moines, IA, USA'), so normalize
    # any remaining spaced dash to a comma before tokenizing on commas.
    left = re.sub(r"\s+[–—-]\s+\d{1,2}\s+[A-Za-z]+\.?\s+\d{4}\s*$", "", title.strip())
    left = re.sub(r"\s+[–—-]\s+", ", ", left)
    tokens = [t.strip() for t in left.split(",") if t.strip()]
    if not tokens:
        return "", "", "", ""
    country = tokens[-1]
    rest = tokens[:-1]
    region = ""
    if rest and rest[-1].upper() in _US_STATE_CODES:
        region = rest[-1].upper()
        rest = rest[:-1]
    if len(rest) >= 2:
        venue, city = rest[0], rest[-1]
    elif len(rest) == 1:
        venue, city = rest[0], ""
    else:
        venue = city = ""
    if country.upper().replace(".", "") in {"USA", "US"} or region:
        country = "US"
    return venue, city, region, country


def _fetch_tribe_map_shows(url: str, artist: str) -> list[Show] | None:
    """Parse an Events-Calendar-Pro 'map' view tour page (no Claude).

    Each show is an `article.tribe-events-pro-map__event-card` with a <time> date, a
    title carrying venue/city/state, and a datetime-wrapper carrying the start time.
    The venue-direct ticket link lives in a separate `--linked` actions div keyed by
    the card's post id. Returns None if the structure isn't found so the caller can
    fall back to the Claude text scrape.
    """
    try:
        from bs4 import BeautifulSoup  # type: ignore

        resp = requests.get(url, timeout=20, headers=_BROWSER_HEADERS)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        log.error("Tribe map fetch error for %s: %s", artist, exc)
        return None

    cards = soup.select("article.tribe-events-pro-map__event-card")
    if not cards:
        return None

    # post id -> venue-direct ticket URL (the '--linked' actions divs carry the real hrefs;
    # the in-card actions are placeholder <span>s).
    link_by_id: dict[str, str] = {}
    for div in soup.select("div.tribe-events-pro-map__event-actions--linked"):
        div_id = div.get("id", "")
        m = re.search(r"event-actions-(\d+)$", div_id)
        if not m:
            continue
        a = div.find("a", attrs={"data-js": "tribe-events-pro-map-event-actions-link-details"})
        if a and a.get("href", "").startswith("http"):
            link_by_id[m.group(1)] = a["href"]

    today = _date.today().isoformat()
    shows: list[Show] = []
    seen: set[str] = set()
    for card in cards:
        time_el = card.find("time")
        date_str = (time_el.get("datetime", "")[:10] if time_el else "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str) or date_str < today:
            continue
        title_el = card.select_one("h3.tribe-events-pro-map__event-title")
        title = title_el.get_text(" ", strip=True) if title_el else ""
        venue, city, region, country = _parse_tribe_title_location(title)

        start_time = ""
        dt_el = card.select_one(".tribe-events-pro-map__event-datetime-wrapper")
        if dt_el:
            tm = re.search(r"\d{1,2}(?::\d{2})?\s*[apAP]\.?\s*[mM]\.?", dt_el.get_text(" ", strip=True))
            if tm:
                start_time = _parse_time_to_24h(tm.group(0))

        post_id = next(
            (c[len("post-"):] for c in card.get("class", []) if c.startswith("post-")),
            "",
        )
        ticket_url = link_by_id.get(post_id, "")

        key = f"{date_str}|{venue}|{city}"
        if key in seen:
            continue
        seen.add(key)
        shows.append(Show(
            artist=artist,
            date=date_str,
            venue=venue,
            city=city,
            region=region,
            country=country,
            ticket_url=ticket_url,
            source="artist_website",
            start_time=start_time,
        ))

    shows.sort(key=lambda s: s.date)
    log.info("Tribe map: %d shows for %s", len(shows), artist)
    return shows


def _extract_prompt(artist: str, page_text: str, has_images: bool = False) -> str:
    """Build the date-extraction prompt shared by the text and vision scrape paths."""
    today_d = _date.today()
    today = today_d.isoformat()
    this_year, next_year = today_d.year, today_d.year + 1
    source_desc = (
        "the text and poster image(s) below, scraped from their official tour page"
        if has_images
        else "the following text scraped from their official tour page"
    )
    image_note = (
        "Some poster images contain the schedule as graphics — extract dates from the images as well as the text. "
        "A poster may lay the schedule out as a grid or table — e.g. a column of ship names or tour codes "
        "beside one or two columns of dates. In such a table, treat EVERY individual date cell as its own "
        "separate show; never merge two dates into a single range and never skip a date. "
        "If a row shows only a date and a ship name or code with no city or state, put that ship name or code "
        "in the venue field and leave city, region, and country empty — do not guess a city or state. "
        if has_images
        else ""
    )
    return (
        f"Extract all upcoming show dates for '{artist}' from {source_desc}. "
        f"Only include shows on or after {today}. "
        f"Today is {today}. Some listings show only a month and day with no year (e.g. 'Saturday, July 11'); "
        f"for each such date use the next occurrence on or after today — {this_year} if that month/day still falls on or after today this year, otherwise {next_year}. "
        "Keep the stated year for any listing that already includes one. "
        "Every listing names a place — a venue, theater, hall, or park, or a named festival/event "
        "(often after the word 'at', e.g. '... at Kaatsbaan 2026 Annual Festival'). ALWAYS put that "
        "name in the venue field; never return an empty venue when the listing names one. If the "
        "city/state is not shown, still KEEP the show — fill the venue and leave city, region, and "
        "country empty rather than dropping it. "
        f"{image_note}"
        "Return ONLY a JSON array of objects using standard JSON syntax — curly braces {{ and }} for objects, square brackets for the array. "
        "Each object must have exactly these keys: date (YYYY-MM-DD), start_time, venue, city, region, country, ticket_url. "
        "For start_time: the show's start time as 'HH:MM' in 24-hour format if it is shown next to the listing; use an empty string if no time is shown. "
        "For ticket_url: use the full URL (must start with 'http') found next to the show listing. "
        "NEVER use link text like 'Buy Tickets', 'Buy Now', or 'Tickets' as the ticket_url value — only use actual URLs. "
        "Use an empty string for ticket_url if no real URL is found for that show. "
        "Do not use markdown, asterisks, or any non-JSON formatting. Do not include any text outside the JSON array.\n\n"
        f"{page_text}"
    )


def _parse_show_json(resp_msg, artist: str) -> list[dict]:
    """Extract the JSON array of show dicts from a Claude response, recovering from truncation."""
    text = ""
    for block in resp_msg.content:
        if hasattr(block, "text"):
            text += block.text

    events = extract_json(text, "[")
    if isinstance(events, list):
        return events

    # Response may have been truncated before the closing ]. Try to recover
    # by finding the last complete object and closing the array.
    start = text.find("[")
    last_brace = text.rfind("}")
    if start != -1 and last_brace != -1 and last_brace > start:
        try:
            events = json.loads(text[start:last_brace + 1] + "]")
            log.warning(
                "Artist website: recovered %d shows from truncated JSON for %s",
                len(events), artist,
            )
            return events
        except json.JSONDecodeError:
            pass

    log.error(
        "Artist website parse error for %s: no JSON array found\nRaw: %s",
        artist, text[:500],
    )
    return []


def _events_to_shows(events: list[dict], artist: str) -> list[Show]:
    """Build Show objects (source=artist_website) from parsed event dicts."""
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
                start_time=str(ev.get("start_time", "") or ""),
            )
        )
    return shows


# Wix CDN image URLs look like
# https://static.wixstatic.com/media/<id>~mv2.png/v1/fill/w_382,h_573,.../file.png
# Truncating at "~mv2.<ext>" yields the full-resolution original (best for OCR).
_WIX_MEDIA_RE = re.compile(
    r"https://static\.wixstatic\.com/media/[^\s\"')]+?~mv2\.(?:png|jpe?g|webp|gif)",
    re.IGNORECASE,
)
_MEDIA_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}


def _collect_poster_images(soup) -> list[str]:
    """Return up to 4 full-resolution Wix image URLs likely to be schedule posters.

    Prefers large portrait images (typical of a tour-date poster) over band photos;
    falls back to the largest images of any orientation when none qualify.
    """
    qualifying: list[tuple[int, str]] = []   # (area, url) — portrait + large
    fallback: list[tuple[int, str]] = []     # (area, url) — any orientation
    seen: set[str] = set()
    for img in soup.find_all("img"):
        urls = [img.get("src", "")]
        srcset = img.get("srcset", "")
        if srcset:
            urls += [part.strip().split(" ")[0] for part in srcset.split(",") if part.strip()]
        media_url = ""
        for u in urls:
            m = _WIX_MEDIA_RE.search(u or "")
            if m:
                media_url = m.group(0)
                break
        if not media_url or media_url in seen:
            continue
        seen.add(media_url)
        try:
            w = int(img.get("width", 0) or 0)
            h = int(img.get("height", 0) or 0)
        except ValueError:
            w = h = 0
        fallback.append((w * h, media_url))
        if h > w and w >= 300:
            qualifying.append((w * h, media_url))
    chosen = qualifying or fallback
    chosen.sort(reverse=True)
    return [u for _, u in chosen[:4]]


def _image_block(img_url: str, artist: str) -> dict | None:
    """Download an image and return a base64 Claude vision content block, or None on failure."""
    try:
        resp = requests.get(img_url, timeout=15, headers=_BROWSER_HEADERS)
        resp.raise_for_status()
    except Exception as exc:
        log.warning("Vision tour: could not download %s for %s: %s", img_url, artist, exc)
        return None
    ext = img_url.rsplit(".", 1)[-1].lower()
    media_type = _MEDIA_TYPES.get(ext, "image/png")
    b64 = base64.standard_b64encode(resp.content).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}}


def _render_html(url: str, artist: str) -> str | None:
    """Return fully rendered HTML of a JS page via Playwright, or None if unavailable."""
    try:
        with browser_page() as page:
            if page is None:
                return None
            page.goto(url, wait_until="load", timeout=30000)
            page.wait_for_timeout(2000)
            return page.content()
    except Exception as exc:
        log.error("Playwright render error for %s: %s", artist, exc)
        return None


def _fetch_image_tour_shows(url: str, artist: str) -> list[Show]:
    """Read a tour page whose dates live inside poster images via Claude vision.

    Sends the page text and the schedule poster image(s) in a single Claude call so
    both text-listed and image-only dates are captured.
    """
    try:
        from bs4 import BeautifulSoup  # type: ignore

        page_resp = requests.get(url, timeout=15, headers=_BROWSER_HEADERS)
        page_resp.raise_for_status()
        html = page_resp.text
    except Exception as exc:
        log.error("Vision tour fetch error for %s: %s", artist, exc)
        return []

    soup = BeautifulSoup(html, "html.parser")
    posters = _collect_poster_images(soup)
    if not posters:
        # Static HTML had no candidate images — render with Playwright and retry.
        rendered = _render_html(url, artist)
        if rendered:
            soup = BeautifulSoup(rendered, "html.parser")
            posters = _collect_poster_images(soup)

    # Page text (reuse the <a> -> "text (url)" rewrite so any text dates keep their links).
    for a in soup.find_all("a", href=True):
        full_href = urljoin(url, a["href"])
        link_text = a.get_text(strip=True)
        a.replace_with(f"{link_text} ({full_href})" if link_text else full_href)
    page_text = re.sub(r"\n{3,}", "\n\n", soup.get_text(separator="\n")).strip()[:32000]

    image_blocks = []
    for img_url in posters:
        block = _image_block(img_url, artist)
        if block:
            image_blocks.append(block)
    log.info("Vision tour: %d poster image(s) for %s: %s", len(image_blocks), artist, posters)

    if not image_blocks and len(page_text) < 200:
        log.warning("Vision tour page for %s had no images and no text, skipping", artist)
        return []

    prompt = _extract_prompt(artist, page_text, has_images=bool(image_blocks))
    content = [{"type": "text", "text": prompt}] + image_blocks

    claude_state._claude_throttle()
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        raw = client.messages.with_raw_response.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_WEBSITE_MAX_TOKENS,
            messages=[{"role": "user", "content": content}],
        )
        resp_msg = raw.parse()
        claude_state._claude_call_count += 1
        claude_state._claude_call_done(dict(raw.headers))
        claude_state._track_cost(resp_msg)
    except Exception as exc:
        log.error("Vision tour Claude parse error for %s: %s", artist, exc)
        return []

    events = _parse_show_json(resp_msg, artist)
    shows = _events_to_shows(events, artist)
    log.info("Vision tour: %d shows for %s", len(shows), artist)
    return shows


def fetch_artist_website(artist: str) -> list[Show]:
    """Scrape the artist's tour page and use Claude to parse dates and ticket links."""
    url = ARTIST_WEBSITES.get(artist, "")
    if not url:
        return []

    # Elfsight/JS-widget pages: parse JSON-LD directly via Playwright (no Claude).
    # If the page has no Elfsight calendar to read, it's still a JS-rendered page, so fall
    # through to the Claude text scrape below against the RENDERED DOM rather than giving up —
    # a static fetch of one of these returns an empty shell.
    render_first = artist in PLAYWRIGHT_RENDER_PAGES
    if render_first:
        shows = _fetch_elfsight_shows(url, artist)
        if shows:
            return shows

    # Events-Calendar-Pro map view: parse cards + venue-direct links directly (no Claude).
    # Falls through to the Claude text scrape if the structure yields nothing.
    if artist in TRIBE_EVENTS_MAP_PAGES:
        shows = _fetch_tribe_map_shows(url, artist)
        if shows:
            return shows

    if not _key_set(ANTHROPIC_API_KEY):
        return []

    # Pages whose dates live inside poster images: read text + images via Claude vision.
    if artist in VISION_TOUR_PAGES:
        return _fetch_image_tour_shows(url, artist)

    try:
        from bs4 import BeautifulSoup  # type: ignore

        if render_first:
            html = _rendered_html(url, artist)
            if not html:
                return []
        else:
            page_resp = requests.get(url, timeout=15, headers=_BROWSER_HEADERS)
            page_resp.raise_for_status()
            html = page_resp.text
        soup = BeautifulSoup(html, "html.parser")

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

    prompt = _extract_prompt(artist, page_text)

    claude_state._claude_throttle()
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        raw = client.messages.with_raw_response.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_WEBSITE_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        resp_msg = raw.parse()
        claude_state._claude_call_count += 1
        claude_state._claude_call_done(dict(raw.headers))
        claude_state._track_cost(resp_msg)
    except Exception as exc:
        log.error("Artist website Claude parse error for %s: %s", artist, exc)
        return []

    events = _parse_show_json(resp_msg, artist)
    shows = _events_to_shows(events, artist)
    log.info("Artist website: %d shows for %s", len(shows), artist)
    return shows
