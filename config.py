import json
import os
import re

from dotenv import load_dotenv

load_dotenv()


def extract_json(text: str, container: str = "["):
    """Extract the first complete JSON array (or object, container="{") from a Claude reply.

    Claude routinely wraps its JSON in prose — a preamble ("Based on my search results…")
    and often a trailing note after the closing bracket. The old `re.search(r"\\[.*\\]", DOTALL)`
    spanned from the first "[" to the LAST "]" anywhere in the reply, so a single bracket in
    that trailing prose made the match swallow non-JSON text and json.loads() failed with
    "Extra data" — which silently cost Back 2 Mac its web-search results on 2026-07-20.

    raw_decode() parses exactly one well-formed value and ignores whatever follows, and it
    tracks nesting correctly (no regex can). Returns None when no valid JSON value is found.
    """
    if not text:
        return None
    text = re.sub(r"```(?:json)?\s*", "", text)
    decoder = json.JSONDecoder()
    start = text.find(container)
    while start != -1:
        try:
            value, _ = decoder.raw_decode(text[start:])
            return value
        except json.JSONDecodeError:
            # That bracket didn't begin a valid value (e.g. prose like "[see below]") —
            # try the next candidate rather than giving up on the whole reply.
            start = text.find(container, start + 1)
    return None

SEATGEEK_CLIENT_ID = os.environ.get("SEATGEEK_CLIENT_ID", "")
TICKETMASTER_API_KEY = os.environ.get("TICKETMASTER_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


def _key_set(val: str) -> bool:
    """Return True only if val is a real key (not empty or a placeholder)."""
    return bool(val) and val != "pending_approval"


def _iso_time(dt: str) -> str:
    """Extract 'HH:MM' from an ISO datetime (e.g. '2026-08-15T19:30:00').

    Returns '' when there is no time component or it is midnight — these APIs
    use 00:00 as a 'time unknown' sentinel.
    """
    if not dt or "T" not in dt:
        return ""
    t = dt[11:16]
    return "" if t in ("", "00:00") else t


def _fmt_time_12h(t: str) -> str:
    """Format a canonical 24-hour 'HH:MM' as 12-hour 'H:MM AM/PM' for display.

    Returns '' for blank input and leaves anything that isn't a plain 'HH:MM'
    unchanged (so it's safe to call on an already-formatted value).
    """
    m = re.match(r"^(\d{1,2}):(\d{2})$", t.strip()) if t else None
    if not m:
        return t or ""
    h, mn = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mn <= 59):
        return t
    return f"{h % 12 or 12}:{mn:02d} {'AM' if h < 12 else 'PM'}"


def _parse_time_to_24h(s: str) -> str:
    """Normalize a time string to canonical 24-hour 'HH:MM'.

    Accepts 12-hour ('8 PM', '7:30 p.m.') or 24-hour ('19:30'); returns '' if it
    can't be parsed. Used when reading times back out of the sheet so the internal
    representation stays 24-hour regardless of how the cell was written or typed.
    """
    if not s:
        return ""
    s = s.strip()
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*([AaPp])\.?\s*[Mm]\.?$", s)  # 12-hour
    if m:
        h, mn, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
        if ap == "p" and h != 12:
            h += 12
        elif ap == "a" and h == 12:
            h = 0
        return f"{h:02d}:{mn:02d}" if 0 <= h <= 23 and 0 <= mn <= 59 else ""
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)  # 24-hour
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        return f"{h:02d}:{mn:02d}" if 0 <= h <= 23 and 0 <= mn <= 59 else ""
    return ""


# Many venue/ticketing URLs embed the show's ISO datetime, e.g.
# https://rezalivetheatre.branson.direct/show/reza/2026-06-25T20:00:00
_URL_DATETIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def _time_from_url(url: str) -> str:
    """Extract 'HH:MM' from an ISO datetime embedded in a URL.

    e.g. '.../reza/2026-06-25T20:00:00' → '20:00'. Returns '' if no datetime is
    present (or it is midnight, which _iso_time treats as a 'time unknown' sentinel).
    """
    if not url:
        return ""
    m = _URL_DATETIME_RE.search(url)
    return _iso_time(m.group(0)) if m else ""


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


# URL paths that are clearly NOT a show's ticket page even if they sit on the venue's
# site and happen to mention the act + a date (e.g. a hotel room-rate calendar contains
# every date). Used to reject such pages during crawling/search adoption.
_NON_TICKET_URL_RE = re.compile(
    r"room-?rates?|/rooms?(?:/|$)|/hotels?(?:/|$)|/lodging|/dining|/restaurants?"
    r"|/menus?(?:/|$)|/careers?|gift-?cards?|/spa(?:/|$)|/golf|/parking|/directions"
    r"|/stay(?:/|$)|/accommodations?",
    re.I,
)


def _is_non_ticket_url(url: str) -> bool:
    return bool(_NON_TICKET_URL_RE.search(url or ""))


def _is_bare_homepage(url: str) -> bool:
    """True if the URL is just a site root (no path/query) — a venue homepage, not an event page."""
    from urllib.parse import urlparse
    p = urlparse(url)
    return p.path.strip("/") == "" and not p.query


# Generic venue words that don't identify a specific venue's domain.
_VENUE_GENERIC = {
    "theatre", "theater", "center", "centre", "casino", "resort", "hotel", "park",
    "hall", "arts", "art", "performing", "amphitheater", "amphitheatre", "fairgrounds",
    "vineyards", "vineyard", "winery", "festival", "stage", "live", "music", "club",
    "lounge", "gaming", "pavilion", "opera", "house", "civic", "convention", "college",
    "university", "community", "downtown", "city", "county", "the", "of", "and", "for",
    "grand", "plaza", "square", "room", "ballroom", "bar", "grill", "cafe", "tavern",
    "fair", "expo", "arena", "stadium", "field", "gardens", "garden", "events", "event",
    "entertainment", "band", "show", "shows",
}

# Third-party ticketing hosts that ARE legitimate event-specific ticket pages even though
# the venue's name isn't in the domain. Matched on domain boundary (not substring), so
# e.g. 'tickets.com' never matches 'gotickets.com'.
_TICKETING_HOSTS = (
    "etix.com", "ovationtix.com", "tickets.com", "showare.com", "seatengine.com",
    "simpletix.com", "ticketleap.com", "tixr.com", "brownpapertickets.com",
    "ticketspice.com", "seetickets.us", "ludus.com", "onthestage.tickets",
    "universitytickets.com",
)

# Resale / aggregator marketplaces — never the venue's own ticket page, hard-rejected.
_RESALE_DOMAINS = (
    "gotickets.com", "buytickets.com", "vividseats.com", "stubhub.com", "tickpick.com",
    "ticketnetwork.com", "ticketliquidator.com", "megaseats.com", "gametime.co",
    "rateyourseats.com", "event-tickets-center.com", "ticketsmarter.com",
)


def _host_matches(host: str, domains) -> bool:
    """True if host equals one of `domains` or is a subdomain of it (boundary-aware)."""
    return any(host == d or host.endswith("." + d) for d in domains)


def _venue_name_tokens(venue: str) -> set[str]:
    """Distinctive lowercased venue tokens (drops generic words like 'theater', 'winery')."""
    return {t for t in re.split(r"[^a-z0-9]+", venue.lower())
            if len(t) >= 4 and t not in _VENUE_GENERIC}


def _acceptable_venue_result(url: str, venue: str) -> bool:
    """A URL is an acceptable venue/event ticket link only if its domain relates to the venue
    (a distinctive venue-name token appears in the host) or it's a known primary ticketing host.
    Resale marketplaces, aggregators, and off-venue pages (the act's own EPK, social, blogs)
    are rejected."""
    from urllib.parse import urlparse
    host = urlparse(url).netloc.lower()
    if _host_matches(host, _RESALE_DOMAINS):
        return False
    if _host_matches(host, _TICKETING_HOSTS):
        return True
    host_alnum = re.sub(r"[^a-z0-9]", "", host)
    return any(tok in host_alnum for tok in _venue_name_tokens(venue))


CLAUDE_MODEL = "claude-haiku-4-5"
CLAUDE_MAX_TOKENS = 4096  # per call — needs room for full JSON list of tour dates
# Date-extraction ceiling for artist-website scrapes only. Tour pages can list many
# dozens of dates (cruise/residency runs); at 4096 the JSON output truncates and the
# tail (often the next-year dates) is dropped. Haiku 4.5 supports 64K output; 16000 is
# the non-streaming-safe default and holds ~250 events. Cost: Haiku output is $5/1M, so
# ≤ ~$0.08/call worst case, and only dense pages approach it.
CLAUDE_WEBSITE_MAX_TOKENS = 16000
CLAUDE_CALL_LIMIT = 50  # max Claude calls per run (safety cap)

COST_CAP_USD: float = float(os.environ.get("COST_CAP_USD", "2.00"))
_HAIKU_INPUT_COST_PER_TOKEN = 1.00 / 1_000_000
_HAIKU_OUTPUT_COST_PER_TOKEN = 5.00 / 1_000_000
_WEB_SEARCH_COST_PER_USE = 0.01

# Skip Claude web search for an artist if non-Claude sources already found this many shows
WEB_SEARCH_SKIP_THRESHOLD = 3

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
    "A Man Named Cash",
    "Vitaly: An Evening of Wonders!",
    # Exclusive / Core Roster artists added from Airtable
    "Back 2 Mac: A Tribute to Fleetwood Mac",
    "Calpulli Mex Dance Co.",
    "Priscilla Presley",
    "The Wankers",
    "Love TKO Teddy Pendergrass",
    "Tony Danza: Standards & Stories",
    "Eagle Wings & More - The Ultimate Tribute Salute w/ End of the Innocence",
    "Always Celine",
    "Reza",
    "Legends of Pop in Concert",
]

# The roster is US-only: non-US dates are dropped for EVERY artist during aggregation
# (see _is_us_show / aggregate in aggregation.py) — EXCEPT the cruise acts below, whose
# schedules are ship itineraries that inherently call on foreign ports.
CRUISE_ACTS: set[str] = {
    "Legends of Classic Rock",
    "Kyle Martin's Piano Man",
}

# STRICT US-only artists: acts that tour abroad with foreign dates that carry NO country/
# region label (e.g. The Dolly Show's UK towns, Arrival's "Sweden/Lithuania TBA"). For these
# a show is kept ONLY if it's positively US — an unlabeled/blank-location show is dropped.
# (Every other non-cruise artist gets the lenient filter: drop only positively-non-US shows,
# so US residencies with blank location columns like Reza are never touched.)
US_ONLY_ARTISTS: set[str] = {
    "The Dolly Show",
    "Arrival From Sweden: The Music of ABBA",
}

# US state names + postal codes + territories, used to positively identify a US show from
# its region/venue text. Region values in the data are a mix of codes ("NY"), full names
# ("Kentucky"), and territories ("Puerto Rico", "St. Thomas").
_US_STATE_CODES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA","KS","KY",
    "LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND",
    "OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY","DC",
}
_US_STATE_NAMES = {
    "ALABAMA","ALASKA","ARIZONA","ARKANSAS","CALIFORNIA","COLORADO","CONNECTICUT","DELAWARE",
    "FLORIDA","GEORGIA","HAWAII","IDAHO","ILLINOIS","INDIANA","IOWA","KANSAS","KENTUCKY",
    "LOUISIANA","MAINE","MARYLAND","MASSACHUSETTS","MICHIGAN","MINNESOTA","MISSISSIPPI",
    "MISSOURI","MONTANA","NEBRASKA","NEVADA","NEWHAMPSHIRE","NEWJERSEY","NEWMEXICO","NEWYORK",
    "NORTHCAROLINA","NORTHDAKOTA","OHIO","OKLAHOMA","OREGON","PENNSYLVANIA","RHODEISLAND",
    "SOUTHCAROLINA","SOUTHDAKOTA","TENNESSEE","TEXAS","UTAH","VERMONT","VIRGINIA","WASHINGTON",
    "WESTVIRGINIA","WISCONSIN","WYOMING","DISTRICTOFCOLUMBIA",
}
# US territories count as US.
_US_TERRITORIES = {
    "PR","PUERTORICO","USVIRGINISLANDS","VIRGININSLANDS","STTHOMAS","STCROIX","STJOHN",
    "GUAM","AMERICANSAMOA","NORTHERNMARIANAISLANDS",
}
_US_REGION_TOKENS = _US_STATE_CODES | _US_STATE_NAMES | _US_TERRITORIES

# WordPress `event_cat` taxonomy terms assigned per act when publishing events.
# Names must match terms that ALREADY EXIST on the live site exactly — the plugin
# only assigns existing terms and never creates new ones, so a typo here is dropped
# rather than spawning a stray category. Acts absent from this map publish with no
# category. Keys are full BAND_NAMES strings (the artist value carried in the
# publish payload), not display names.
EVENT_CATEGORIES: dict[str, list[str]] = {
    "Arrival From Sweden: The Music of ABBA": ["Tributes", "Concerts"],
    "The Dolly Show": ["Tributes", "Concerts"],
    "Kyle Martin's Piano Man": ["Tributes", "Concerts"],
    "The Rocket Man Show": ["Tributes", "Concerts"],
    "A1A: The Original Jimmy Buffett Tribute": ["Tributes", "Concerts"],
    "Bohemian Queen": ["Tributes", "Concerts"],
    "Elvis: The Concert of Kings": ["Tributes", "Concerts"],
    "Free Fallin: The Tom Petty Concert Experience": ["Tributes", "Concerts"],
    "Kiss The Sky: A Jimi Hendrix Tribute": ["Tributes", "Concerts"],
    "Legends of Classic Rock": ["Tributes", "Concerts"],
    "Monkee Men": ["Tributes", "Concerts"],
    "A Man Named Cash": ["Tributes", "Concerts"],
    "Vitaly: An Evening of Wonders!": ["Magic", "Variety", "Family"],
    "Back 2 Mac: A Tribute to Fleetwood Mac": ["Tributes", "Concerts"],
    "Calpulli Mex Dance Co.": ["Dance", "Family"],
    "Priscilla Presley": ["Celebrities", "Talk Series"],
    "The Wankers": ["Tributes", "Concerts"],
    "Love TKO Teddy Pendergrass": ["Tributes", "Concerts"],
    "Tony Danza: Standards & Stories": ["Celebrities", "Cabaret", "Concerts"],
    "Eagle Wings & More - The Ultimate Tribute Salute w/ End of the Innocence": ["Tributes", "Concerts"],
    "Always Celine": ["Tributes", "Concerts"],
    "Reza": ["Magic", "Variety", "Family"],
    "Legends of Pop in Concert": ["Tributes", "Concerts"],
}

# Shorter names used in tab titles and output — full names kept internally for API lookups
DISPLAY_NAMES: dict[str, str] = {
    "Arrival From Sweden: The Music of ABBA": "Arrival From Sweden",
    "Kyle Martin's Piano Man": "Piano Man",
    "The Rocket Man Show": "Rocket Man Show",
    "A1A: The Original Jimmy Buffett Tribute": "A1A",
    "Elvis: The Concert of Kings": "Concert of Kings",
    "Free Fallin: The Tom Petty Concert Experience": "Free Fallin",
    "Kiss The Sky: A Jimi Hendrix Tribute": "Kiss The Sky",
    "Vitaly: An Evening of Wonders!": "Vitaly",
    "Back 2 Mac: A Tribute to Fleetwood Mac": "Back 2 Mac",
    "Love TKO Teddy Pendergrass": "Love TKO",
    "Tony Danza: Standards & Stories": "Tony Danza",
    "Eagle Wings & More - The Ultimate Tribute Salute w/ End of the Innocence": "Eagle Wings & More",
}


def _display_name(artist: str) -> str:
    return DISPLAY_NAMES.get(artist, artist)


# Extra performer/attraction-name phrases that count as a match for an act, beyond the names
# auto-derived from BAND_NAMES/DISPLAY_NAMES. Use when a source legitimately lists the act
# under a subtitle / short form the strict matcher would otherwise reject — e.g. Ticketmaster
# carrying "Elvis: The Concert of Kings" as just "Concert of Kings". Keep each alias
# distinctive (not a lone generic word) so it doesn't readmit the cross-act contamination the
# guard exists to stop. Keys are full BAND_NAMES strings.
ACT_NAME_ALIASES: dict[str, list[str]] = {
    "Arrival From Sweden: The Music of ABBA": ["Arrival from Sweden"],
    "Elvis: The Concert of Kings": ["Concert of Kings"],
}


# Generic words that don't, on their own, identify a tribute/show act. Used to decide
# which name tokens are "distinctive" when confirming a page/performer really is this act.
_ACT_STOPWORDS = {
    "the", "of", "a", "an", "and", "to", "in", "with", "for", "feat", "featuring",
    "presents", "tribute", "show", "shows", "concert", "experience", "original",
    "music", "band", "live", "evening", "ultimate", "salute", "celebration", "starring",
}


def _act_tokens(artist: str) -> set[str]:
    """Distinctive lowercased tokens identifying an act (e.g. 'a1a', 'buffett', 'fleetwood')."""
    names = f"{artist} {_display_name(artist)}".lower()
    return {t for t in re.split(r"[^a-z0-9]+", names) if len(t) >= 3 and t not in _ACT_STOPWORDS}


def _act_name_phrases(artist: str) -> set[str]:
    """Normalized (alnum-only) act name phrases used to confirm a page is really about
    this act — e.g. 'bohemianqueen', 'kissthesky', 'a1a'. Matching the whole name avoids
    false positives from a single common word (e.g. 'Queen' on a hotel room-rate page)."""
    core = re.split(r"[:\-(–—]", artist, 1)[0]  # drop subtitle after ':' / '-' / '('
    phrases = set()
    for n in (_display_name(artist), core, artist):
        norm = re.sub(r"[^a-z0-9]", "", n.lower())
        if len(norm) >= 3:
            phrases.add(norm)
    return phrases


def _act_identity_phrases(artist: str) -> set[str]:
    """Normalized whole-name phrases that DISTINCTIVELY identify an act, for matching a
    candidate performer/attraction name (or page text) against the act.

    Stricter than `_act_name_phrases`: the display name and full name are taken as
    consecutive phrases (e.g. 'bohemianqueen', 'elvisconcertofkings'), and the
    subtitle-stripped 'core' is added only when it is itself multi-word — so a generic
    single word such as 'elvis' or 'queen' never qualifies on its own. A single-word
    phrase survives only when it IS the act's display name (e.g. 'a1a', 'reza', 'vitaly'),
    never as a leftover word from a longer name. This is what enforces the rule that a
    multi-word act name must appear consecutively and in order ('Queen by The Bohemians'
    therefore does NOT match 'Bohemian Queen')."""
    core = re.split(r"[:\-(–—]", artist, 1)[0].strip()
    core_sig = [t for t in re.split(r"[^a-z0-9]+", core.lower()) if t and t not in _ACT_STOPWORDS]
    candidates = [_display_name(artist), artist]
    if len(core_sig) >= 2:
        candidates.append(core)
    candidates.extend(ACT_NAME_ALIASES.get(artist, []))
    phrases = set()
    for n in candidates:
        norm = re.sub(r"[^a-z0-9]", "", n.lower())
        if len(norm) >= 3:
            phrases.add(norm)
    return phrases


def act_name_matches(candidate_name: str, artist: str) -> bool:
    """True when `candidate_name` names THIS act by its full, consecutive name.

    `candidate_name` is a performer/attraction name from a structured source, or a chunk
    of page text. A multi-word act name must appear consecutively and in order, so e.g.
    'Queen by The Bohemians' does NOT match 'Bohemian Queen' even though both words occur.
    An empty candidate returns True (there is nothing to disprove); callers that want to
    drop unverifiable records check for a non-empty candidate first."""
    if not candidate_name:
        return True
    norm = re.sub(r"[^a-z0-9]", "", candidate_name.lower())
    return any(p in norm for p in _act_identity_phrases(artist))


# Short prefixes for Doc subtab titles (must be unique when combined with season/zone label).
# Artists not listed here fall back to _display_name(), which is already short for most.
SUBTAB_PREFIXES: dict[str, str] = {
    "Arrival From Sweden: The Music of ABBA": "AFS",
    "Elvis: The Concert of Kings": "Elvis",
    "Eagle Wings & More - The Ultimate Tribute Salute w/ End of the Innocence": "Eagle Wings",
    "Legends of Classic Rock": "Legends CR",
    "Legends of Pop in Concert": "Legends Pop",
    "Calpulli Mex Dance Co.": "Calpulli",
    "Back 2 Mac: A Tribute to Fleetwood Mac": "Back 2 Mac",
}


def _subtab_prefix(artist: str) -> str:
    return SUBTAB_PREFIXES.get(artist, _display_name(artist))


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
    "A Man Named Cash": "https://www.amannamedcash.com/tour-dates",
    "Vitaly: An Evening of Wonders!": "https://www.eveningofwonders.com/tickets/",
    "Calpulli Mex Dance Co.": "https://calpullidance.org/tour-dates",
    "Priscilla Presley": "https://www.priscillapresley.com/",
    "The Wankers": "https://www.thewankers.net/tours",
    "Love TKO Teddy Pendergrass": "https://teddypendergrassofficial.com/tour-dates/",
    "Tony Danza: Standards & Stories": "https://tonydanza.com/tony-danza-live-show",
    # "Reza": "https://rezalivetheatre.com/shows",
    "Reza": "https://rezalivetheatre.branson.direct/schedule?filter=s-reza",
}

# Bandsintown profile names differ from our internal names for some artists.
BANDSINTOWN_ARTIST_NAMES: dict[str, str] = {
    "A1A: The Original Jimmy Buffett Tribute": "A1A Official Jimmy Buffett Tribute Band",
    "Free Fallin: The Tom Petty Concert Experience": "Free Fallin - The Tom Petty Concert Experience",
    "Kiss The Sky: A Jimi Hendrix Tribute": "id_15607366",
    "Monkee Men": "The Monkee Men - Greatest Monkees Tribute",
}

# App_ids used for the Bandsintown REST lookup. An entry here is what makes
# fetch_bandsintown run the REST path for an artist at all (there is no env default —
# CI passes no Bandsintown secret, so the app_id must be hardcoded, like a registered
# public API identifier). Kiss The Sky is reachable ONLY via its own app_id; Monkee Men
# works with LP's shared app_id but needs the entry (plus its BANDSINTOWN_ARTIST_NAMES
# override for the "The Monkee Men - Greatest Monkees Tribute" profile name).
BANDSINTOWN_APP_IDS: dict[str, str] = {
    "Kiss The Sky: A Jimi Hendrix Tribute": "9e91d98985d7c2eadfca1dcba0337f06",
    "Monkee Men": "20dc6f6c1662659e685dfadbe56333cd",
}

# JS-rendered pages that need Playwright DOM rendering (not Bandsintown — use full-page render).
PLAYWRIGHT_RENDER_PAGES: dict[str, str] = {
    "Calpulli Mex Dance Co.": "https://calpullidance.org/tour-dates",
    # Tour dates live in a JS-loaded WordPress "portfolio grid" (cws_portfolio, admin-ajax);
    # the static HTML has only the "TOUR DATES" heading, so render the DOM before scraping.
    "Monkee Men": "https://monkeemen.com/#tour",
}

# Tour pages whose dates live inside poster images — read via Claude vision.
VISION_TOUR_PAGES: dict[str, str] = {
    "Legends of Classic Rock": "https://www.locrband.com/tour",
}

# Tour pages rendered by The Events Calendar Pro "map" view. The full event list ships
# in the static HTML as `article.tribe-events-pro-map__event-card` elements, and each
# card's venue-direct ticket link lives in a separate `--linked` actions div joined by
# post id — so these are parsed deterministically (no Claude). See
# sources.artist_website._fetch_tribe_map_shows.
TRIBE_EVENTS_MAP_PAGES: dict[str, str] = {
    "The Dolly Show": "https://thedollyshow.com/show-dates-2026-tour/",
}

# Artists whose tour pages are purely a Bandsintown JS widget (no static HTML dates).
BANDSINTOWN_WIDGET_PAGES: dict[str, str] = {
    "A1A: The Original Jimmy Buffett Tribute": "https://www.a1a-live.com/live.html",
    "Bohemian Queen": "https://www.zennentertainment.com/shows",
    "Free Fallin: The Tom Petty Concert Experience": "https://www.freefallin.us/live",
    # /events embeds a Bandsintown widget (app_id=js_www.michaelgriffinescapes.com); the page
    # text is just "No Upcoming Shows", so intercept the widget's /events call instead.
    "Michael Griffin Escapes": "https://www.michaelgriffinescapes.com/events",
    # Bandsintown profile page — Playwright intercepts the same rest.bandsintown.com/events call
    "Back 2 Mac: A Tribute to Fleetwood Mac": "https://www.bandsintown.com/a/14943189-back-2-mac-a-tribute-to-fleetwood-mac",
}

# Output destinations
BACK_2_MAC_SHEETS_ID = os.environ.get("BACK_2_MAC_SHEETS_ID", "")
GOOGLE_SHEETS_ID = os.environ.get("GOOGLE_SHEETS_ID", "")
GOOGLE_DOC_ID = os.environ.get("GOOGLE_DOC_ID", "")
BLOCKING_DOC_ID = os.environ.get("BLOCKING_TEST_ID", "")
OUTPUT_WEBSITE_URL = os.environ.get("OUTPUT_WEBSITE_URL", "")
OUTPUT_WEBSITE_SECRET = os.environ.get("OUTPUT_WEBSITE_SECRET", "")
OUTPUT_JSON_PATH = os.environ.get("OUTPUT_JSON_PATH", "/tmp/tour_dates.json")

# WordPress VS Event List publishing (see outputs/wordpress_events.py).
# The publish endpoint lives in the same Tour Calendar plugin as the ingest
# webhook, so by default we derive its URL from OUTPUT_WEBSITE_URL by swapping
# the trailing /ingest path for /publish-events. Auth reuses OUTPUT_WEBSITE_SECRET.
WORDPRESS_PUBLISH_EVENTS_URL = os.environ.get("WORDPRESS_PUBLISH_EVENTS_URL", "") or (
    OUTPUT_WEBSITE_URL.replace("/ingest", "/publish-events") if OUTPUT_WEBSITE_URL else ""
)
# Duplicate-event reporter/cleanup endpoint, same plugin and auth as above.
WORDPRESS_CLEANUP_DUPLICATES_URL = os.environ.get("WORDPRESS_CLEANUP_DUPLICATES_URL", "") or (
    OUTPUT_WEBSITE_URL.replace("/ingest", "/cleanup-duplicates") if OUTPUT_WEBSITE_URL else ""
)
# Rewrites the body (bio) of existing events for an act from its current Drive
# description — same plugin and auth as above. Used to refresh a bio after the
# act's Drive doc is edited, without recreating events.
WORDPRESS_UPDATE_DESCRIPTIONS_URL = os.environ.get("WORDPRESS_UPDATE_DESCRIPTIONS_URL", "") or (
    OUTPUT_WEBSITE_URL.replace("/ingest", "/update-descriptions") if OUTPUT_WEBSITE_URL else ""
)
# Updates the ticket link (event-link meta + "Venue Website" button) on existing
# events, matched per show by act + date — same plugin and auth as above. Used to push
# corrected venue-direct links onto event posts (incl. drafts) after link verification.
WORDPRESS_UPDATE_LINKS_URL = os.environ.get("WORDPRESS_UPDATE_LINKS_URL", "") or (
    OUTPUT_WEBSITE_URL.replace("/ingest", "/update-links") if OUTPUT_WEBSITE_URL else ""
)
# Sets the featured image on existing events for one or more acts (incl. drafts), matched
# by act — same plugin and auth as above. Used to swap a retired/unlicensed photo across
# every event of an act without recreating them.
WORDPRESS_UPDATE_IMAGES_URL = os.environ.get("WORDPRESS_UPDATE_IMAGES_URL", "") or (
    OUTPUT_WEBSITE_URL.replace("/ingest", "/update-images") if OUTPUT_WEBSITE_URL else ""
)
# Google Drive folder that holds one subfolder per act (named by the act's
# internal name), each containing an image file and a description.txt — used as
# the fallback when no existing event of that act is on the site yet.
WORDPRESS_ASSETS_DRIVE_FOLDER_ID = os.environ.get("WORDPRESS_ASSETS_DRIVE_FOLDER_ID", "")
# Fallback 24-hour start time for shows whose source supplied no time. Empty by
# default — we now carry a per-show start_time, so unknown times stay blank rather
# than being guessed. Set this to e.g. "20:00" to force a default on timeless shows.
WORDPRESS_DEFAULT_EVENT_TIME = os.environ.get("WORDPRESS_DEFAULT_EVENT_TIME", "")

# Airtable
AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY", "")
AIRTABLE_BASE_ID = "appMMwX47V1g2Sv5u"  # Love Productions Artists
AIRTABLE_ARTIST_TABLE = "tbloEhiPP4kyTTVDb"  # Artist List
AIRTABLE_PRIORITY_ORDER = ["Top of Roster", "Exclusive", "Core Roster"]
# Show Calendar (booked/inquiry shows) — used by the Airtable↔WP events audit.
AIRTABLE_SHOW_CALENDAR_BASE_ID = "appXLETHThc0p5MOz"
AIRTABLE_SHOW_CALENDAR_TABLE = "tblK2LMog1WUEv3j0"

# Lists existing WP `event` posts (read-only) for the audit, same plugin/auth as above.
WORDPRESS_LIST_EVENTS_URL = os.environ.get("WORDPRESS_LIST_EVENTS_URL", "") or (
    OUTPUT_WEBSITE_URL.replace("/ingest", "/list-events") if OUTPUT_WEBSITE_URL else ""
)
WORDPRESS_TRASH_EVENTS_URL = os.environ.get("WORDPRESS_TRASH_EVENTS_URL", "") or (
    OUTPUT_WEBSITE_URL.replace("/ingest", "/trash-events") if OUTPUT_WEBSITE_URL else ""
)
