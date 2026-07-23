"""A calendar widget's ld+json block may be an object, an array, or an @graph wrapper.

Assuming a dict raised AttributeError mid-scrape and took out the whole artist's aggregation
(The Platters, 2026-07-23) — in a full run that's an artist counted as FAILED and a non-zero
exit, so the shape handling is worth pinning.
"""
from sources.artist_website import _ld_json_entries

EVENT = {"@type": "Event", "name": "A Show", "startDate": "2027-01-06T20:00:00"}
OTHER = {"@type": "Organization", "name": "Not an event"}


def test_single_object():
    assert _ld_json_entries(EVENT) == [EVENT]


def test_bare_array_the_platters_shape():
    assert _ld_json_entries([EVENT, OTHER, EVENT]) == [EVENT, EVENT]


def test_graph_wrapper():
    assert _ld_json_entries({"@context": "https://schema.org", "@graph": [OTHER, EVENT]}) == [EVENT]


def test_non_event_types_are_dropped():
    assert _ld_json_entries(OTHER) == []


def test_junk_shapes_do_not_raise():
    for junk in (None, "a string", 42, [None, "x", 1], [[EVENT]]):
        assert _ld_json_entries(junk) == []
