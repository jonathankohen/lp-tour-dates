"""write_website() pushes the same sanitized payload to WordPress AND to the Cloudflare
Worker mirror (cloudflare-worker/), each independently — a dead target must never stop
the other, and the mirror must never see a row the website didn't."""
import pytest
import requests

import outputs.website as website
from models import Show


def _show(artist="Bohemian Queen", venue="Arcada Theatre", city="St. Charles", date="2026-10-02"):
    return Show(artist=artist, date=date, venue=venue, city=city, region="IL", country="US",
                ticket_url="", source="test", raw_id=f"{artist}|{date}|{venue}")


class _Resp:
    def __init__(self, status=200):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


@pytest.fixture
def posts(monkeypatch):
    """Capture every requests.post() as (url, headers, payload); status per URL via `fail`."""
    class Calls(list):
        fail = set()

    calls = Calls()

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append((url, headers or {}, json))
        return _Resp(500 if url in calls.fail else 200)

    monkeypatch.setattr(website.requests, "post", fake_post)
    return calls


def _configure(monkeypatch, website_url="", mirror_url=""):
    monkeypatch.setattr(website, "OUTPUT_WEBSITE_URL", website_url)
    monkeypatch.setattr(website, "OUTPUT_WEBSITE_SECRET", "wp-secret" if website_url else "")
    monkeypatch.setattr(website, "OUTPUT_MIRROR_URL", mirror_url)
    monkeypatch.setattr(website, "OUTPUT_MIRROR_SECRET", "cf-secret" if mirror_url else "")


def test_both_targets_get_the_identical_payload_with_their_own_secret(monkeypatch, posts):
    _configure(monkeypatch, "https://wp.example/ingest", "https://cf.example/ingest")
    website.write_website([_show()])
    assert [c[0] for c in posts] == ["https://wp.example/ingest", "https://cf.example/ingest"]
    assert posts[0][1]["X-Tour-Secret"] == "wp-secret"
    assert posts[1][1]["X-Tour-Secret"] == "cf-secret"
    assert posts[0][2] == posts[1][2], "the mirror must receive exactly what WordPress did"
    assert posts[0][2]["shows"][0]["artist"] == "Bohemian Queen"


def test_mirror_is_optional(monkeypatch, posts):
    _configure(monkeypatch, website_url="https://wp.example/ingest")
    website.write_website([_show()])
    assert [c[0] for c in posts] == ["https://wp.example/ingest"]


def test_mirror_alone_still_publishes(monkeypatch, posts):
    _configure(monkeypatch, mirror_url="https://cf.example/ingest")
    website.write_website([_show()])
    assert [c[0] for c in posts] == ["https://cf.example/ingest"]


def test_nothing_configured_is_a_noop(monkeypatch, posts):
    _configure(monkeypatch)
    website.write_website([_show()])
    assert posts == []


def test_a_failing_website_does_not_block_the_mirror(monkeypatch, posts):
    _configure(monkeypatch, "https://wp.example/ingest", "https://cf.example/ingest")
    posts.fail.add("https://wp.example/ingest")
    website.write_website([_show()])  # must not raise
    assert [c[0] for c in posts] == ["https://wp.example/ingest", "https://cf.example/ingest"]


def test_private_bookings_are_withheld_from_the_mirror_too(monkeypatch, posts):
    _configure(monkeypatch, "https://wp.example/ingest", "https://cf.example/ingest")
    website.write_website([_show(), _show(venue="Private Event", city="Corporate Party")])
    for _url, _headers, payload in posts:
        assert [s["venue"] for s in payload["shows"]] == ["Arcada Theatre"]
