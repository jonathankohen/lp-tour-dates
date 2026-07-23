"""Cruise acts' tour pages are ship itineraries: one row per port call, not per bookable show.
Those port/sea days stay in the Sheet and routing Doc but must not become public event posts
or front-end entries (Legends of Classic Rock drafted ~30 port-call false positives, 2026-07-23).
A real land gig on the same page still publishes.
"""
from config import CRUISE_ACTS, is_cruise_sailing
from models import Show
from outputs.wordpress_events import _is_private_or_tba

LOCR = "Legends of Classic Rock"
PIANO = "Kyle Martin's Piano Man"


def _s(artist, venue, city="", region=""):
    return Show(artist=artist, date="2026-08-16", venue=venue, city=city, region=region,
                country="", ticket_url="", source="artist_website")


def test_named_cruise_ports_are_hidden():
    for port in ("Port Canaveral", "CocoCay", "Charlotte Amalie", "Basseterre",
                 "San Juan", "Philipsburg", "Cozumel", "Roatan", "Costa Maya"):
        assert is_cruise_sailing(LOCR, port, ""), port


def test_ship_names_are_hidden_via_the_no_venue_word_default():
    """Piano Man's rows carry the SHIP as the venue — no port list needed, the bare-name
    default catches them."""
    for ship in ("Celebrity Edge", "Caribbean Princess", "Ruby Princess", "Celebrity Summit"):
        assert is_cruise_sailing(PIANO, ship, ""), ship


def test_real_land_gigs_for_cruise_acts_still_publish():
    for venue in ("Burlington County Amp", "Cactus Theater", "Marshall Auditorium",
                  "Daryl's House", "The Ironstone Amphitheater", "Hoboken Italian Festival"):
        assert not is_cruise_sailing(LOCR, venue, ""), venue
        assert not is_cruise_sailing(PIANO, venue, ""), venue


def test_non_cruise_acts_are_never_affected():
    """A land act playing an actual port CITY (e.g. a theater in San Juan) is not a cruise act,
    so the cruise filter never touches it — its own US/venue logic applies."""
    assert not is_cruise_sailing("Bohemian Queen", "San Juan", "San Juan")
    assert not is_cruise_sailing("The Dolly Show", "Port Canaveral", "")


def test_cruise_sailing_feeds_the_event_exclusion():
    assert _is_private_or_tba(_s(LOCR, "Port Canaveral", region="Florida"))
    assert not _is_private_or_tba(_s(LOCR, "Burlington County Amp", region="New Jersey"))


def test_cruise_acts_set_is_what_we_expect():
    assert CRUISE_ACTS == {LOCR, PIANO}
