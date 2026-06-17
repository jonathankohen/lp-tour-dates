# Tour Date Automation — Claude Context

## What this project does
Aggregates upcoming show dates for the Love Productions roster (tribute/show acts)
from multiple sources and publishes them to:
- **Google Sheet** — one tab per artist, booked shows only, MM/DD/YY dates, with a
  per-show Start Time column (stored 24h, displayed 12h).
- **Google Doc** — one tab per artist with season/month headers and OPEN fill-in
  days (±2) around each show, for routing/outreach.
- **WordPress front-end** — JSON pushed to the Tour Calendar plugin's `/ingest`
  webhook; the plugin renders a sortable calendar + copy-paste outreach formats.
- **WordPress events** — native `event` posts via the plugin's `/publish-events`
  endpoint (separate, opt-in step).
- **Blocking email Doc** — per-act tabs with Routes + email-zone subtabs.

Ticket links are enriched to prefer venue-direct URLs over Ticketmaster/LiveNation
and other resale/platform domains.

## Keep this file updated
**This file is the onboarding contract for the codebase. When you change
architecture, add/remove a source or output, change CLI flags, add a config knob,
or discover a new scraping quirk, update the relevant section here in the same
change.** The repo was once a single `main.py`; it has since been split into the
module layout below. If you find this doc drifting from the code again, fix it —
treat a stale CLAUDE.md as a bug.

## Architecture
Modular. Entry point is `main.py` (CLI dispatch + `run()` orchestration). No test
directory; verification is via the `--test-*` CLI modes.

```
main.py            # CLI dispatch, run() full pipeline, test_* helpers
config.py          # env vars, constants, per-artist maps, time/URL/platform helpers
models.py          # Show dataclass + dedup_key()
aggregation.py     # aggregate(): fan out to sources, dedup, US-only filter, enrich
enrichment.py      # Claude venue-direct ticket-URL enrichment + link verification
claude_state.py    # cross-process rate-limit throttle + cost tracking/cap
airtable.py        # pulls the active artist roster (priority-ordered) from Airtable
utils.py           # read_shows_from_sheets(), build_doc_from_sheets(), tab matching
sources/           # one module per data source (see Sources)
outputs/           # one module per publish target (see Outputs)
```

### Data flow per artist (`aggregation.aggregate`)
```
fetch_bandsintown → fetch_seatgeek → [fetch_back2mac_sheets] → fetch_artist_website
                  → fetch_ticketmaster → [fetch_claude_web_search]
                                   ↓
            _dedup_shows()  — dedup by MD5(artist|date|venue|city), source priority
                                   ↓
            US-only filter  — drop non-US shows for artists in US_ONLY_ARTISTS
                                   ↓
            enrich_ticket_urls_for_artist()  — one Claude call/artist (when enrich=True)
                                   ↓
            fill_start_times_from_pages()    — recover times from ticket pages (no Claude)
```

In a full `run()`, per-artist aggregation is called with `enrich=False`; enrichment
happens once in a single batched pass (`enrich_ticket_urls_all`) across all artists,
then `fill_start_times_from_pages` runs once, then all outputs are written.

### Source priority (lower = preferred; `aggregation._SOURCE_PRIORITY`)
| Source            | Priority |
| ----------------- | -------- |
| bandsintown       | 0        |
| seatgeek          | 1        |
| artist_website    | 2        |
| ticketmaster      | 3        |
| claude_web_search | 4        |
| back2mac_sheets   | 5        |

Start times use a *separate* trust order (`_TIME_PRIORITY`): structured APIs carry
authoritative local times, so they outrank Claude-extracted times even when the
*kept* record came from a lower-priority source.

## Sources (`sources/`)
- **bandsintown.py** — REST API by artist name/app_id. For artists whose page is a
  JS Bandsintown widget (`BANDSINTOWN_WIDGET_PAGES`), Playwright intercepts the
  widget's internal `rest.bandsintown.com/events` call. Per-artist `app_id`s live in
  `BANDSINTOWN_APP_IDS`; name overrides in `BANDSINTOWN_ARTIST_NAMES`.
- **seatgeek.py** — SeatGeek API. Skipped unless `SEATGEEK_CLIENT_ID` is set.
- **ticketmaster.py** — Ticketmaster Discovery API. `country` comes back as a code (`US`).
- **artist_website.py** — scrapes the act's official tour page (`ARTIST_WEBSITES`).
  Replaces `<a href>` tags with `"text (full_url)"` so Claude sees real URLs; truncates
  page text; skips if <200 chars (JS-render guard). Variants: plain text scrape,
  Playwright DOM render (`PLAYWRIGHT_RENDER_PAGES`), Claude **vision** for poster-image
  schedules (`VISION_TOUR_PAGES`), and an Elfsight JSON-LD calendar path. Date
  extraction uses the higher `CLAUDE_WEBSITE_MAX_TOKENS` ceiling.
- **claude_web_search.py** — Claude web_search fallback. Skipped for an artist when
  non-Claude sources already returned ≥ `WEB_SEARCH_SKIP_THRESHOLD` (3) shows, and
  gated by the cost cap.
- **ticket_page.py** — not a show source; fetches a show's ticket page to (a) recover
  a missing `start_time` from schema.org `Event.startDate` or labeled clock text
  (`fill_start_times_from_pages`, no Claude) and (b) verify a ticket link actually
  references the act+date (`verify_ticket_links`, `page_confirms_event`).
- **back2mac_sheets.py** — reads the Back 2 Mac act's own Google Sheet
  (`BACK_2_MAC_SHEETS_ID`); provides dates but no venue, so it's lowest priority.

## Key behaviors

### Rate limiting & cost (`claude_state.py`)
- `_claude_throttle()` reads `/tmp/tour_dates_throttle.txt` (epoch float) and sleeps
  until that time; persists across process restarts.
- `_claude_call_done(headers)` parses the `anthropic-ratelimit-*-reset` RFC 3339
  header and writes `reset_epoch + CLAUDE_RATE_LIMIT_BUFFER` (2s) to the throttle file.
  Falls back to now+90s if the header is missing.
- All Claude calls use `client.messages.with_raw_response.create(...)` so headers
  are accessible.
- `_track_cost()` accumulates estimated USD (Haiku input/output + web-search uses);
  `_under_cost_cap(label)` gates optional calls against `COST_CAP_USD` (default $2.00,
  override via env). `CLAUDE_CALL_LIMIT` (50) is a hard per-run safety cap.

### Ticket URL filtering (`config.py`)
- `_is_platform_url(url)` checks `_PLATFORM_DOMAINS`: `ticketmaster.` (all TLDs),
  `livenation.com`, `axs.com`, `eventbrite.com`, `seatgeek.com`, `bandsintown.com`.
- Enrichment only applies a Claude-found URL if it starts with `http` and is not a
  platform URL; otherwise the platform URL is kept as a fallback.

### US-only filter (`aggregation.py`, `config.py`)
- Artists listed in `US_ONLY_ARTISTS` (currently `The Dolly Show`) have non-US shows
  dropped after dedup, before enrichment.
- `_is_us_show()` normalizes `country` (strips non-letters, uppercases) and keeps the
  show if it's empty (sources leave it blank when a US state was parsed but no country
  label) or matches `US/USA/UNITEDSTATES/UNITEDSTATESOFAMERICA`. Add an artist to the
  set to restrict them too.

### Start times
- Carried per-show on `Show.start_time`, canonical 24-hour `"HH:MM"`, `""` if unknown.
- APIs and ISO datetimes in URLs (`_time_from_url`) supply it; `ticket_page` recovers
  the rest. Sheet displays 12-hour (`_fmt_time_12h`); read-back normalizes any format
  to 24h (`_parse_time_to_24h`). No default time is guessed.

## Outputs (`outputs/`)
- **sheets.py** `write_google_sheets(shows, reorder=True)` — one tab per artist
  (`_display_name`, truncated to 100). Reads back existing tabs to preserve
  manually-entered ticket URLs and start times. `build_sheet_rows` inserts `Open` rows
  for gaps ≤5 days, and an `Open / … / Open` block for gaps >5 days. Each tab is written
  per-artist, so a single-artist run only touches that artist's tab.
- **doc.py** `write_google_doc(shows, partial=False)` — per-artist tab with season/month
  subtabs and OPEN fill-in days. `partial=True` updates only the artists present.
- **website.py** `write_website(shows)` — POSTs `{generated_at, shows}` to
  `OUTPUT_WEBSITE_URL` with `X-Tour-Secret`. **Replaces the entire front-end dataset**,
  so never call it with a single artist's shows — read the full set from the Sheet first
  (see the `--artist` flow). No-op if `OUTPUT_WEBSITE_URL` is unset.
- **wordpress_events.py** `publish_events(...)` — creates/updates VS Event List `event`
  posts via `/publish-events` (the CPT isn't REST-exposed, so the plugin does it
  server-side). Pulls each act's fallback image + bio from a Google Drive folder
  (`WORDPRESS_ASSETS_DRIVE_FOLDER_ID`). Also `cleanup_duplicate_events()` and
  `update_event_descriptions()`.
- **blocking_email_doc.py** `write_blocking_email_doc(shows)` — per-act tabs (by acronym)
  with a Routes subtab + email-zone subtabs in the blocking Doc (`BLOCKING_TEST_ID`).
  Per-artist safe.
- **json_output.py** `write_json(shows)` — writes the same payload to `OUTPUT_JSON_PATH`.

## Per-artist config maps (`config.py`)
`BAND_NAMES` (hardcoded fallback roster), `EVENT_CATEGORIES`, `DISPLAY_NAMES`
(`_display_name`), `SUBTAB_PREFIXES` (`_subtab_prefix`), `ARTIST_WEBSITES`,
`BANDSINTOWN_ARTIST_NAMES`, `BANDSINTOWN_APP_IDS`, `PLAYWRIGHT_RENDER_PAGES`,
`VISION_TOUR_PAGES`, `BANDSINTOWN_WIDGET_PAGES`, `US_ONLY_ARTISTS`. Keys are the full
internal artist name (the value carried on `Show.artist`), not the display name.

The live roster is normally fetched from Airtable (`airtable.fetch_airtable_priority_artists`,
priority order: Top of Roster → Exclusive → Core Roster). `run()` falls back to
`BAND_NAMES` if Airtable returns empty.

## Claude model (`config.py`)
`CLAUDE_MODEL = "claude-haiku-4-5"` — chosen for cost (~20× cheaper than Sonnet here).
`CLAUDE_MAX_TOKENS = 4096` (web search + enrichment). `CLAUDE_WEBSITE_MAX_TOKENS =
16000` (artist-website date extraction only — dense cruise/residency pages truncate
at 4096 and drop tail dates; Haiku output is $5/1M so ≤ ~$0.08/call worst case).

## Environment variables (`.env`)
```
ANTHROPIC_API_KEY=                 # claude-haiku-4-5
TICKETMASTER_API_KEY=
SEATGEEK_CLIENT_ID=                # blank/"pending_approval" → source skipped
BANDSINTOWN_APP_ID=
AIRTABLE_API_KEY=
GOOGLE_APPLICATION_CREDENTIALS=    # path to service account JSON (local only)
GOOGLE_SHEETS_ID=                  # main per-artist sheet (live, shared with team)
GOOGLE_DOC_ID=                     # routing Doc
BLOCKING_TEST_ID=                  # blocking email Doc
BACK_2_MAC_SHEETS_ID=              # Back 2 Mac source sheet
OUTPUT_WEBSITE_URL=                # .../wp-json/tour-dates/v1/ingest
OUTPUT_WEBSITE_SECRET=             # X-Tour-Secret for ingest + WP endpoints
WORDPRESS_ASSETS_DRIVE_FOLDER_ID=  # per-act image + description.txt fallbacks
COST_CAP_USD=                      # optional, default 2.00
```
`config._key_set(val)` returns False for empty strings and `"pending_approval"`.
The WordPress publish/cleanup/update-descriptions URLs are derived from
`OUTPUT_WEBSITE_URL` (swapping `/ingest`) unless overridden.

## CLI modes (`main.py`)
```bash
.venv/bin/python main.py                       # Full run (Airtable roster) → all outputs
.venv/bin/python main.py --artist "<name>"     # Single artist → Sheet + Doc(partial) +
                                               #   blocking Doc, then full front-end push
                                               #   (reads ALL artists back from the Sheet
                                               #   and re-posts so nobody is clobbered)
.venv/bin/python main.py --publish-events [--dry-run] [--artist X] [--limit N] [--one-month] [--verify-links]
.venv/bin/python main.py --add-show --artist X --date YYYY-MM-DD [--date ...] --venue V --city C [--region ST] [--ticket-url U] [--time "8:00 PM"] [--title T] [--dry-run]
.venv/bin/python main.py --cleanup-duplicates [--apply] [--force-delete]
.venv/bin/python main.py --update-descriptions --artist X [--dry-run]
.venv/bin/python main.py --doc-from-sheets     # Rebuild the Doc from current Sheet data
.venv/bin/python main.py --blocking-email-doc  # Rebuild blocking Doc from Sheet data
# Test/diagnostic modes (no production writes unless noted):
.venv/bin/python main.py --test-sheets         # Dummy data → Sheets
.venv/bin/python main.py --test-doc            # Dummy data → Doc
.venv/bin/python main.py --test-ticketmaster   # Ticketmaster only → Sheets
.venv/bin/python main.py --test-claude         # Ping test
.venv/bin/python main.py --test-claude-artist  # Web search for first artist → Sheets
.venv/bin/python main.py --test-claude-calls   # First 2 artists: scrape+search+enrich, logs only
.venv/bin/python main.py --test-airtable       # Print resolved roster
# Add --debug to any mode for DEBUG logging.
```

## Known issues / limitations
- **Duplicate shows**: same show from two sources with slightly different venue
  spellings ("Arcada Theatre" vs "The Arcada Theater") evades the MD5 dedup. Not a blocker.
- **Bandsintown widget sites**: A1A, Bohemian Queen, Free Fallin, Back 2 Mac use JS
  widgets; the REST API returns 0 without each artist's own `app_id`. Playwright
  intercepts the widget's internal API call (`BANDSINTOWN_WIDGET_PAGES`). Kiss The Sky
  works via REST using its hardcoded `app_id`.
- **Elvis website outdated**: only 2023–2024 events; Claude filters them as past →
  0 shows. Relies on web search + Ticketmaster.
- **Piano Man — cruise ships**: shows are cruise sailings with no public ticket URL, so
  enrichment finds nothing.
- **Dolly Show web search**: Claude confuses "The Dolly Show" (tribute) with Dolly
  Parton; the artist-website scrape covers it. The act tours the UK/Australia heavily —
  hence the `US_ONLY_ARTISTS` filter.
- **Atomic sheet writes**: tab writes are a values `update()` (not `clear()+update()`),
  but full-tab writes still briefly diverge while running; avoid running while the team
  is actively viewing.

## What NOT to do
- Don't call `write_website()` (or otherwise push the front-end) with a single
  artist's shows — it replaces the whole dataset. Read all artists from the Sheet first
  (the `--artist` flow already does this).
- Don't add per-show Claude calls — enrichment must stay one call per artist
  (`enrich_ticket_urls_for_artist`) / one batched pass per run.
- Don't raise `CLAUDE_MAX_TOKENS` without checking cost. Date-dense website scrapes
  use the separate `CLAUDE_WEBSITE_MAX_TOKENS`.
- Don't run destructive Sheets/WordPress operations while the team may be viewing.
- Don't commit `.env` or `*.json` (service-account) files (`.gitignore` covers these).

## External resources
- Google Sheet: `GOOGLE_SHEETS_ID` — shared with team, treat as live (and as the
  source of truth re-read for the front-end push).
- Service account JSON: `GOOGLE_APPLICATION_CREDENTIALS` — local machine only.
- Throttle state: `/tmp/tour_dates_throttle.txt` — persists rate-limit reset across runs.
- WordPress Tour Calendar plugin: `wordpress-plugin/tour-calendar/` (front-end render +
  copy-paste formats in `assets/formats.js`; ingest/publish/cleanup endpoints).

## Workflow Orchestration

### 1. Plan Mode Default
- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately — don't keep pushing
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity

### 2. Subagent Strategy
- Use subagents liberally to keep main context window clean
- Offload research, exploration, and parallel analysis to subagents
- For complex problems, throw more compute at it via subagents
- One task per subagent for focused execution

### 3. Self-Improvement Loop
- After ANY correction from the user: update `tasks/lessons.md` with the pattern
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for relevant project

### 4. Verification Before Done
- Never mark a task complete without proving it works
- Diff behavior between main and your changes when relevant
- Ask yourself: "Would a staff engineer approve this?"
- Run tests, check logs, demonstrate correctness

### 5. Demand Elegance (Balanced)
- For non-trivial changes: pause and ask "is there a more elegant way?"
- If a fix feels hacky: "Knowing everything I know now, implement the elegant solution"
- Skip this for simple, obvious fixes — don't over-engineer
- Challenge your own work before presenting it

### 6. Autonomous Bug Fixing
- When given a bug report: just fix it. Don't ask for hand-holding
- Point at logs, errors, failing tests — then resolve them
- Zero context switching required from the user
- Go fix failing CI tests without being told how

## Task Management
1. **Plan First**: Write plan to `tasks/todo.md` with checkable items
2. **Verify Plan**: Check in before starting implementation
3. **Track Progress**: Mark items complete as you go
4. **Explain Changes**: High-level summary at each step
5. **Document Results**: Add review section to `tasks/todo.md`
6. **Capture Lessons**: Update `tasks/lessons.md` after corrections

## Core Principles
- **Simplicity First**: Make every change as simple as possible. Impact minimal code.
- **No Laziness**: Find root causes. No temporary fixes. Senior developer standards.
- **Minimal Impact**: Changes should only touch what's necessary. Avoid introducing bugs.
