"""Residency collapse regression tests.

`outputs.wordpress_events._collapse_residencies` reshapes ONLY the event-post payload:
an act playing one venue 4+ times (across the upcoming shows) is a residency, and its
shows collapse into ONE date-range event per calendar month. Below the threshold, or at
a genuinely different venue, shows pass through one-per-show as before.
"""
from outputs.wordpress_events import _collapse_residencies, _RESIDENCY_MIN_SHOWS
from models import Show


def _show(venue, date, *, artist="Piano Man", city="Las Vegas", region="NV",
          url="", start_time="", source="bandsintown"):
    return Show(artist=artist, date=date, venue=venue, city=city, region=region,
                country="US", ticket_url=url, source=source, start_time=start_time)


def test_residency_collapses_to_one_event_per_month():
    """5 shows at one venue across two months -> 2 range events (one per month)."""
    shows = [
        _show("South Point Casino", "2026-07-05", start_time="19:00", url="https://sp.com/a"),
        _show("South Point Casino", "2026-07-12", start_time="20:00"),
        _show("South Point Casino", "2026-07-26", start_time="19:00"),
        _show("South Point Casino", "2026-08-02", start_time="19:00"),
        _show("South Point Casino", "2026-08-09", start_time="19:00"),
    ]
    out, meta = _collapse_residencies(shows)
    assert len(out) == 2  # July + August

    july = next(s for s in out if s.date.startswith("2026-07"))
    aug = next(s for s in out if s.date.startswith("2026-08"))

    jm = meta[id(july)]
    assert july.date == "2026-07-05"         # range start = earliest that month
    assert jm["is_residency"] is True
    assert jm["end_date"] == "2026-07-26"     # range end = latest that month
    assert [d["date"] for d in jm["residency_dates"]] == ["2026-07-05", "2026-07-12", "2026-07-26"]
    assert jm["residency_dates"][0]["start_time"] == "7:00 PM"  # 12h, from earliest show
    assert july.start_time == ""              # omitted on the event itself

    am = meta[id(aug)]
    assert aug.date == "2026-08-02"
    assert am["end_date"] == "2026-08-09"


def test_below_threshold_untouched():
    """3 shows (threshold is 4) stay as individual shows with no residency meta."""
    shows = [_show("South Point Casino", f"2026-07-0{d}") for d in (5, 6, 7)]
    assert len(shows) == _RESIDENCY_MIN_SHOWS - 1
    out, meta = _collapse_residencies(shows)
    assert len(out) == 3
    assert meta == {}
    assert out == shows  # same objects, passed through


def test_venue_spelling_variants_group_together():
    """Same venue under different spellings still counts toward the residency threshold."""
    shows = [
        _show("South Point Hotel Casino & Spa", "2026-07-05"),
        _show("South Point Casino", "2026-07-12"),
        _show("The South Point Casino Showroom", "2026-07-19"),
        _show("South Point Hotel & Casino", "2026-07-26"),
    ]
    out, meta = _collapse_residencies(shows)
    assert len(out) == 1                       # all four collapse into one July event
    assert meta[id(out[0])]["end_date"] == "2026-07-26"
    assert len(meta[id(out[0])]["residency_dates"]) == 4


def test_distinct_venues_not_collapsed():
    """4 shows split across two genuinely different venues -> neither is a residency."""
    shows = [
        _show("Orpheum Theatre", "2026-07-05", city="Phoenix", region="AZ"),
        _show("Orpheum Theatre", "2026-07-12", city="Phoenix", region="AZ"),
        _show("Comerica Theatre", "2026-07-06", city="Phoenix", region="AZ"),
        _show("Comerica Theatre", "2026-07-13", city="Phoenix", region="AZ"),
    ]
    out, meta = _collapse_residencies(shows)
    assert len(out) == 4
    assert meta == {}


def test_tokenless_venue_codes_never_collapse():
    """The mis-parsed cruise codes 'ST'/'IC' (no distinctive token, no city) must not forge a
    residency even with 4+ shows — they pass through as individual shows."""
    shows = [_show("ST", f"2027-0{m}-03", city="") for m in range(1, 5)]
    shows += [_show("IC", f"2027-0{m}-10", city="") for m in range(5, 9)]
    out, meta = _collapse_residencies(shows)
    assert len(out) == 8
    assert meta == {}


def test_different_acts_same_venue_not_merged():
    """Two acts at the same venue are separate residencies (3 each -> below threshold)."""
    shows = [_show("South Point Casino", f"2026-07-0{d}", artist="Piano Man") for d in (5, 6, 7)]
    shows += [_show("South Point Casino", f"2026-07-1{d}", artist="Bohemian Queen") for d in (5, 6, 7)]
    out, meta = _collapse_residencies(shows)
    assert len(out) == 6
    assert meta == {}
