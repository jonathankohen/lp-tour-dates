"""Regression tests for the act-name guard at the source/aggregation seam.

These reproduce the original bug end-to-end with canned API payloads (no network): a fuzzy
Ticketmaster keyword search for "Bohemian Queen" returns a "Queen by The Bohemians" event,
and the guard must drop it while keeping the genuine show.
"""
import pytest

import aggregation
from aggregation import _filter_by_act_name
from sources import ticketmaster, seatgeek


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _tm_event(attraction_name, date, venue, eid):
    return {
        "id": eid,
        "name": attraction_name,
        "url": f"https://www.ticketmaster.com/event/{eid}",
        "dates": {"start": {"localDate": date, "localTime": "20:00:00"}},
        "_embedded": {
            "venues": [{"name": venue, "city": {"name": "Las Vegas"},
                         "state": {"stateCode": "NV"}, "country": {"countryCode": "US"}}],
            "attractions": [{"name": attraction_name}],
        },
    }


def test_ticketmaster_drops_similarly_named_act(monkeypatch):
    """fetch_ticketmaster captures the real attraction name; the guard drops the impostor."""
    payload = {"_embedded": {"events": [
        _tm_event("Bohemian Queen", "2026-08-01", "Real Theater", "EV1"),
        _tm_event("Queen by The Bohemians", "2026-08-15", "Other Hall", "EV2"),
    ]}}
    monkeypatch.setattr(ticketmaster, "TICKETMASTER_API_KEY", "test-key")
    monkeypatch.setattr(ticketmaster.requests, "get", lambda *a, **k: _FakeResp(payload))

    shows = ticketmaster.fetch_ticketmaster("Bohemian Queen")
    assert {s.performer for s in shows} == {"Bohemian Queen", "Queen by The Bohemians"}

    kept = _filter_by_act_name(shows, "Bohemian Queen")
    assert [s.venue for s in kept] == ["Real Theater"]
    assert all(s.performer == "Bohemian Queen" for s in kept)


def test_ticketmaster_matches_on_event_name_when_attraction_is_a_variant(monkeypatch):
    """Real case: TM files The Dolly Show under a mangled attraction ('Dolly the Show') while
    the EVENT title names the act correctly. The show must be kept via the event name, and a
    genuinely different act (Legends In Concert) must still be dropped."""
    def ev(name, attraction, date, venue, eid):
        d = _tm_event(attraction, date, venue, eid)  # sets attraction + a placeholder name
        d["name"] = name                              # override with the real event title
        return d
    payload = {"_embedded": {"events": [
        ev("The Dolly Show starring Kelly O'Brien", "Dolly the Show", "2026-07-31", "The Ingersoll", "D1"),
        ev("Christmas with Dolly - Tribute Show", "Legends In Concert", "2026-12-04", "Hollywood Casino", "D2"),
    ]}}
    monkeypatch.setattr(ticketmaster, "TICKETMASTER_API_KEY", "test-key")
    monkeypatch.setattr(ticketmaster.requests, "get", lambda *a, **k: _FakeResp(payload))

    shows = ticketmaster.fetch_ticketmaster("The Dolly Show")
    kept = _filter_by_act_name(shows, "The Dolly Show")
    assert [s.venue for s in kept] == ["The Ingersoll"]


def test_seatgeek_drops_similarly_named_act(monkeypatch):
    payload = {"events": [
        {"id": 1, "datetime_local": "2026-09-10T20:00:00", "time_tbd": False,
         "url": "https://seatgeek.com/e/1",
         "venue": {"name": "Real Theater", "city": "Reno", "state": "NV", "country": "US"},
         "performers": [{"name": "Bohemian Queen"}]},
        {"id": 2, "datetime_local": "2026-09-20T20:00:00", "time_tbd": False,
         "url": "https://seatgeek.com/e/2",
         "venue": {"name": "Other Hall", "city": "Reno", "state": "NV", "country": "US"},
         "performers": [{"name": "Queen by The Bohemians"}]},
    ]}
    monkeypatch.setattr(seatgeek, "SEATGEEK_CLIENT_ID", "test-id")
    monkeypatch.setattr(seatgeek.requests, "get", lambda *a, **k: _FakeResp(payload))

    shows = seatgeek.fetch_seatgeek("Bohemian Queen")
    kept = _filter_by_act_name(shows, "Bohemian Queen")
    assert [s.venue for s in kept] == ["Real Theater"]


def test_bandsintown_widget_multiact_filtered():
    """Zenn-style multi-act widget: every event is stamped artist='Bohemian Queen' but carries
    its real act in performer (from the event title + lineup). The guard keeps only Bohemian
    Queen and drops the agency's other acts."""
    from models import Show

    def bq(performer, venue):
        return Show(artist="Bohemian Queen", date="2099-07-02", venue=venue, city="X",
                    region="WA", country="US", ticket_url="", source="bandsintown", performer=performer)

    shows = [
        bq("BOHEMIAN QUEEN @ Clearwater Casino Zenn Entertainment LLC Bohemian Queen", "Clearwater"),
        bq("THE Z STREET BAND @ Bluewater Casino Zenn Entertainment LLC The Z Street Band", "Bluewater"),
        bq("SEPARATE JOURNEYS @ Murray Theater Zenn Entertainment LLC", "Murray"),
        bq("AEROWINGS @ Pitman Zenn Entertainment LLC Aerowings", "Pitman"),
    ]
    kept = _filter_by_act_name(shows, "Bohemian Queen")
    assert [s.venue for s in kept] == ["Clearwater"]


def test_structured_show_without_performer_is_kept():
    """Bandsintown/website shows carry no performer name — they must not be dropped."""
    from models import Show
    s = Show(artist="Bohemian Queen", date="2026-08-01", venue="V", city="C", region="NV",
             country="US", ticket_url="", source="bandsintown", performer="")
    assert _filter_by_act_name([s], "Bohemian Queen") == [s]


def test_web_search_show_dropped_when_page_disconfirms(monkeypatch):
    """A web-search show whose ticket page loads but never names the act is dropped."""
    from models import Show
    s = Show(artist="Bohemian Queen", date="2026-08-01", venue="V", city="C", region="NV",
             country="US", ticket_url="https://example.com/queen-by-the-bohemians",
             source="claude_web_search")
    monkeypatch.setattr(aggregation, "fetch_page_text",
                        lambda *a, **k: "tickets for queen by the bohemians live in concert")
    assert _filter_by_act_name([s], "Bohemian Queen") == []


def test_web_search_show_kept_when_page_confirms(monkeypatch):
    from models import Show
    s = Show(artist="Bohemian Queen", date="2026-08-01", venue="V", city="C", region="NV",
             country="US", ticket_url="https://example.com/show",
             source="claude_web_search")
    monkeypatch.setattr(aggregation, "fetch_page_text",
                        lambda *a, **k: "bohemian queen live - a tribute to queen, aug 1")
    assert _filter_by_act_name([s], "Bohemian Queen") == [s]


def test_web_search_rendered_page_rescues_js_only_act_name(monkeypatch):
    """Static HTML misses the act name (JS page) but a browser render finds it — keep the show."""
    from models import Show
    s = Show(artist="Arrival From Sweden: The Music of ABBA", date="2026-07-05", venue="V",
             city="Uncasville", region="CT", country="US",
             ticket_url="https://mohegansun.com/show", source="claude_web_search")

    def fake_fetch(url, render=False, force_render=False):
        if force_render:                       # browser render exposes the JS-injected name
            return "arrival from sweden - the music of abba, july 5"
        return "buy tickets seating chart parking info " * 40  # static: lots of text, no act name

    monkeypatch.setattr(aggregation, "fetch_page_text", fake_fetch)
    assert _filter_by_act_name([s], "Arrival From Sweden: The Music of ABBA") == [s]


def test_web_search_dropped_when_rendered_page_also_disconfirms(monkeypatch):
    """If neither the static nor the rendered page names the act, drop it."""
    from models import Show
    s = Show(artist="Bohemian Queen", date="2026-08-01", venue="V", city="C", region="NV",
             country="US", ticket_url="https://example.com/other-band", source="claude_web_search")
    monkeypatch.setattr(aggregation, "fetch_page_text",
                        lambda url, render=False, force_render=False: "a totally different concert")
    assert _filter_by_act_name([s], "Bohemian Queen") == []


def test_web_search_show_kept_when_page_unreachable(monkeypatch):
    """No page text (unreachable / no URL) can't disprove the show — keep it, audit flags it."""
    from models import Show
    s = Show(artist="Bohemian Queen", date="2026-08-01", venue="V", city="C", region="NV",
             country="US", ticket_url="https://example.com/down",
             source="claude_web_search")
    monkeypatch.setattr(aggregation, "fetch_page_text", lambda *a, **k: "")
    assert _filter_by_act_name([s], "Bohemian Queen") == [s]


def test_unlocatable_show_is_dropped():
    """A show with only a date + cryptic venue token (no city/region/country/link) is
    unactionable noise (e.g. an unresolved cruise-ship code from the poster-vision scrape)
    and must be dropped."""
    from aggregation import _is_locatable
    from models import Show
    junk = Show(artist="Legends of Classic Rock", date="2027-01-03", venue="ST",
                city="", region="", country="", ticket_url="", source="artist_website")
    assert _is_locatable(junk) is False


def test_located_or_linked_shows_are_kept():
    """Anything with a location OR a ticket link is publishable and kept."""
    from aggregation import _is_locatable
    from models import Show
    by_city = Show(artist="Legends of Classic Rock", date="2026-06-24", venue="",
                   city="Charlotte Amalie", region="St. Thomas", country="US Virgin Islands",
                   ticket_url="", source="artist_website")
    by_country = Show(artist="Arrival From Sweden", date="2026-12-11", venue="TBA",
                      city="", region="", country="Sweden", ticket_url="", source="artist_website")
    by_link = Show(artist="X", date="2026-07-25", venue="Burlington County Amp", city="",
                   region="New Jersey", country="USA",
                   ticket_url="https://www.co.burlington.nj.us/935/Amphitheater", source="artist_website")
    assert all(_is_locatable(s) for s in (by_city, by_country, by_link))


def _ws_show(date, venue, city, source, url=""):
    from models import Show
    return Show(artist="Arrival From Sweden: The Music of ABBA", date=date, venue=venue,
                city=city, region="NH", country="US", ticket_url=url, source=source)


def test_web_search_dropped_on_already_listed_date():
    """A web-search show on a date another source already covers is a duplicate (the same
    night under a different venue/city name) and must be dropped — even if it has a link."""
    from aggregation import _filter_web_search_shows
    real = _ws_show("2026-07-11", "Great Waters Music Festival", "Wolfeboro",
                    "artist_website", "https://ci.ovationtix.com/37020/production/1262776")
    dupe = _ws_show("2026-07-11", "Concerts in the Clouds", "Moultonborough",
                    "claude_web_search", "https://example.com/tix")
    kept = _filter_web_search_shows([real, dupe], real.artist)
    assert real in kept and dupe not in kept


def test_web_search_dropped_without_ticket_link():
    """A web-search show on a genuinely new date but with no ticket link is not actionable."""
    from aggregation import _filter_web_search_shows
    linkless = _ws_show("2026-07-15", "Some Venue", "Nowhere", "claude_web_search", "")
    assert _filter_web_search_shows([linkless], linkless.artist) == []


def test_web_search_kept_on_new_ticketed_date():
    """A web-search show that adds a NEW date and carries a link is kept."""
    from aggregation import _filter_web_search_shows
    listed = _ws_show("2026-07-11", "Great Waters", "Wolfeboro", "ticketmaster",
                      "https://tm.com/e/1")
    fresh = _ws_show("2026-07-19", "New Hall", "Concord", "claude_web_search",
                     "https://newhall.com/tickets")
    kept = _filter_web_search_shows([listed, fresh], fresh.artist)
    assert fresh in kept and listed in kept


def test_named_venue_without_city_is_kept():
    """A real named venue/festival with no city (e.g. Calpulli's Kaatsbaan festival, whose
    calendar entry has no location) must be kept — only cryptic codes get dropped."""
    from aggregation import _is_locatable
    from models import Show
    kaatsbaan = Show(artist="Calpulli Mex Dance Co.", date="2026-08-29",
                     venue="Kaatsbaan 2026 Annual Festival", city="", region="", country="",
                     ticket_url="", source="artist_website")
    assert _is_locatable(kaatsbaan) is True


def test_cryptic_venue_code_still_dropped():
    """The 'ST'/'IC' cruise-code noise (no real word in the venue) is still dropped."""
    from aggregation import _is_locatable
    from models import Show
    for code in ("ST", "IC"):
        junk = Show(artist="Legends of Classic Rock", date="2027-01-03", venue=code,
                    city="", region="", country="", ticket_url="", source="artist_website")
        assert _is_locatable(junk) is False
