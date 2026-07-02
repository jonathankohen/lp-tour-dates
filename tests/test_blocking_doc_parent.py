"""Regression test for the blocking-email Doc parent-tab reuse (the "Tab title must be unique"
bug). The create fallback used to ignore the live tab list and try to create a parent tab whose
title already existed. `_existing_parent_id` must reuse an existing top-level tab — matched by
either the configured title OR the display-name title we'd otherwise create under.
"""
from outputs.blocking_email_doc import _existing_parent_id
from config import _display_name

A1A = "A1A: The Original Jimmy Buffett Tribute"          # has an acronym but NO parent-title map entry
B2M = "Back 2 Mac: A Tribute to Fleetwood Mac"           # mapped -> "BACK 2 MAC Dates"


def test_reuses_existing_tab_by_display_name():
    """The bug: artist absent from the hardcoded map, but a tab titled with its display name
    already exists. Must reuse it, not signal 'create' (which collided)."""
    title = _display_name(A1A)[:50]
    assert _existing_parent_id(A1A, {title: "tab-1"}) == "tab-1"


def test_reuses_existing_tab_by_configured_title():
    assert _existing_parent_id(B2M, {"BACK 2 MAC Dates": "tab-2"}) == "tab-2"


def test_returns_none_when_no_matching_tab():
    """No existing tab -> caller creates one."""
    assert _existing_parent_id(A1A, {"Some Other Artist": "tab-9"}) is None
