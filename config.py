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
OUTPUT_JSON_PATH = os.environ.get("OUTPUT_JSON_PATH", "/tmp/tour_dates.json")

# Airtable
AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY", "")
AIRTABLE_BASE_ID = "appMMwX47V1g2Sv5u"  # Love Productions Artists
AIRTABLE_ARTIST_TABLE = "tbloEhiPP4kyTTVDb"  # Artist List
AIRTABLE_PRIORITY_ORDER = ["Top of Roster", "Exclusive", "Core Roster"]
