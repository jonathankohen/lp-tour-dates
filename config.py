import os
import re

from dotenv import load_dotenv

load_dotenv()

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
    "Elvis: The Concert of Kings": "Elvis: Concert of Kings",
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
    "Vitaly: An Evening of Wonders!": "https://www.eveningofwonders.com/tickets/",
    "Calpulli Mex Dance Co.": "https://calpullidance.org/tour-dates",
    "Priscilla Presley": "https://www.priscillapresley.com/world-exclusive/",
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
}

# Artists accessible only via their own Bandsintown app_id.
BANDSINTOWN_APP_IDS: dict[str, str] = {
    "Kiss The Sky: A Jimi Hendrix Tribute": "9e91d98985d7c2eadfca1dcba0337f06",
}

# JS-rendered pages that need Playwright DOM rendering (not Bandsintown — use full-page render).
PLAYWRIGHT_RENDER_PAGES: dict[str, str] = {
    "Calpulli Mex Dance Co.": "https://calpullidance.org/tour-dates",
}

# Tour pages whose dates live inside poster images — read via Claude vision.
VISION_TOUR_PAGES: dict[str, str] = {
    "Legends of Classic Rock": "https://www.locrband.com/tour",
}

# Artists whose tour pages are purely a Bandsintown JS widget (no static HTML dates).
BANDSINTOWN_WIDGET_PAGES: dict[str, str] = {
    "A1A: The Original Jimmy Buffett Tribute": "https://www.a1a-live.com/live.html",
    "Bohemian Queen": "https://www.zennentertainment.com/shows",
    "Free Fallin: The Tom Petty Concert Experience": "https://www.freefallin.us/live",
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
