"""Tests for the full-run regression safety net (main._detect_regressions / _future_show_counts).

These guard against silently publishing a per-artist collapse (e.g. a widget scrape that timed
out and returned 0 shows), which previously shipped with a green exit code.
"""
from main import (_detect_lost_dates, _detect_regressions, _future_show_counts,
                  _future_shows_by_artist, _merge_preserved_shows, _REGRESSION_MIN_PREV,
                  _run_exit_code)
from models import Show


def _show(artist, date, venue="Some Venue"):
    return Show(artist=artist, date=date, venue=venue, city="", region="", country="",
                ticket_url="", source="sheet")


def test_future_counts_exclude_past_and_open_rows():
    shows = [
        _show("A", "2026-08-01"),           # future, real
        _show("A", "2026-09-01"),           # future, real
        _show("A", "2020-01-01"),           # past -> excluded
        _show("A", "2026-10-01", "Open"),   # filler -> excluded
        _show("B", "2026-08-01"),
    ]
    counts = _future_show_counts(shows, today="2026-07-02")
    assert counts == {"A": 2, "B": 1}


def test_collapse_is_flagged():
    """20 -> 2 (the widget-timeout incident) trips the guard."""
    prev = {"Bohemian Queen": 20}
    fresh = {"Bohemian Queen": 2}
    assert _detect_regressions(prev, fresh, ["Bohemian Queen"]) == ["Bohemian Queen"]


def test_healthy_run_not_flagged():
    prev = {"A": 20, "B": 8}
    fresh = {"A": 22, "B": 7}
    assert _detect_regressions(prev, fresh, ["A", "B"]) == []


def test_small_roster_artist_not_flagged():
    """An artist below the baseline floor (e.g. a 3-show act going to 1) is not guarded —
    small counts swing legitimately and would false-positive."""
    prev = {"Tiny": _REGRESSION_MIN_PREV - 1}
    fresh = {"Tiny": 0}
    assert _detect_regressions(prev, fresh, ["Tiny"]) == []


def test_moderate_dip_not_flagged():
    """A drop that stays above 40% of the prior count is a normal churn, not a regression."""
    prev = {"A": 10}
    fresh = {"A": 5}   # 50% of prior, and > max(2, 4)
    assert _detect_regressions(prev, fresh, ["A"]) == []


def test_new_artist_no_baseline_not_flagged():
    assert _detect_regressions({}, {"New": 1}, ["New"]) == []


# --- lost-dates test: the Tony Danza case the count test cannot see -------------------

DANZA = "Tony Danza: Standards & Stories"


def _danza_baseline():
    """His real published slate on 2026-07-23: 5 club dates + a 12-night Café Carlyle run."""
    club = [_show(DANZA, d) for d in ("2026-07-23", "2026-07-24", "2026-07-25",
                                      "2026-07-30", "2026-08-01")]
    carlyle = [_show(DANZA, f"2026-09-{d:02d}", "Café Carlyle") for d in range(8, 20)]
    return club + carlyle


def _danza_fresh():
    """What the run actually returned: the 5 club dates + 4 newly-contracted ones. The Carlyle
    residency is gone — it exists in no live source and nowhere in Airtable."""
    club = [_show(DANZA, d) for d in ("2026-07-23", "2026-07-24", "2026-07-25",
                                      "2026-07-30", "2026-08-01")]
    contracted = [_show(DANZA, d) for d in ("2026-10-03", "2026-10-11",
                                            "2026-10-25", "2026-11-20")]
    return club + contracted


def test_count_test_alone_misses_the_danza_case():
    """17 -> 9 clears the 40% count threshold, which is exactly why a second test is needed."""
    prev, fresh = {DANZA: 17}, {DANZA: 9}
    assert _detect_regressions(prev, fresh, [DANZA]) == []


def test_lost_dates_catches_the_danza_case():
    """Gaining 4 dates while losing 12 is a regression however healthy the net count looks."""
    prev = {DANZA: _danza_baseline()}
    fresh = {DANZA: _danza_fresh()}
    assert _detect_lost_dates(prev, fresh, [DANZA]) == [DANZA]


def test_lost_dates_ignores_normal_churn():
    """Two dates rolling off a 12-date slate is routine, not a collapse."""
    prev = {"A": [_show("A", f"2026-08-{d:02d}") for d in range(1, 13)]}
    fresh = {"A": [_show("A", f"2026-08-{d:02d}") for d in range(3, 13)]}
    assert _detect_lost_dates(prev, fresh, ["A"]) == []


def test_lost_dates_ignores_small_baselines():
    prev = {"Tiny": [_show("Tiny", "2026-08-01"), _show("Tiny", "2026-08-02")]}
    assert _detect_lost_dates(prev, {"Tiny": []}, ["Tiny"]) == []


# --- merge-on-trip: keep the residency AND publish the new contracted dates ------------

def test_merge_keeps_sheet_only_dates_and_the_new_ones():
    prev = {DANZA: _danza_baseline()}
    fresh = {DANZA: _danza_fresh()}
    merged, preserved = _merge_preserved_shows(list(fresh[DANZA]), prev, fresh, [DANZA])
    dates = {s.date for s in merged}
    assert preserved == 12
    assert {f"2026-09-{d:02d}" for d in range(8, 20)} <= dates   # Carlyle survives
    assert {"2026-10-03", "2026-11-20"} <= dates                  # new contracts publish
    assert len(merged) == 21


def test_merge_does_not_double_book_a_date_the_run_found():
    """When the run found that date, the fresh record wins — no duplicate row."""
    prev = {"A": [_show("A", "2026-08-01", "Old Venue Name")]}
    fresh = {"A": [_show("A", "2026-08-01", "New Venue Name")]}
    merged, preserved = _merge_preserved_shows(list(fresh["A"]), prev, fresh, ["A"])
    assert preserved == 0
    assert [s.venue for s in merged] == ["New Venue Name"]


def test_future_shows_by_artist_excludes_past_and_open():
    shows = [_show("A", "2026-08-01"), _show("A", "2020-01-01"),
             _show("A", "2026-10-01", "Open")]
    assert [s.date for s in _future_shows_by_artist(shows, "2026-07-02")["A"]] == ["2026-08-01"]


# --- exit-code semantics -------------------------------------------------------------


def test_regression_alone_does_not_fail_the_run():
    """A guard trip means the guard WORKED: the missing Sheet rows were preserved and merged
    with the fresh set, so the outputs are correct. Tony Danza's real-but-unsourced Café
    Carlyle run trips this every week, and a permanently red CI is one nobody reads."""
    assert _run_exit_code(failed_artists=[], regressed=["Tony Danza: Standards & Stories"]) == 0


def test_aggregation_failure_fails_the_run():
    """An artist that raised during aggregation genuinely broke."""
    assert _run_exit_code(failed_artists=["A1A"], regressed=[]) == 1


def test_failure_wins_over_regression():
    assert _run_exit_code(failed_artists=["A1A"], regressed=["Tony Danza"]) == 1


def test_clean_run_is_zero():
    assert _run_exit_code(failed_artists=[], regressed=[]) == 0
