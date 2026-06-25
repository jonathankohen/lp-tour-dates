"""Dedup regression tests.

The MD5 dedup key is artist|date|venue|city, so the same show reported under two slightly
different venue spellings survives as two rows. When those rows share a ticket URL it's
provably the same event, so `_dedup_shows` collapses them (`_collapse_by_ticket_url`).
"""
from aggregation import _dedup_shows, _normalize_ticket_url, _collapse_by_city_venue, dedup_for_publish
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


def test_distinct_shows_in_different_cities_not_collapsed():
    """Genuinely different shows (different city, different venue, different URL) stay separate."""
    a = Show(artist="Bohemian Queen", date=FUTURE, venue="Orpheum Theatre", city="Phoenix",
             region="AZ", country="US", ticket_url="https://example.com/show-1", source="ticketmaster")
    b = Show(artist="Bohemian Queen", date=FUTURE, venue="Rialto Theatre", city="Tucson",
             region="AZ", country="US", ticket_url="https://example.com/show-2", source="ticketmaster")
    assert len(_dedup_shows([a, b])) == 2


def test_venue_spelling_merged_without_url():
    """The CLAUDE.md 'Arcada Theatre' vs 'The Arcada Theater' case: same date+city, no shared URL,
    now collapsed by the fuzzy venue-token pass (they share the distinctive token 'arcada')."""
    a = _bq("Arcada Theatre", "US", "", "bandsintown")
    b = _bq("The Arcada Theater", "US", "", "ticketmaster")
    assert len(_dedup_shows([a, b])) == 1


def _show(artist, date, venue, city, region, url, source):
    return Show(artist=artist, date=date, venue=venue, city=city, region=region,
                country="US", ticket_url=url, source=source)


def test_city_venue_collapse_suquamish():
    """Same show, two sources, different venue spelling + different URL — collapses to one,
    keeping the higher-priority (Bandsintown) record."""
    a = _show("Bohemian Queen", "2026-07-02", "Suquamish Clearwater Casino Resort", "Suquamish",
              "WA", "https://www.bandsintown.com/t/107565465", "Bandsintown")
    b = _show("Bohemian Queen", "2026-07-02", "Suquamish Clearwater Resort Lawn", "Suquamish",
              "WA", "https://clearwatercasino.com", "Ticketmaster")
    out = dedup_for_publish([a, b])
    assert len(out) == 1
    assert out[0].venue == "Suquamish Clearwater Casino Resort"  # Bandsintown wins


def test_city_venue_collapse_yucaipa_subset_name():
    a = _show("Bohemian Queen", "2026-10-02", "Yucaipa Performing Arts Center", "Yucaipa",
              "CA", "https://www.bandsintown.com/t/108152567", "Bandsintown")
    b = _show("Bohemian Queen", "2026-10-02", "Yucaipa Performing Arts Center Indoor Theatre",
              "Yucaipa", "CA", "https://www.ticketmaster.com/x", "Ticketmaster")
    assert len(dedup_for_publish([a, b])) == 1


def test_city_venue_collapse_keeps_distinct_venues():
    """Two genuinely different venues in a city (no shared distinctive token) are NOT merged."""
    a = _show("Reza", "2026-08-01", "The Orpheum", "Phoenix", "AZ", "", "ticketmaster")
    b = _show("Reza", "2026-08-01", "Comerica Theatre", "Phoenix", "AZ", "", "ticketmaster")
    assert len(_collapse_by_city_venue([a, b])) == 2


def test_city_venue_collapse_keeps_different_dates():
    """Same venue, different dates (e.g. a 3-night residency) stays separate."""
    shows = [_show("Bohemian Queen", f"2026-09-0{d}", "South Point Hotel Casino & Spa",
                   "Las Vegas", "NV", "", "bandsintown") for d in (4, 5, 6)]
    assert len(_collapse_by_city_venue(shows)) == 3


def test_collapse_backfills_missing_start_time():
    a = _bq("Venue A", "US", "https://example.com/show", "ticketmaster")  # higher priority, no time
    b = _bq("Venue B", "US", "https://example.com/show", "back2mac_sheets")
    b.start_time = "19:30"
    out = _dedup_shows([a, b])
    assert len(out) == 1
    assert out[0].source == "ticketmaster"
    assert out[0].start_time == "19:30"  # backfilled from the dropped twin
