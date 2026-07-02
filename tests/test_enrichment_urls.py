"""Regression tests for enrichment URL adoption (`_should_adopt_enrichment_url`).

The bug: Bandsintown-sourced shows store a stable `bandsintown.com/e/<id>` event page, which
counts as a platform URL, so enrichment ran on them and overwrote the good event link with
whatever Claude returned — including venue homepages and the act's own EPK (real Kiss the Sky
cases: labellewinery.com, kisstheskypress.com/epk, showboatevents.com). Enrichment must only
REPLACE an existing link with an event-specific venue page, never downgrade to a homepage.
"""
from enrichment import _should_adopt_enrichment_url
from config import _is_bare_homepage
from models import Show


def _show(venue, ticket_url):
    return Show(artist="Kiss The Sky: A Jimi Hendrix Tribute", date="2026-07-23", venue=venue,
                city="Derry", region="NH", country="US", ticket_url=ticket_url, source="bandsintown")


BIT_EVENT = "https://www.bandsintown.com/e/108120859?app_id=x&utm_campaign=event"


def test_bare_homepage_does_not_replace_bandsintown_event():
    """labellewinery.com (homepage) must NOT replace the Bandsintown event page."""
    show = _show("Labelle Winery Performance Series", BIT_EVENT)
    assert _should_adopt_enrichment_url("https://labellewinery.com", show) is False


def test_off_venue_epk_does_not_replace_event():
    """The act's own EPK (host unrelated to the venue) must be rejected."""
    show = _show("Allen Theatre & Backstage Cafe", BIT_EVENT)
    assert _should_adopt_enrichment_url("https://www.kisstheskypress.com/epk", show) is False


def test_venue_homepage_with_trailing_slash_rejected():
    show = _show("Showboat Atlantic City", BIT_EVENT)
    assert _should_adopt_enrichment_url("https://showboatevents.com/", show) is False


def test_event_specific_venue_page_replaces():
    """A real event page on the venue's own domain IS a genuine upgrade and is adopted."""
    show = _show("Labelle Winery Performance Series", BIT_EVENT)
    assert _should_adopt_enrichment_url(
        "https://labellewinery.com/events/kiss-the-sky-2026-07-23", show) is True


def test_platform_url_never_adopted():
    show = _show("Showboat Atlantic City", BIT_EVENT)
    assert _should_adopt_enrichment_url(
        "https://www.ticketmaster.com/event/abc", show) is False


def test_homepage_accepted_when_no_existing_link():
    """With no link at all, a venue homepage beats nothing."""
    show = _show("Labelle Winery Performance Series", "")
    assert _should_adopt_enrichment_url("https://labellewinery.com", show) is True


def test_is_bare_homepage():
    assert _is_bare_homepage("https://x.com") is True
    assert _is_bare_homepage("https://x.com/") is True
    assert _is_bare_homepage("https://x.com/events/show-1") is False
    assert _is_bare_homepage("https://x.com/?event=5") is False
