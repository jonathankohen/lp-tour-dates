"""Which drafts `--publish-drafts` is willing to publish.

The selection is the whole safety story on the Python side: a published event is public,
and a past-dated one is public *and* wrong. The server re-checks both, so these tests pin
the client's half of that agreement.
"""
from outputs.wordpress_events import select_publishable_drafts

TODAY = "2026-08-14"


def _ev(eid, date, status="draft", title="A Man Named Cash"):
    return {"id": eid, "date": date, "status": status, "title": title, "link": "", "location": ""}


def test_takes_future_drafts():
    events = [_ev(1, "2026-09-01"), _ev(2, "2026-12-25")]
    assert [e["id"] for e in select_publishable_drafts(events, TODAY)] == [1, 2]


def test_a_show_today_still_counts():
    # The date has not passed until the day is over — a show tonight belongs on the calendar.
    assert len(select_publishable_drafts([_ev(1, TODAY)], TODAY)) == 1


def test_past_dated_draft_is_never_published():
    assert select_publishable_drafts([_ev(1, "2026-08-13")], TODAY) == []


def test_already_published_and_trashed_are_left_alone():
    events = [_ev(1, "2026-09-01", status="publish"), _ev(2, "2026-09-01", status="trash")]
    assert select_publishable_drafts(events, TODAY) == []


def test_a_draft_with_no_date_is_skipped():
    assert select_publishable_drafts([_ev(1, "")], TODAY) == []


def test_skip_ids_are_held_back():
    events = [_ev(1, "2026-09-01"), _ev(2, "2026-09-02")]
    assert [e["id"] for e in select_publishable_drafts(events, TODAY, skip_ids={1})] == [2]


def test_artist_filter_matches_the_title_case_insensitively():
    events = [_ev(1, "2026-09-01", title="Bohemian Queen"), _ev(2, "2026-09-01", title="The Wankers")]
    assert [e["id"] for e in select_publishable_drafts(events, TODAY, artist_filter="wankers")] == [2]


def test_artist_filter_sees_through_an_html_entity():
    # /list-events returns the raw post title, so "&" arrives as "&amp;".
    events = [_ev(1, "2026-09-01", title="Tony Danza: Standards &amp; Stories")]
    assert len(select_publishable_drafts(events, TODAY, artist_filter="standards & stories")) == 1


# --- Ownership: only publish drafts this pipeline created -----------------------------
# Regression for 2026-08-14: #9506 "Stairway to Zeppelin" — an off-roster act with no Sheet
# rows — was a hand-built draft belonging to the team, and --publish-drafts published it.

SHEET = {("A Man Named Cash", "2026-09-01"), ("Bohemian Queen", "2026-09-02")}


def test_a_draft_with_no_sheet_show_is_left_alone():
    events = [_ev(9506, "2026-09-26", title="Stairway to Zeppelin")]
    assert select_publishable_drafts(events, TODAY, known_keys=SHEET) == []


def test_our_own_drafts_still_publish():
    events = [_ev(1, "2026-09-01", title="A Man Named Cash"),
              _ev(2, "2026-09-02", title="Bohemian Queen")]
    assert [e["id"] for e in select_publishable_drafts(events, TODAY, known_keys=SHEET)] == [1, 2]


def test_right_act_wrong_date_is_not_ours():
    # The act is on the roster, but this particular night never came from the Sheet.
    events = [_ev(1, "2026-09-03", title="A Man Named Cash")]
    assert select_publishable_drafts(events, TODAY, known_keys=SHEET) == []


def test_ownership_check_resolves_the_display_name():
    # Event titles carry the display name; the Sheet carries the full internal name.
    sheet = {("Tony Danza: Standards & Stories", "2026-09-19")}
    events = [_ev(1, "2026-09-19", title="Tony Danza: Standards &amp; Stories")]
    assert len(select_publishable_drafts(events, TODAY, known_keys=sheet)) == 1


def test_omitting_known_keys_skips_the_check():
    # An explicit --id run vouches for its own list.
    events = [_ev(9506, "2026-09-26", title="Stairway to Zeppelin")]
    assert len(select_publishable_drafts(events, TODAY)) == 1
