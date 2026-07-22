"""Regression tests for partial Google Sheet reads.

Reproduces the CI failure of 2026-07-20: the run exceeded the Sheets 60-reads-per-minute
quota, read_shows_from_sheets() swallowed the per-tab 429s and returned 179 of 338 shows,
and that truncated set was handed to write_website() — which REPLACES the whole front-end
dataset. Reza's 107 shows and six other acts were wiped off the public calendar.

The fixes under test:
  1. all tabs are fetched in ONE batchGet (so the quota isn't blown in the first place),
  2. a failed / short read raises SheetReadError instead of returning a partial list,
  3. the sheet-write preservation pass reads each tab once and refuses to write a tab whose
     existing rows it could not read (that write would erase manual URLs / start times).
"""
import pytest

import utils
from utils import read_shows_from_sheets, SheetReadError
from outputs.sheets import _ticket_urls_from_rows, _start_times_from_rows


class _FakeRequest:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    def execute(self):
        if self._error:
            raise self._error
        return self._result


class _FakeResp:
    def __init__(self, status):
        self.status = status


class _HttpError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.resp = _FakeResp(status)


class _FakeValues:
    def __init__(self, batch_result=None, batch_error=None):
        self._batch_result = batch_result
        self._batch_error = batch_error
        self.batch_calls = 0

    def batchGet(self, spreadsheetId, ranges):
        self.batch_calls += 1
        self.last_ranges = list(ranges)
        return _FakeRequest(self._batch_result, self._batch_error)


class _FakeSpreadsheets:
    def __init__(self, titles, values):
        self._titles = titles
        self._values = values

    def get(self, spreadsheetId):
        return _FakeRequest({"sheets": [{"properties": {"title": t}} for t in self._titles]})

    def values(self):
        return self._values


class _FakeService:
    def __init__(self, titles, values):
        self._ss = _FakeSpreadsheets(titles, values)

    def spreadsheets(self):
        return self._ss


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    """The 429 path really sleeps out the per-minute quota window; don't do that in tests."""
    monkeypatch.setattr(utils.time, "sleep", lambda _s: None)


def _install(monkeypatch, tmp_path, titles, batch_result=None, batch_error=None):
    """Point read_shows_from_sheets at a fake Sheets service."""
    values = _FakeValues(batch_result, batch_error)
    service = _FakeService(titles, values)

    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(creds))
    monkeypatch.setattr(utils, "GOOGLE_SHEETS_ID", "sheet-id")

    import sys, types
    fake_disc = types.ModuleType("googleapiclient.discovery")
    fake_disc.build = lambda *a, **k: service
    fake_pkg = types.ModuleType("googleapiclient")
    fake_pkg.discovery = fake_disc
    fake_sa_mod = types.ModuleType("google.oauth2.service_account")
    fake_sa_mod.Credentials = types.SimpleNamespace(
        from_service_account_file=lambda *a, **k: object()
    )
    fake_oauth2 = types.ModuleType("google.oauth2")
    fake_oauth2.service_account = fake_sa_mod
    for name, mod in [
        ("googleapiclient", fake_pkg),
        ("googleapiclient.discovery", fake_disc),
        ("google.oauth2", fake_oauth2),
        ("google.oauth2.service_account", fake_sa_mod),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)
    return values


_HEADER = ["Date", "Venue", "City", "Region", "Country", "Ticket URL", "Source", "Start Time"]


def _rows(*shows):
    return [_HEADER] + list(shows)


def test_all_tabs_read_in_one_batch_request(monkeypatch, tmp_path):
    """The N+1 per-tab reads that blew the quota are now a single batchGet."""
    titles = ["Reza", "Piano Man"]
    batch = {"valueRanges": [
        {"values": _rows(["08/01/26", "Grand Ole Opry", "Nashville", "TN", "US", "", "sheet", ""])},
        {"values": _rows(["08/02/26", "Blue Note", "New York", "NY", "US", "", "sheet", ""])},
    ]}
    values = _install(monkeypatch, tmp_path, titles, batch_result=batch)

    shows = read_shows_from_sheets()

    assert values.batch_calls == 1, "tabs must be fetched in one batchGet, not one call per tab"
    assert len(shows) == 2


def test_quota_failure_raises_instead_of_returning_partial(monkeypatch, tmp_path):
    """A 429 on the read must not surface as a short list a caller would publish."""
    _install(monkeypatch, tmp_path, ["Reza", "Piano Man"], batch_error=_HttpError(429))

    with pytest.raises(SheetReadError):
        read_shows_from_sheets()


def test_short_response_raises(monkeypatch, tmp_path):
    """Fewer value ranges than tabs requested = an incomplete read, not a valid result."""
    titles = ["Reza", "Piano Man"]
    batch = {"valueRanges": [
        {"values": _rows(["08/01/26", "Grand Ole Opry", "Nashville", "TN", "US", "", "sheet", ""])},
    ]}
    _install(monkeypatch, tmp_path, titles, batch_result=batch)

    with pytest.raises(SheetReadError):
        read_shows_from_sheets()


def test_non_strict_read_returns_empty_rather_than_raising(monkeypatch, tmp_path):
    """The regression-guard baseline tolerates a failure — it publishes nothing."""
    _install(monkeypatch, tmp_path, ["Reza"], batch_error=_HttpError(429))

    assert read_shows_from_sheets(strict=False) == []


def test_tab_titles_with_quotes_are_escaped(monkeypatch, tmp_path):
    """A1 notation escapes a single quote in a tab title by doubling it."""
    batch = {"valueRanges": [{"values": _rows()}]}
    values = _install(monkeypatch, tmp_path, ["Kyle Martin's Piano Man"], batch_result=batch)

    read_shows_from_sheets(strict=False)

    assert values.last_ranges == ["'Kyle Martin''s Piano Man'!A1:H"]


# --- the write-side preservation pass ------------------------------------------------


def test_preservation_parses_urls_and_times_from_one_row_set():
    """Both preservation passes now read the same single fetch of the tab."""
    rows = _rows(
        ["08/01/26", "Grand Ole Opry", "Nashville", "TN", "US",
         "https://opry.com/events/reza-08-01", "sheet", "8:00 PM"],
    )
    urls = _ticket_urls_from_rows(rows, "Reza")
    times = _start_times_from_rows(rows, "Reza")

    assert list(urls.values()) == ["https://opry.com/events/reza-08-01"]
    assert list(times.values()) == ["20:00"]
    # Same dedup key from both passes — they must stay in lockstep.
    assert set(urls) == set(times)


def test_platform_urls_are_not_preserved():
    rows = _rows(
        ["08/01/26", "Grand Ole Opry", "Nashville", "TN", "US",
         "https://www.ticketmaster.com/event/123", "sheet", ""],
    )
    assert _ticket_urls_from_rows(rows, "Reza") == {}


# --- write_google_sheets must not overwrite a tab it could not read ------------------


class _RecordingService:
    """Minimal Sheets service that records writes and can fail the preservation read."""

    def __init__(self, titles, rows_by_tab, read_error=None):
        self._titles = titles
        self._rows_by_tab = rows_by_tab
        self._read_error = read_error
        self.writes: list[str] = []
        self.outer = self

        class _Values:
            def get(_s, spreadsheetId, range):
                return _FakeRequest({"values": self._rows_by_tab.get(range, [])})

            def batchGet(_s, spreadsheetId, ranges):
                if self._read_error:
                    return _FakeRequest(error=self._read_error)
                return _FakeRequest({"valueRanges": [
                    {"values": self._rows_by_tab.get(r, [])} for r in ranges
                ]})

            def update(_s, spreadsheetId, range, valueInputOption, body):
                self.writes.append(f"update {range}")
                self.last_written_rows = body["values"]
                return _FakeRequest({})

            def clear(_s, spreadsheetId, range):
                self.writes.append(f"clear {range}")
                return _FakeRequest({})

        class _SS:
            def get(_s, spreadsheetId):
                return _FakeRequest(
                    {"sheets": [{"properties": {"title": t, "sheetId": i}}
                                for i, t in enumerate(self._titles)]}
                )

            def values(_s):
                return _Values()

            def batchUpdate(_s, spreadsheetId, body):
                self.writes.append("batchUpdate")
                return _FakeRequest({"replies": [{"addSheet": {"properties": {"sheetId": 9}}}]})

        self._ss = _SS()

    def spreadsheets(self):
        return self._ss


def _write_with(monkeypatch, service, shows):
    import outputs.sheets as osheets
    monkeypatch.setattr(osheets, "GOOGLE_SHEETS_ID", "sheet-id")
    monkeypatch.setattr(osheets, "_execute_with_retry", lambda req: req.execute())
    osheets.write_google_sheets(shows, reorder=False)


def test_write_is_skipped_when_the_preservation_read_fails(monkeypatch, tmp_path, capsys):
    """THE regression: a 429 on the read must not lead to a tab being overwritten.

    The old code returned {} from both preservation readers on any exception, then wrote
    the tab anyway — erasing every manually-entered ticket URL and start time in it.
    """
    from models import Show
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(creds))

    service = _RecordingService(["Reza"], {}, read_error=_HttpError(429))
    _install_google_stubs(monkeypatch, service)

    shows = [Show(artist="Reza", date="2026-08-01", venue="Opry", city="Nashville",
                  region="TN", country="US", ticket_url="", source="test")]
    _write_with(monkeypatch, service, shows)

    assert service.writes == [], "no tab may be written when its existing rows are unreadable"


def test_manual_ticket_url_and_time_are_preserved(monkeypatch, tmp_path):
    """The happy path still preserves what a human typed into the Sheet."""
    from models import Show
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(creds))

    existing = _rows(["08/01/26", "Opry", "Nashville", "TN", "US",
                      "https://opry.com/events/reza", "sheet", "8:00 PM"])
    service = _RecordingService(["Reza"], {"'Reza'!A1:H": existing})
    _install_google_stubs(monkeypatch, service)

    show = Show(artist="Reza", date="2026-08-01", venue="Opry", city="Nashville",
                region="TN", country="US", ticket_url="", source="test")
    _write_with(monkeypatch, service, [show])

    assert service.writes, "the tab should have been written"
    assert show.ticket_url == "https://opry.com/events/reza"
    assert show.start_time == "20:00"


def _install_google_stubs(monkeypatch, service):
    import sys, types
    fake_disc = types.ModuleType("googleapiclient.discovery")
    fake_disc.build = lambda *a, **k: service
    fake_pkg = types.ModuleType("googleapiclient")
    fake_pkg.discovery = fake_disc
    fake_sa_mod = types.ModuleType("google.oauth2.service_account")
    fake_sa_mod.Credentials = types.SimpleNamespace(
        from_service_account_file=lambda *a, **k: object()
    )
    fake_oauth2 = types.ModuleType("google.oauth2")
    fake_oauth2.service_account = fake_sa_mod
    for name, mod in [
        ("googleapiclient", fake_pkg),
        ("googleapiclient.discovery", fake_disc),
        ("google.oauth2", fake_oauth2),
        ("google.oauth2.service_account", fake_sa_mod),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)
