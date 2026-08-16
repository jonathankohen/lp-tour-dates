"""A Google Calendar 'add to calendar' link is NEVER a ticket link.

Tour pages and ticketing widgets render one right next to the real ticket button, so a
scrape, a web search, or a hand-edited Sheet cell can easily put one in the ticket slot —
where it sends the reader to their own calendar instead of to tickets. This locks the ban
at every point a ticket URL enters the pipeline.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import is_calendar_link, _is_non_ticket_url  # noqa: E402
from models import Show  # noqa: E402


CALENDAR_LINKS = [
    "https://calendar.google.com/calendar/render?action=TEMPLATE&text=Show&dates=20260815T000000Z",
    "https://calendar.google.com/calendar/u/0/r/eventedit",
    "http://www.google.com/calendar/event?action=TEMPLATE&text=Reza",
    "https://google.com/calendar/render?action=TEMPLATE",
    "https://calendar.google.co.uk/calendar/render?action=TEMPLATE",
]

REAL_TICKET_LINKS = [
    "https://www.arcadalive.com/events/the-platters",
    "https://www.ticketmaster.com/event/12345",
    "https://tixr.com/e/98765",
    "https://www.etix.com/ticket/p/1234567/reza",
    # A venue whose own name contains 'calendar'-adjacent words must still pass.
    "https://calendarhall.com/events/2026-08-15",
    "https://venue.com/calendar/2026-08-15-the-dolly-show",
]


def test_calendar_links_are_detected():
    for url in CALENDAR_LINKS:
        assert is_calendar_link(url), url


def test_real_ticket_links_are_not_calendar_links():
    for url in REAL_TICKET_LINKS:
        assert not is_calendar_link(url), url


def test_empty_url_is_not_a_calendar_link():
    assert not is_calendar_link("")
    assert not is_calendar_link(None)


def test_calendar_links_count_as_non_ticket_urls():
    """Folding into _is_non_ticket_url is what bans them from every adoption/preservation
    path (enrichment, deep crawl, Sheet URL preservation) in one place."""
    for url in CALENDAR_LINKS:
        assert _is_non_ticket_url(url), url


def test_aggregation_strips_calendar_links_at_collection():
    from aggregation import _strip_calendar_links

    shows = [
        Show(artist="A", date="2026-09-01", venue="V", city="C", region="ST", country="US",
             ticket_url=CALENDAR_LINKS[0], source="artist_website"),
        Show(artist="A", date="2026-09-02", venue="V2", city="C", region="ST", country="US",
             ticket_url=REAL_TICKET_LINKS[0], source="artist_website"),
    ]
    _strip_calendar_links(shows)
    assert shows[0].ticket_url == ""
    assert shows[1].ticket_url == REAL_TICKET_LINKS[0]


def test_enrichment_never_adopts_a_calendar_link():
    """Even a show with NO link at all must not take one — 'something beats nothing'
    does not apply to a link that can never sell a ticket."""
    from enrichment import _should_adopt_enrichment_url

    linkless = Show(artist="A", date="2026-09-01", venue="The Arcada Theatre", city="C",
                    region="ST", country="US", ticket_url="", source="artist_website")
    linked = Show(artist="A", date="2026-09-01", venue="The Arcada Theatre", city="C",
                  region="ST", country="US", ticket_url="https://bandsintown.com/e/1",
                  source="bandsintown")
    for url in CALENDAR_LINKS:
        assert not _should_adopt_enrichment_url(url, linkless), url
        assert not _should_adopt_enrichment_url(url, linked), url


def test_sheet_url_preservation_skips_calendar_links():
    from outputs.sheets import _ticket_urls_from_rows

    rows = [
        ["Date", "Venue", "City", "Region", "Country", "Ticket URL", "Source", "Time"],
        ["09/01/26", "The Arcada Theatre", "St. Charles", "IL", "US", CALENDAR_LINKS[0], "x", ""],
        ["09/02/26", "The Arcada Theatre", "St. Charles", "IL", "US", REAL_TICKET_LINKS[0], "x", ""],
    ]
    saved = _ticket_urls_from_rows(rows, "A")
    assert list(saved.values()) == [REAL_TICKET_LINKS[0]]
