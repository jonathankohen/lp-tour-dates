"""Keyless web search — no API key, no AI.

Used by the programmatic ticket-link verifier (enrichment.find_event_ticket_urls_via_search)
to find candidate ticket pages when a stored link fails verification. Returns plain result
URLs; the caller "sifts" them by re-running page_confirms_event, so no AI is involved.

Strategy (graceful, mirroring the optional-Playwright pattern elsewhere):
  1. Prefer the `ddgs` library (formerly `duckduckgo_search`) if installed. NOTE: modern
     `ddgs` is a metasearch — it fans out across multiple keyless backends (DuckDuckGo,
     plus Google/Brave/Yandex/Wikipedia/etc.) and handles rate-limit backoff. Still no key.
  2. Fall back to scraping DuckDuckGo's lite HTML endpoint with the requests +
     beautifulsoup4 deps we already have, decoding DDG's `uddg=` redirect links. This
     fallback is DuckDuckGo-only.
Any failure returns [] rather than raising — search is a best-effort fallback.
"""
import logging
import time
from urllib.parse import parse_qs, urlparse

import requests

log = logging.getLogger(__name__)

# ddgs (multi-engine) and its HTTP client log every per-engine request/error at INFO,
# which floods our output with 429/403/timeout noise from engines we don't control.
# Keep only real warnings.
for _noisy in ("ddgs", "httpx", "primp"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# Curated keyless backends: real web-search engines only, dropping ones that return
# non-ticket junk (Wikipedia/Grokipedia) or reliably rate-limit (Mojeek/Startpage).
# Pure DuckDuckGo blocks automated queries (returns nothing), so it can't be used alone.
_BACKENDS = "google, brave, yahoo, yandex, duckduckgo"

_HTML_ENDPOINT = "https://html.duckduckgo.com/html/"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# DuckDuckGo throttles bursty automated queries; keep a minimum gap between calls
# (persisted in-process) and back off on failure.
_MIN_INTERVAL_S = 2.0
_last_call_at = 0.0


def _throttle() -> None:
    global _last_call_at
    wait = _MIN_INTERVAL_S - (time.time() - _last_call_at)
    if wait > 0:
        time.sleep(wait)
    _last_call_at = time.time()


def _search_via_library(query: str, max_results: int) -> list[str] | None:
    """Use the ddgs / duckduckgo_search library if available. Returns None if not installed."""
    try:
        from ddgs import DDGS  # type: ignore
    except ImportError:
        try:
            from duckduckgo_search import DDGS  # type: ignore
        except ImportError:
            return None
    def _text(ddgs):
        try:
            return ddgs.text(query, max_results=max_results, backend=_BACKENDS)
        except TypeError:  # older/newer signature without a backend kwarg
            return ddgs.text(query, max_results=max_results)

    try:
        with DDGS(timeout=10) as ddgs:
            return [
                r["href"]
                for r in _text(ddgs)
                if isinstance(r, dict) and r.get("href", "").startswith("http")
            ]
    except Exception as exc:
        log.debug("ddgs library search failed for %r: %s", query, exc)
        return None


def _decode_ddg_href(href: str) -> str:
    """DDG HTML results wrap targets in a redirect: //duckduckgo.com/l/?uddg=<encoded>.
    Return the real target, or the href unchanged if it's already a plain URL."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return target or href
    return href


def _search_via_html(query: str, max_results: int) -> list[str]:
    """Fallback: scrape the DDG lite HTML endpoint with requests + BeautifulSoup."""
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except ImportError:
        log.warning("beautifulsoup4 not installed — cannot scrape DuckDuckGo HTML results.")
        return []
    try:
        resp = requests.post(_HTML_ENDPOINT, data={"q": query}, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as exc:
        log.debug("DDG HTML endpoint failed for %r: %s", query, exc)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    urls: list[str] = []
    for a in soup.select("a.result__a[href]"):
        url = _decode_ddg_href(a["href"])
        if url.startswith("http") and url not in urls:
            urls.append(url)
            if len(urls) >= max_results:
                break
    return urls


def ddg_search(query: str, max_results: int = 8) -> list[str]:
    """Return up to `max_results` result URLs for `query` from DuckDuckGo (no API key).

    Tries the ddgs library first, then the HTML endpoint. Returns [] on any failure.
    """
    if not query.strip():
        return []
    _throttle()
    results = _search_via_library(query, max_results)
    if results is None:  # library not installed
        results = _search_via_html(query, max_results)
    elif not results:  # library installed but returned nothing — try the HTML endpoint too
        results = _search_via_html(query, max_results)
    log.debug("DDG search %r -> %d result(s)", query, len(results))
    return results[:max_results]
