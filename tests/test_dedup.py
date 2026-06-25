"""Dedup regression tests.

The MD5 dedup key is artist|date|venue|city, so the same show reported under two slightly
different venue spellings survives as two rows. When those rows share a ticket URL it's
provably the same event, so `_dedup_shows` collapses them (`_collapse_by_ticket_url`).
"""
from aggregation import _dedup_shows, _normalize_ticket_url
from models import Show

FUTURE = "2099-07-02"  # far future so the date filter keeps it


def _bq(venue, country, url, source):
    return Show(artist="Bohemian Queen", date=FUTURE, venue=venue, city="Suquamish",
                region="WA", country=country, ticket_url=url, source=source)


def test_same_url_different_venue_spelling_collapsed():
    """The exact spreadsheet duplicate: same date/city/URL, venue + country spelled differently."""
    a = _bq("Suquamish Clearwater Casino Resort", "United States",
            "https://clearwatercasino.com/resort/packages-specials/", "ticketmaster")
    b = _bq("Suquamish Clearwater Resort Lawn", "US",
            "https://clearwatercasino.com/resort/packages-specials/", "bandsintown")
    out = _dedup_shows([a, b])
    assert len(out) == 1
    assert out[0].source == "bandsintown"  # higher-priority source wins


def test_url_normalization_ignores_scheme_www_trailing_slash():
    a = _bq("Venue A", "US", "https://clearwatercasino.com/resort/packages-specials/", "ticketmaster")
    b = _bq("Venue B", "US", "http://www.clearwatercasino.com/resort/packages-specials", "ticketmaster")
    assert _normalize_ticket_url(a.ticket_url) == _normalize_ticket_url(b.ticket_url)
    assert len(_dedup_shows([a, b])) == 1


def test_different_urls_not_collapsed():
    a = _bq("Venue A", "US", "https://example.com/show-1", "ticketmaster")
    b = _bq("Venue B", "US", "https://example.com/show-2", "ticketmaster")
    assert len(_dedup_shows([a, b])) == 2


def test_no_url_shows_not_merged():
    """Without a shared URL we can't be sure two venue spellings are the same show — keep both."""
    a = _bq("Arcada Theatre", "US", "", "bandsintown")
    b = _bq("The Arcada Theater", "US", "", "ticketmaster")
    assert len(_dedup_shows([a, b])) == 2


def test_collapse_backfills_missing_start_time():
    a = _bq("Venue A", "US", "https://example.com/show", "ticketmaster")  # higher priority, no time
    b = _bq("Venue B", "US", "https://example.com/show", "back2mac_sheets")
    b.start_time = "19:30"
    out = _dedup_shows([a, b])
    assert len(out) == 1
    assert out[0].source == "ticketmaster"
    assert out[0].start_time == "19:30"  # backfilled from the dropped twin
