"""Ticketmaster keeps cancelled and postponed events in its search results, on their original
date, flagged only by `dates.status.code`. The source ignored that field, so called-off shows
published as real ones — Bohemian Queen 2026-09-03 and The Platters 2026-10-03, both reported
by the team on 2026-08-04 and both `cancelled` in the API.

The subtlety these tests pin: `offsale` is NOT a cancellation. It means tickets aren't
currently purchasable (sold out, or not yet on sale) — the show is real and must publish.
"""
import sources.ticketmaster as tm


def _event(date, status, name="Bohemian Queen", venue="South Point Showroom"):
    return {
        "name": name,
        "id": f"ev-{date}",
        "url": f"https://www.ticketmaster.com/event/{date}",
        "dates": {"start": {"localDate": date, "localTime": "19:00:00"},
                  "status": {"code": status}},
        "_embedded": {
            "venues": [{"name": venue, "city": {"name": "Las Vegas"},
                        "state": {"stateCode": "NV"}, "country": {"countryCode": "US"}}],
            "attractions": [{"name": name}],
        },
    }


def _fetch(monkeypatch, events, artist="Bohemian Queen"):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"_embedded": {"events": events}}

    monkeypatch.setattr(tm, "TICKETMASTER_API_KEY", "fake-key")
    monkeypatch.setattr(tm, "_key_set", lambda v: True)
    monkeypatch.setattr(tm.requests, "get", lambda *a, **k: _Resp())
    return tm.fetch_ticketmaster(artist)


def test_cancelled_event_is_dropped(monkeypatch):
    """The exact Bohemian Queen 9/3 bug: TM lists it under the correct act, flagged cancelled."""
    shows = _fetch(monkeypatch, [
        _event("2026-09-03", "cancelled"),
        _event("2026-09-04", "onsale"),
    ])
    assert [s.date for s in shows] == ["2026-09-04"]


def test_postponed_event_is_dropped(monkeypatch):
    """A postponed show is not happening on the listed date, so the date must not publish."""
    shows = _fetch(monkeypatch, [_event("2026-09-03", "postponed")])
    assert shows == []


def test_american_spelling_is_dropped(monkeypatch):
    """TM has used both spellings; matching only 'cancelled' would let 'canceled' through."""
    shows = _fetch(monkeypatch, [_event("2026-09-03", "canceled")])
    assert shows == []


def test_offsale_event_is_kept(monkeypatch):
    """offsale = sold out / not yet on sale. The show is real — dropping it would delete
    every sold-out residency night from the calendar."""
    shows = _fetch(monkeypatch, [_event("2026-09-03", "offsale")])
    assert [s.date for s in shows] == ["2026-09-03"]


def test_rescheduled_event_is_kept(monkeypatch):
    """A rescheduled record already carries the NEW date, so it's a real upcoming show."""
    shows = _fetch(monkeypatch, [_event("2026-09-03", "rescheduled")])
    assert [s.date for s in shows] == ["2026-09-03"]


def test_missing_status_is_kept(monkeypatch):
    """No status block must never mean 'cancelled' — absence of evidence isn't a cancellation."""
    ev = _event("2026-09-03", "onsale")
    del ev["dates"]["status"]
    shows = _fetch(monkeypatch, [ev])
    assert [s.date for s in shows] == ["2026-09-03"]
