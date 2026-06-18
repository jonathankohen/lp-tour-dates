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
