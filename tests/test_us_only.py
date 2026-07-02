"""Tests for the roster-wide US-only policy (aggregation._is_us_show).

Non-US dates are dropped for every non-cruise artist. Strict artists (US_ONLY_ARTISTS) also
drop blank/unlabelled-location shows (The Dolly Show's UK towns carry no country/region);
lenient artists keep ambiguous locations so US residencies with blank columns (Reza) survive.
"""
from aggregation import _is_us_show
from models import Show


def _s(city="", region="", country="", venue="V"):
    return Show(artist="X", date="2026-09-01", venue=venue, city=city, region=region,
                country=country, ticket_url="", source="artist_website")


# --- positive US signals: always kept ---
def test_us_state_code_kept():
    assert _is_us_show(_s(city="Boston", region="MA"), strict=True) is True

def test_us_state_full_name_kept():
    # Rocket Man rows use full state names, e.g. region="Kentucky".
    assert _is_us_show(_s(city="Mount Vernon", region="Kentucky"), strict=True) is True

def test_us_country_kept():
    assert _is_us_show(_s(city="Anywhere", country="USA"), strict=True) is True

def test_us_territory_kept():
    assert _is_us_show(_s(city="Charlotte Amalie", region="St. Thomas")) is True


# --- positive non-US signals: always dropped (even lenient) ---
def test_foreign_country_dropped_lenient():
    # Priscilla's European leg carries an explicit country.
    assert _is_us_show(_s(city="Oslo", country="Norway"), strict=False) is False

def test_canada_dropped():
    assert _is_us_show(_s(city="Winnipeg", region="MB", country="CANADA")) is False


# --- ambiguous (blank) location: lenient keeps, strict drops ---
def test_blank_location_kept_when_lenient():
    # Reza: venue holds the location but city/region/country columns are blank -> must survive.
    assert _is_us_show(_s(venue="Reza Live Theatre"), strict=False) is True

def test_uk_town_dropped_when_strict():
    # The Dolly Show's UK dates: only a (UK) city, no region/country -> strict drops.
    assert _is_us_show(_s(city="Northallerton"), strict=True) is False

def test_blank_location_dropped_when_strict():
    # Arrival's "Sweden/Lithuania TBA" have no country/region label.
    assert _is_us_show(_s(city="", region="", venue="Sweden TBA"), strict=True) is False
