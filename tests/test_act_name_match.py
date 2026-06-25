"""Iron-clad act-name matching for the whole roster.

`config.act_name_matches(candidate, artist)` is the guard that stops a fuzzy keyword search
(or a web-search hallucination) from attaching another band's dates to one of our acts. The
incident that motivated it: "Queen by The Bohemians" shows were published under "Bohemian
Queen" because both names share the words *Queen* and *Bohemian*.

Two invariants are tested for EVERY artist on the roster:
  1. self-match — the act matches its own full name and display name (and known good variants);
  2. impostor rejection — a similarly-worded but different act/artist does NOT match.
"""
import pytest

from config import BAND_NAMES, DISPLAY_NAMES, act_name_matches, _display_name


# --- 1. Self-match: every act matches its own names ---------------------------------------

@pytest.mark.parametrize("artist", BAND_NAMES)
def test_artist_matches_full_name(artist):
    assert act_name_matches(artist, artist), f"{artist!r} should match its own full name"


@pytest.mark.parametrize("artist", BAND_NAMES)
def test_artist_matches_display_name(artist):
    assert act_name_matches(_display_name(artist), artist), \
        f"display name {_display_name(artist)!r} should match {artist!r}"


# Real-world name renderings a source might return that must still match the act.
GOOD_VARIANTS = {
    "Bohemian Queen": ["Bohemian Queen", "BOHEMIAN QUEEN - A Tribute to Queen", "The Bohemian Queen Experience"],
    "A1A: The Original Jimmy Buffett Tribute": ["A1A", "A1A - The Original Jimmy Buffett Tribute"],
    "Kiss The Sky: A Jimi Hendrix Tribute": ["Kiss The Sky", "Kiss the Sky (Jimi Hendrix Tribute)"],
    "Back 2 Mac: A Tribute to Fleetwood Mac": ["Back 2 Mac", "BACK 2 MAC"],
    "The Dolly Show": ["The Dolly Show"],
    "Always Celine": ["Always Celine", "ALWAYS CELINE"],
    "Reza": ["Reza", "Reza - Edge of Illusion"],
    "Elvis: The Concert of Kings": ["Elvis: The Concert of Kings", "Elvis - Concert of Kings",
                                     "Concert of Kings"],  # ACT_NAME_ALIASES: subtitle alone is accepted
    "Arrival From Sweden: The Music of ABBA": ["Arrival From Sweden", "Arrival from Sweden"],
    "Legends of Classic Rock": ["Legends of Classic Rock"],
    "Legends of Pop in Concert": ["Legends of Pop in Concert"],
}


@pytest.mark.parametrize("artist,variants", GOOD_VARIANTS.items())
def test_good_variants_match(artist, variants):
    for v in variants:
        assert act_name_matches(v, artist), f"{v!r} should match {artist!r}"


# --- 2. Impostor rejection: similarly-worded acts must NOT match ---------------------------

# Each entry: the act -> names of DIFFERENT acts/artists that share words but aren't ours.
IMPOSTORS = {
    "Bohemian Queen": ["Queen by The Bohemians", "Queen", "The Bohemians", "Queen + Adam Lambert"],
    "Always Celine": ["Celine Dion", "Celine"],
    "The Dolly Show": ["Dolly Parton", "Dolly", "The Dolly Parton Story"],
    "Kiss The Sky: A Jimi Hendrix Tribute": ["Jimi Hendrix", "Kiss", "Hendrix"],
    "Free Fallin: The Tom Petty Concert Experience": ["Tom Petty", "Tom Petty and the Heartbreakers"],
    "Back 2 Mac: A Tribute to Fleetwood Mac": ["Fleetwood Mac", "Fleetwood"],
    "A1A: The Original Jimmy Buffett Tribute": ["Jimmy Buffett", "Buffett"],
    "The Rocket Man Show": ["Elton John", "Rocketman (the movie)"],
    "Monkee Men": ["The Monkees", "The Monkees Live"],
    # The alias accepts "Arrival from Sweden", NOT the bare subtitle — a generic "The Music of
    # ABBA" could be any ABBA tribute, so it stays rejected (this is the Mohegan Sun flag).
    "Arrival From Sweden: The Music of ABBA": ["ABBA", "ABBA The Concert", "Mamma Mia",
                                                "The Music of ABBA", "The Music of Abba"],
    "Legends of Classic Rock": ["Legends of Pop in Concert", "Classic Rock"],
    "Legends of Pop in Concert": ["Legends of Classic Rock", "Pop"],
    "Elvis: The Concert of Kings": ["Elvis Lives", "Elvis Presley", "Elvis Tribute", "Return of the King"],
    "Priscilla Presley": ["Elvis Presley", "Lisa Marie Presley"],
    "Tony Danza: Standards & Stories": ["Tony Bennett", "Danza"],
}


@pytest.mark.parametrize("artist,impostors", IMPOSTORS.items())
def test_impostors_rejected(artist, impostors):
    for name in impostors:
        assert not act_name_matches(name, artist), \
            f"{name!r} must NOT match {artist!r} (cross-act contamination)"


def test_bohemian_queen_exact_regression():
    """The exact incident: 'Queen by The Bohemians' must never read as 'Bohemian Queen'."""
    assert act_name_matches("Bohemian Queen", "Bohemian Queen")
    assert not act_name_matches("Queen by The Bohemians", "Bohemian Queen")


def test_empty_candidate_is_kept():
    """An empty performer name can't be disproven, so it matches (callers gate on non-empty)."""
    assert act_name_matches("", "Bohemian Queen")


def test_no_two_distinct_acts_collide():
    """No roster act's own name should match a DIFFERENT roster act (cross-roster safety)."""
    for a in BAND_NAMES:
        for b in BAND_NAMES:
            if a == b:
                continue
            assert not act_name_matches(a, b), f"{a!r} wrongly matches {b!r}"
