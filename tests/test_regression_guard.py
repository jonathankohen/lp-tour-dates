"""Tests for the full-run regression safety net (main._detect_regressions / _future_show_counts).

These guard against silently publishing a per-artist collapse (e.g. a widget scrape that timed
out and returned 0 shows), which previously shipped with a green exit code.
"""
from main import _detect_regressions, _future_show_counts, _REGRESSION_MIN_PREV, _run_exit_code
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


# --- exit-code semantics -------------------------------------------------------------


def test_regression_alone_does_not_fail_the_run():
    """A guard trip means the guard WORKED: last-good data was republished, so the outputs
    are correct. Tony Danza's real-but-unsourced Cafe Carlyle run trips this every week,
    and a permanently red CI is one nobody reads."""
    assert _run_exit_code(failed_artists=[], regressed=["Tony Danza: Standards & Stories"]) == 0


def test_aggregation_failure_fails_the_run():
    """An artist that raised during aggregation genuinely broke."""
    assert _run_exit_code(failed_artists=["A1A"], regressed=[]) == 1


def test_failure_wins_over_regression():
    assert _run_exit_code(failed_artists=["A1A"], regressed=["Tony Danza"]) == 1


def test_clean_run_is_zero():
    assert _run_exit_code(failed_artists=[], regressed=[]) == 0
