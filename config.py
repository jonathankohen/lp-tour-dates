import os

from dotenv import load_dotenv

load_dotenv()

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
CLAUDE_CALL_LIMIT = 50  # max Claude calls per run (safety cap)

COST_CAP_USD: float = float(os.environ.get("COST_CAP_USD", "2.00"))
_HAIKU_INPUT_COST_PER_TOKEN  = 1.00 / 1_000_000
_HAIKU_OUTPUT_COST_PER_TOKEN = 5.00 / 1_000_000
_WEB_SEARCH_COST_PER_USE     = 0.01

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
]

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
}


def _display_name(artist: str) -> str:
    return DISPLAY_NAMES.get(artist, artist)


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
BANDSINTOWN_ARTIST_NAMES: dict[str, str] = {
    "A1A: The Original Jimmy Buffett Tribute": "A1A Official Jimmy Buffett Tribute Band",
    "Free Fallin: The Tom Petty Concert Experience": "Free Fallin - The Tom Petty Concert Experience",
    "Kiss The Sky: A Jimi Hendrix Tribute": "id_15607366",
}

# Artists accessible only via their own Bandsintown app_id.
BANDSINTOWN_APP_IDS: dict[str, str] = {
    "Kiss The Sky: A Jimi Hendrix Tribute": "9e91d98985d7c2eadfca1dcba0337f06",
}

# Artists whose tour pages are purely a Bandsintown JS widget (no static HTML dates).
BANDSINTOWN_WIDGET_PAGES: dict[str, str] = {
    "A1A: The Original Jimmy Buffett Tribute": "https://www.a1a-live.com/live.html",
    "Bohemian Queen": "https://www.zennentertainment.com/shows",
    "Free Fallin: The Tom Petty Concert Experience": "https://www.freefallin.us/live",
}

# Output destinations
GOOGLE_SHEETS_ID = os.environ.get("GOOGLE_SHEETS_ID", "")
GOOGLE_DOC_ID = os.environ.get("GOOGLE_DOC_ID", "")
BLOCKING_DOC_ID = os.environ.get("BLOCKING_TEST_ID", "")
OUTPUT_WEBSITE_URL = os.environ.get("OUTPUT_WEBSITE_URL", "")
OUTPUT_JSON_PATH = os.environ.get("OUTPUT_JSON_PATH", "/tmp/tour_dates.json")

# Airtable
AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY", "")
AIRTABLE_BASE_ID = "appMMwX47V1g2Sv5u"   # Love Productions Artists
AIRTABLE_ARTIST_TABLE = "tbloEhiPP4kyTTVDb"  # Artist List
AIRTABLE_PRIORITY_ORDER = ["Top of Roster", "Exclusive", "Core Roster"]
