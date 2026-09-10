# Tasks

Working plan for the current task. See `CLAUDE.md` → **Task Management** for the workflow.

How to use this file:
1. **Plan first** — before implementing, write the task as a checklist of small,
   verifiable steps under a dated heading.
2. **Verify the plan** — check in with the user before starting non-trivial work.
3. **Track progress** — mark items `[x]` as you complete them.
4. **Document results** — when the task is done, add a short **Review** block:
   what changed, how it was verified, and anything left open.

Keep this file current: clear or archive finished tasks so the top of the file is
always the work in flight. Capture any user corrections in `lessons.md`.

---

## Template (copy for each new task)

### YYYY-MM-DD — <task title>
**Goal:** <one-line description of the desired outcome>

- [ ] Step 1
- [ ] Step 2
- [ ] Step 3

**Review:**
- _What changed:_
- _How verified:_
- _Open items / follow-ups:_

---

## Active

### 2026-09-10 — Read-only API for external consumers
**Goal:** Let a co-worker's AI bots read tour dates without holding a credential that can
change anything on the live site.

- [x] Add a read key (`tour_dates_read_key`) separate from the ingest secret, with a
      `TOUR_DATES_READ_KEY` wp-config override and an `admin_init` backfill (the activation
      hook does not re-run on a plugin upgrade)
- [x] Add `GET /wp-json/tour-dates/v1/shows` with its own permission callback — accepts
      `X-Tour-Key` / `Authorization: Bearer`, plus the ingest secret as a superset; never a
      query parameter
- [x] Filters: `artist`, `from`/`to`, `region`, `q`, `limit`/`offset`; response carries
      `total` vs `count` and sorts by date so paging is stable
- [x] Keep parsing/filtering/credential logic WP-free in `includes/read-filters.php`
- [x] Settings page: read-key display + regenerate, read API URL, "do not share" warning on
      the ingest secret, and a warning not to paste `tour_dates.json`
- [x] Docs: plugin README (the section to hand the co-worker), CLAUDE.md, this file
- [x] Tests + version bump to 1.6.0

**Review:**
- _What changed:_ `wordpress-plugin/tour-calendar/tour-calendar.php` (read key helpers, route,
  permission callback, handler, settings UI, 1.5.1/1.4.0 → 1.6.0 for both version strings,
  which had drifted apart); new `includes/read-filters.php` and `tests/read-api-test.php`;
  README, CLAUDE.md. **No Python changed** — the pipeline neither consumes nor needs this.
- _How verified:_ `php -l` on all three PHP files; `php tests/read-api-test.php` → 53 checks
  pass (artist-name variants, inclusive date bounds, malformed/reversed ranges, limit cap,
  offset paging, empty payload, and the credential matrix incl. the `hash_equals('','')`
  empty-vs-empty trap); a throwaway end-to-end harness loaded the real plugin against WP
  stubs and confirmed the route wiring, that all nine write routes still use the ingest
  callback, that a read key sent to a write route gets 403, and that a key in the query
  string does not authenticate. `pytest tests/` still green (guard against stray edits).
- _Open items / follow-ups:_
  - **Not deployed.** The endpoint 404s until the plugin zip is uploaded (v1.6.0+). Live
    smoke tests (403 on no/wrong key, read key rejected by `/trash-events`) still to run.
  - `write_json()` still emits private/corporate bookings to `tour_dates.json`; the manual
    paste box is warned rather than fixed. Making `write_json` strip them would close the
    gap properly.
  - The read key is shared, so usage can't be attributed per consumer. Named per-consumer
    keys were considered and deferred; revisit if more than one outside party needs access.
  - `tests/read-api-test.php` is not wired into CI (CI runs pytest only).

---


## Done

_(Move completed tasks here, or delete once their lessons are captured.)_
