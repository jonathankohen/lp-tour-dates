"""A show on the Airtable Show Calendar is a fully-executed contract, so it must reach every
output even when no ticketing source knows about it. These tests pin the guarantee: the
filters that legitimately drop speculative data must never drop a contracted show.
"""
import aggregation
from aggregation import (
    CONTRACTED_SOURCE,
    _collapse_contracted_duplicates,
    _filter_by_act_name,
    _filter_web_search_shows,
    _is_locatable,
    _is_us_show,
    dedup_for_publish,
)
from config import band_for_name, is_private_booking
from models import Show
from sources.airtable_calendar import _VENUE_PLACEHOLDER, fetch_airtable_calendar, reset_cache

ARTIST = "The Dolly Show"  # a STRICT US-only act, so the harshest filter applies


def _contract(date="2027-04-23", venue="Orpheum Theater", city="Galesburg", region="IL"):
    return Show(artist=ARTIST, date=date, venue=venue, city=city, region=region,
                country="", ticket_url="", source=CONTRACTED_SOURCE)


def _sourced(date="2027-04-23", venue="The Orpheum", city="Galesburg", source="bandsintown",
             url="https://galesburgorpheum.org/event/the-dolly-show/"):
    return Show(artist=ARTIST, date=date, venue=venue, city=city, region="IL",
                country="US", ticket_url=url, source=source)


# --- the drop paths a contracted show must survive ----------------------------------------

def test_contracted_show_with_no_link_or_location_is_locatable():
    """The unlocatable guard exists to kill poster-scrape noise, not signed contracts."""
    bare = Show(artist=ARTIST, date="2027-04-23", venue=_VENUE_PLACEHOLDER, city="", region="",
                country="", ticket_url="", source=CONTRACTED_SOURCE)
    assert _is_locatable(bare)


def test_contracted_show_survives_the_whole_pipeline_with_nothing_else_to_back_it(monkeypatch):
    """The end-to-end guarantee: a contract nobody has ticketed, with no country label and no
    link, still comes out of aggregate() for a STRICT US-only act."""
    bare = Show(artist=ARTIST, date="2027-04-23", venue="Orpheum Theater", city="Galesburg",
                region="", country="", ticket_url="", source=CONTRACTED_SOURCE)
    # It has no US signal at all, so the strict filter alone WOULD drop it ...
    assert not _is_us_show(bare, strict=True)
    assert ARTIST in aggregation.US_ONLY_ARTISTS

    monkeypatch.setattr(aggregation, "fetch_airtable_calendar", lambda a: [bare])
    for fn in ("fetch_bandsintown", "fetch_seatgeek", "fetch_ticketmaster",
               "fetch_artist_website", "fetch_claude_web_search"):
        monkeypatch.setattr(aggregation, fn, lambda a: [])

    # ... and yet it survives, because contracts are exempt.
    out = aggregation.aggregate(ARTIST, enrich=False)
    assert [(s.date, s.venue) for s in out] == [("2027-04-23", "Orpheum Theater")]


def test_act_name_guard_never_drops_a_contract():
    """No performer field to check, and the calendar keys rows to the act itself."""
    kept = _filter_by_act_name([_contract()], ARTIST)
    assert len(kept) == 1


def test_web_search_filter_leaves_contracts_alone_and_uses_their_dates():
    """A web-search echo of a contracted date is the duplicate that should go, not the contract."""
    contract = _contract(date="2027-04-23")
    echo = Show(artist=ARTIST, date="2027-04-23", venue="Orpheum", city="Galesburg", region="IL",
                country="US", ticket_url="https://example.com/x", source="claude_web_search")
    kept = _filter_web_search_shows([contract, echo], ARTIST)
    assert kept == [contract]


# --- dedup: a contract must not ship a second copy of an already-listed show ---------------

def test_contract_collapses_into_a_richer_record_for_the_same_show():
    """Different venue spelling, same date+city — one show, and the richer record wins."""
    contract, sourced = _contract(), _sourced()
    kept = _collapse_contracted_duplicates([contract, sourced])
    assert kept == [sourced]


def test_contract_survives_when_no_other_source_has_the_date():
    contract = _contract(date="2027-05-01")
    other = _sourced(date="2027-04-23")
    kept = dedup_for_publish([contract, other])
    assert contract in kept and len(kept) == 2


def test_contract_and_a_different_city_same_day_are_kept_as_two_shows():
    """Nothing says these are the same show, so neither is discarded."""
    contract = _contract(city="Galesburg", venue="Orpheum Theater")
    other = _sourced(city="Nashville", venue="Brown County Music Center")
    kept = _collapse_contracted_duplicates([contract, other])
    assert len(kept) == 2


def test_contract_collapses_when_it_has_no_city_to_compare():
    contract = _contract(city="", venue="Orpheum")
    kept = _collapse_contracted_duplicates([contract, _sourced()])
    assert len(kept) == 1 and kept[0].source == "bandsintown"


# --- row -> Show mapping -------------------------------------------------------------------

def test_venue_placeholder_is_not_read_as_a_tba_placeholder():
    """The event publisher drops anything matching /\\btba\\b/; a contract is the opposite."""
    from outputs.wordpress_events import _is_private_or_tba
    assert not _is_private_or_tba(_contract(venue=_VENUE_PLACEHOLDER, city=""))


def test_blank_venue_falls_back_so_the_sheet_round_trip_keeps_the_row(monkeypatch):
    """read_shows_from_sheets skips venueless rows, so a contract must never write one."""
    rows = [
        {"date": "2027-04-23", "venue": "", "city": "Galesburg", "region": "IL",
         "slug": "the-dolly-show", "web_link": "", "record_id": "rec1"},
        {"date": "2027-04-24", "venue": "", "city": "", "region": "",
         "slug": "the-dolly-show", "web_link": "", "record_id": "rec2"},
        {"date": "2027-04-25", "venue": "??", "city": "New Orleans", "region": "LA",
         "slug": "the-dolly-show", "web_link": "", "record_id": "rec3"},
    ]
    reset_cache()
    monkeypatch.setattr("sources.airtable_calendar.fetch_airtable_show_calendar", lambda **_: rows)
    shows = fetch_airtable_calendar(ARTIST)
    reset_cache()
    assert [s.venue for s in shows] == ["Galesburg", _VENUE_PLACEHOLDER, "New Orleans"]
    assert all(s.venue.strip() for s in shows)


def test_venue_cell_is_cleaned_and_a_pasted_url_becomes_the_ticket_link(monkeypatch):
    """Hand-maintained cells carry newlines and pasted venue websites; neither belongs in a
    venue name, and the URL is a better ticket link than nothing."""
    rows = [{"date": "2026-08-28", "venue": "Alaska Raceway \nhttps://www.raceak.com/",
             "city": "Palmer ", "region": " AK ", "slug": "the-dolly-show",
             "web_link": "", "record_id": "rec1"}]
    reset_cache()
    monkeypatch.setattr("sources.airtable_calendar.fetch_airtable_show_calendar", lambda **_: rows)
    show = fetch_airtable_calendar(ARTIST)[0]
    reset_cache()
    assert show.venue == "Alaska Raceway"
    assert show.ticket_url == "https://www.raceak.com/"
    assert (show.city, show.region) == ("Palmer", "AK")


# --- private/corporate bookings: internal yes, public never ------------------------------

def test_private_and_corporate_bookings_are_withheld_from_public_outputs():
    from outputs.wordpress_events import _is_private_or_tba
    for venue, city in [("Private Event", "Detroit"),
                        ("Edmonton Gives (Corporate)", ""),
                        ("Grand Ballroom", "Private Party"),
                        ("On Hold", "Chicago")]:
        show = _contract(venue=venue, city=city)
        assert is_private_booking(show.venue, show.city, show.title), venue
        assert _is_private_or_tba(show), venue


def test_a_real_venue_is_not_mistaken_for_a_private_booking():
    """The test is phrase-based on purpose — a bare \\bprivate\\b would match this venue."""
    from outputs.wordpress_events import _is_private_or_tba
    show = _contract(venue="PrivateBank Theatre", city="Chicago")
    assert not is_private_booking(show.venue, show.city, show.title)
    assert not _is_private_or_tba(show)


def test_front_end_payload_strips_private_bookings(monkeypatch):
    """write_website replaces the whole public dataset, so the filter belongs at that boundary."""
    import outputs.website as website
    posted = {}

    class _Resp:
        def raise_for_status(self): pass

    monkeypatch.setattr(website, "OUTPUT_WEBSITE_URL", "https://example.com/ingest")
    monkeypatch.setattr(website.requests, "post",
                        lambda url, json, headers, timeout: posted.update(json) or _Resp())
    website.write_website([_contract(venue="Orpheum Theater"),
                           _contract(date="2027-04-24", venue="Private Event", city="Detroit")])
    assert [s["venue"] for s in posted["shows"]] == ["Orpheum Theater"]


# --- only countersigned contracts force-publish -------------------------------------------

def test_only_fully_executed_rows_are_returned(monkeypatch):
    """The calendar view still contains deals out for signature; those aren't force-published."""
    import airtable
    records = [
        {"id": "r1", "fields": {"Show Date": "2027-04-23", "Venue": "A",
                                "LPC Contract Status": "(FE) Fully Executed"}},
        {"id": "r2", "fields": {"Show Date": "2027-04-24", "Venue": "B",
                                "LPC Contract Status": "(OFS) Out at Venue for Signature"}},
        {"id": "r3", "fields": {"Show Date": "2027-04-25", "Venue": "C",
                                "LPC Contract Status": "(NATB) Needs Approval to be sent"}},
    ]
    seen = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"records": records}

    def _get(url, headers, params, timeout):
        seen["params"] = params
        return _Resp()

    monkeypatch.setattr(airtable.requests, "get", _get)
    monkeypatch.setattr(airtable, "AIRTABLE_API_KEY", "key")

    rows = airtable.fetch_airtable_show_calendar()
    assert [r["venue"] for r in rows] == ["A"]
    # ... and it must read the calendar VIEW, not the raw booking-pipeline table.
    assert ("view", airtable.AIRTABLE_SHOW_CALENDAR_VIEW) in seen["params"]

    assert len(airtable.fetch_airtable_show_calendar(executed_only=False)) == 3


def test_roster_slug_aliases_map_to_real_acts():
    """These two Show Calendar slugs don't normalize onto the roster name by themselves."""
    assert band_for_name("capulli-mexican-dance-company") == "Calpulli Mex Dance Co."
    assert band_for_name("the-monkee-men") == "Monkee Men"
    assert band_for_name("the-dolly-show") == "The Dolly Show"
    assert band_for_name("vandenberg-the-whitesnake-years") == ""  # genuinely off-roster
