"""Regression tests for extracting JSON out of a prose-wrapped Claude reply.

Reproduces the 2026-07-20 CI failure: Back 2 Mac's web-search reply put a note AFTER the
JSON array, and the old greedy `re.search(r"\\[.*\\]", DOTALL)` spanned from the first "["
to the LAST "]" in the whole reply — swallowing the trailing prose, so json.loads() died
with "Extra data: line 12 column 1" and the act silently lost every web-search show.
"""
from config import extract_json


BACK_2_MAC_REPLY = '''Based on my search results, I found one confirmed upcoming show for "Back 2 Mac: A Tribute to Fleetwood Mac" on or after July 20, 2026:

[
  {
    "date": "2026-01-23",
    "start_time": "21:00",
    "venue": "HI-FI",
    "city": "Indianapolis",
    "region": "IN",
    "country": "USA",
    "ticket_url": "https://mokbpresents.com/event/back-2-mac-2026/"
  }
]
Note: The date 2026-01-23 is actually before the requested cutoff of 2026-07-20. The search
results indicate that most major ticket platforms [Ticketmaster, SeatGeek] show no other
upcoming dates for this act.'''


def test_trailing_prose_with_brackets_does_not_break_parsing():
    """The exact failure: a bracket in the trailing note used to swallow non-JSON text."""
    events = extract_json(BACK_2_MAC_REPLY, "[")
    assert isinstance(events, list)
    assert len(events) == 1
    assert events[0]["venue"] == "HI-FI"
    assert events[0]["ticket_url"] == "https://mokbpresents.com/event/back-2-mac-2026/"


def test_plain_array():
    assert extract_json('[{"a": 1}]', "[") == [{"a": 1}]


def test_code_fenced_json():
    assert extract_json('```json\n[{"a": 1}]\n```', "[") == [{"a": 1}]


def test_object_container():
    reply = 'Here are the URLs:\n{"0": "https://x.com/e/1"}\nHope that helps! [see note]'
    assert extract_json(reply, "{") == {"0": "https://x.com/e/1"}


def test_nested_structures_are_not_truncated():
    """A non-greedy regex would have stopped at the first ']' — raw_decode tracks nesting."""
    reply = 'Result:\n[{"dates": ["2026-08-01", "2026-08-02"], "v": "X"}]\nDone.'
    out = extract_json(reply, "[")
    assert out == [{"dates": ["2026-08-01", "2026-08-02"], "v": "X"}]


def test_prose_bracket_before_the_real_array_is_skipped():
    """A bracket that doesn't start valid JSON must not abort the whole extraction."""
    reply = 'See [our note] for details.\n[{"date": "2026-08-01"}]'
    assert extract_json(reply, "[") == [{"date": "2026-08-01"}]


def test_no_json_returns_none():
    assert extract_json("Sorry, I could not find any upcoming shows.", "[") is None
    assert extract_json("", "[") is None
