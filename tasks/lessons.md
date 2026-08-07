# Lessons

Durable rules learned from user corrections, so the same mistake isn't repeated.
See `CLAUDE.md` → **Self-Improvement Loop**.

How to use this file:
- **After ANY correction from the user**, add an entry capturing the pattern — not
  just the one-off fix, but the general rule that prevents it next time.
- Write each lesson as an actionable rule ("Always… / Never… / Before X, do Y"),
  with a one-line **Why** so the reasoning survives.
- Review relevant lessons at the start of a session before touching that area.
- Prune or merge lessons that are obsolete or now encoded in `CLAUDE.md`/code.

---

## Template (copy for each new lesson)

### YYYY-MM-DD — <short rule title>
**Rule:** <what to always/never do>
**Why:** <the correction or reasoning behind it>
**Applies to:** <area: aggregation / outputs / sheets / front-end / CLI / …>

---

## Lessons

### 2026-06-17 — Link verification must confirm the act BY NAME and constrain the domain
**Rule:** When auto-adopting a ticket URL, (1) confirm the page contains the full act
*name phrase* (e.g. "bohemianqueen"), not just one common word like "queen"; (2) reject
clearly-non-ticket paths (hotel room-rate, /rooms, /dining); and (3) for search results,
require the result domain to relate to the venue (venue-name token in the host) or be a
known ticketing host — otherwise aggregators/social/blogs that merely mention the act +
date get adopted. See `page_confirms_event`, `config._is_non_ticket_url`,
`enrichment._acceptable_venue_result`.
**Why:** A live run adopted `southpointcasino.com/hotel/room-rate-calendar` for Bohemian
Queen (matched "Queen" bed + a calendar date), plus TikTok/Facebook/blog/aggregator links.
**Applies to:** enrichment / verification

### 2026-06-17 — Default to --dry-run for link verification; it writes to live site/Sheet
**Rule:** Always demonstrate `--verify-links[-local]` with `--dry-run` (and ideally a
single `--artist`) before a live run. Writes (Sheet, front-end, event posts) only happen
after verification completes, so interrupting mid-verify is safe — but never assume.
**Why:** A live full run was started by mistake and was alarming; it was safely interrupted
before the write phase.
**Applies to:** CLI / outputs


### 2026-06-17 — Never push the front-end with a single artist's shows
**Rule:** `outputs/website.write_website()` replaces the entire front-end dataset.
Before pushing for one artist, read all artists back from the Sheet
(`utils.read_shows_from_sheets()`) and post the merged set. The `--artist` CLI flow
already does this — follow that pattern.
**Why:** Posting only one artist's shows would wipe the other ~20 acts from the live
WordPress calendar.
**Applies to:** outputs / front-end

### 2026-06-17 — Keep CLAUDE.md in sync with the code
**Rule:** When changing architecture, sources, outputs, CLI flags, config knobs, or
discovering a scraping quirk, update the matching CLAUDE.md section in the same change.
**Why:** CLAUDE.md had drifted to describing a single-file `main.py` long after the
code was split into modules; stale onboarding docs cost real time.
**Applies to:** docs / all

### 2026-06-24 — Validate fuzzy source results against the act's real name
**Rule:** Never trust a source that was queried by a fuzzy keyword/name (Ticketmaster
`keyword=`, Claude web search) to have returned the right act. Capture the source's real
performer/attraction name and require the act's *distinctive whole name to appear
consecutively and in order* (`config.act_name_matches`) before keeping the show — a single
shared word is not enough. For sources with no performer field (web search), confirm against
the ticket page and drop only on positive disconfirmation; keep-but-flag when unverifiable.
**Why:** "Bohemian Queen" shows were contaminated with "Queen by The Bohemians" dates because
the Ticketmaster keyword search matched the shared words *Queen*/*Bohemian* and the source
stamped every result with the requested artist name. Wrong dates went out in an email.
**Applies to:** sources / aggregation

### 2026-08-04 — Check a source's event STATUS, not just its dates
**Rule:** When a source exposes a per-event status/state field, read it. Ticketmaster's
Discovery API keeps cancelled and postponed events in the search results, on their original
date, flagged only by `dates.status.code` — `sources/ticketmaster.py` now drops
`cancelled`/`canceled`/`postponed`. Do NOT drop `offsale` (sold out / not yet on sale — the
show is real) or `rescheduled` (the record already carries the new date).
**Why:** The team reported Bohemian Queen 9/3 as a "false positive" and The Platters 10/3 as
cancelled. Both were literally `status: cancelled` in the API; we were publishing called-off
shows as real ones. The first instinct — a hardcoded per-show suppression list — would have
papered over a general bug that affects every act on every run.
**Applies to:** sources / aggregation

### 2026-08-04 — Prefer a source-level root fix over a hardcoded correction list
**Rule:** When the team reports a bad date, find out WHY the source emitted it before
recording an override. A suppression list is a last resort for data no source can be made to
get right; it also silently kills a genuine future booking on the same artist+date.
**Why:** Both reported bad dates shared one root cause the API told us about directly.
**Applies to:** sources / aggregation

### 2026-08-04 — Guardless write paths need the same preflight as the guarded one
**Rule:** `main.py --artist` overwrites an artist's tab with NO regression guard (rebasing
counts is its purpose), so it must run `_preflight_browser()` and abort when the Playwright
browser is missing. It now does.
**Why:** With the browser binary absent, The Platters' render-based website scrape returned 0
and the guardless `--artist` run rewrote its tab from 20 shows to 1. The full `run()` had had
this preflight for exactly this reason; only the guardless path lacked it.
**Applies to:** CLI / outputs

### 2026-08-04 — Make environment drift self-heal, don't re-fix it manually
**Rule:** A dependency whose binary lives OUTSIDE the venv (Playwright's chromium in
`~/Library/Caches/ms-playwright`) drifts every time the package is upgraded or the venv is
rebuilt. Don't keep running `playwright install` by hand — heal it in code at the single
chokepoint (`sources/browser.py::browser_page`), matched narrowly on that one error, once
per process, then retry.
**Why:** the missing browser had bitten this project repeatedly, and because a failed scrape
returns 0 shows instead of raising, each occurrence risked publishing a wiped tab.
**Applies to:** sources / tooling

### 2026-08-04 — Never append to a file with `cat >>` in this shell
**Rule:** `cat` is aliased to a syntax highlighter here, so `cat >> file <<'EOF'` writes
ANSI colour escapes into the file instead of the text. Use the Write/Edit tools.
**Why:** It corrupted both `tasks/lessons.md` and the memory index in one session.
**Applies to:** tooling / all

### 2026-08-06 — Don't flag a formatting defect you can see; fix it
**Rule:** When a report is "this output looks wrong", the deliverable is output that looks
right. If while fixing the reported symptom you can see a second defect in the same output,
fix that too instead of listing it as optional follow-up. Flagging is for work that's genuinely
out of scope or needs a decision — not for "I noticed but stopped".
**Why:** Fixing the invalid Gutenberg blocks on event 11225 left the bio still reading as
run-on prose with stranded labels ("…classic concerts. Sound * The ONLY Hendrix tribute…").
I reported that as a known limitation; the user had to come back with "something is still off"
and paste the body. The real root cause — the plugin unwrapping deliberate line breaks — was
one function away and needed no plugin deploy.
**Applies to:** outputs / all

### 2026-08-06 — Derive a heuristic's threshold from the whole corpus, and audit every decision
**Rule:** Before shipping a rule that classifies data (soft wrap vs. deliberate break), dump ALL
23 bios and print the rule's verdict on every single case. Tune until the exceptions are ones you
can name. Don't infer the rule from the one file that prompted the report.
**Why:** The first two attempts each looked right on Kiss The Sky and silently mis-joined the
Legends of Classic Rock band roster — first because a bold lead-in wasn't recognised as a new
unit, then because `<strong>` tags inflated a short line past the length threshold. Only the
full-corpus audit (26 breaks / 1 join) exposed both.
**Applies to:** outputs / aggregation
