# Tour Date Automation — Claude Context

## What this project does
Aggregates upcoming show dates for 12 tribute/show acts from multiple sources and publishes them to a Google Sheet (one tab per artist, booked shows only, MM/DD/YY dates) and a Google Doc (one tab per artist with month headers and 2 open dates before/after each show). Ticket links are enriched to prefer venue-direct URLs over Ticketmaster/LiveNation.

## Architecture
Single file: `main.py`. No framework, no tests directory.

### Data flow per artist
```
fetch_bandsintown → fetch_seatgeek → fetch_artist_website → fetch_ticketmaster → fetch_claude_web_search
                                         ↓
                              aggregate() — dedup by MD5(artist+date+venue+city), priority order
                                         ↓
                         enrich_ticket_urls_for_artist() — one Claude web_search call per artist
                                         ↓
                              write_google_sheets() / write_json() / write_website()
```

### Source priority (lower = preferred)
| Source | Priority |
|--------|----------|
| Bandsintown | 0 |
| SeatGeek | 1 |
| artist_website | 2 |
| Ticketmaster | 3 |
| claude_web_search | 4 |

## Key functions

### Rate limiting
- `_claude_throttle()` — reads `/tmp/tour_dates_throttle.txt` (epoch float) and sleeps until that time. Persists across process restarts.
- `_claude_call_done(headers)` — parses `anthropic-ratelimit-input-tokens-reset` RFC 3339 header, writes `reset_epoch + 2` to the throttle file.
- All Claude calls use `client.messages.with_raw_response.create(...)` so headers are accessible.

### Ticket URL filtering
- `_is_platform_url(url)` — checks against `_PLATFORM_DOMAINS` tuple.
- Currently blocks: `ticketmaster.` (all TLDs), `livenation.com`, `axs.com`, `eventbrite.com`, `seatgeek.com`.
- `enrich_ticket_urls_for_artist()` only applies a URL if it starts with `http` AND is not a platform URL.

### Artist website scraping (`fetch_artist_website`)
- Replaces `<a href="...">` tags with `"link text (full_url)"` before `get_text()` so Claude sees actual URLs, not just link text like "Buy Tickets".
- Truncates page text to 32000 chars before sending to Claude.
- Skips if page text < 200 chars (JS-rendered site guard).
- JS-rendered sites (confirmed): Free Fallin, Legends, Bohemian Queen, A1A. These fall back to `fetch_claude_web_search`.

### Sheet output (`build_sheet_rows`)
- Gap ≤ 5 days between shows: inserts individual `Open` rows for each calendar day.
- Gap > 5 days: inserts `Open / ... / Open` (3 rows).
- One tab per artist, named by artist name (truncated to 100 chars).

## Environment variables (`.env`)
```
BANDSINTOWN_APP_ID=...          # Now active — artist has key
SEATGEEK_CLIENT_ID=pending_approval   # Not yet obtained
TICKETMASTER_API_KEY=...        # Active
ANTHROPIC_API_KEY=...           # Active — claude-haiku-4-5
GOOGLE_SHEETS_ID=...            # Active
GOOGLE_APPLICATION_CREDENTIALS=...   # Path to service account JSON
OUTPUT_WEBSITE_URL=             # Not configured yet
```

`_key_set(val)` returns False for empty strings and `"pending_approval"` — used to skip sources gracefully.

## Claude model
`CLAUDE_MODEL = "claude-haiku-4-5"` — chosen for cost. Haiku is ~20x cheaper than Sonnet for this workload.
`CLAUDE_MAX_TOKENS = 4096` — all calls use this limit.
`CLAUDE_CALL_LIMIT = 50` — safety cap per run.

## Known issues / limitations
- **Duplicate shows**: When artist_website and web_search return the same show with slightly different venue spellings (e.g. "Arcada Theatre" vs "The Arcada Theater"), the MD5 dedup doesn't catch it. Not a blocker.
- **Bandsintown widget sites**: A1A, Bohemian Queen, Free Fallin use JS-rendered Bandsintown widgets. The REST API returns 0 for these because it requires each artist's own `app_id`. Playwright intercepts the widget's internal API call as a fallback (see `BANDSINTOWN_WIDGET_PAGES`). Kiss The Sky has its `app_id` hardcoded in `BANDSINTOWN_APP_IDS` and works via the REST API directly.
- **Bandsintown app_id discovery**: Each artist's `app_id` is in `data-app-id` on their widget HTML. Widget v3 sites (A1A, Bohemian Queen) don't expose it — Playwright is the workaround. If an artist shares their app_id from Bandsintown Settings → API, add it to `BANDSINTOWN_APP_IDS` and remove from `BANDSINTOWN_WIDGET_PAGES`.
- **Elvis website outdated**: `elvisconcertofkings.com/tour-dates/` only shows 2023–2024 events. Claude correctly filters them as past dates → 0 shows. Web search + Ticketmaster only.
- **Piano Man — cruise ships**: All shows are cruise ship sailings (date ranges, no venue ticket page). Enrichment runs but finds nothing because cruise ship performances don't have public ticket URLs.
- **Dolly Show web search**: Claude's web search confuses "The Dolly Show" (tribute) with Dolly Parton. The artist website scrape now covers this fully (35 dates, 35/36 venue-direct URLs). Web search is a redundant fallback here.
- **Atomic sheet writes**: `clear()` + `update()` creates a brief blank window. Should be replaced with `batchUpdate` values write. Not yet implemented.

## CLI test modes
```bash
.venv/bin/python main.py                      # Full run, all artists, writes to Sheets
.venv/bin/python main.py --test-sheets        # Dummy data → Sheets (no API calls)
.venv/bin/python main.py --test-ticketmaster  # Ticketmaster only → Sheets
.venv/bin/python main.py --test-claude        # Simple ping test
.venv/bin/python main.py --test-claude-calls  # First 2 artists: website scrape + web search + enrichment, logs only
```

## External resources
- Google Sheet: `GOOGLE_SHEETS_ID` in `.env` — shared with team, treat as live
- Service account JSON: `GOOGLE_APPLICATION_CREDENTIALS` — local machine only, not in repo
- Throttle state: `/tmp/tour_dates_throttle.txt` — persists rate limit reset time across restarts

## What NOT to do
- Don't increase `CLAUDE_MAX_TOKENS` beyond 4096 without checking cost impact.
- Don't add per-show Claude calls — enrichment must stay one call per artist (`enrich_ticket_urls_for_artist`).
- Don't run destructive Sheets operations while the team may be viewing (clear+write window).
- Don't commit `.env` or `*.json` files (`.gitignore` covers these).
