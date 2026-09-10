# Tour Calendar — WordPress plugin

Self-contained front-end for the love-automations tour-date data: a sortable
**calendar + list**, **artist toggles**, and one-click **copy-paste** in four
outreach formats. No Flask, no external services, no build step — plain PHP +
vanilla JS that renders entirely in the browser from data the Python job pushes
once a week.

## How it fits together

```
main.py (weekly, GitHub Actions)
   └─ outputs/website.py::write_website()
        POST {generated_at, shows:[…]}  +  header X-Tour-Secret
            → /wp-json/tour-dates/v1/ingest   (this plugin)
                 stores payload in a wp_option
                     ├→ [tour-calendar] shortcode prints it inline
                     │    → assets/app.js renders calendar / list / copy tools
                     └→ GET /wp-json/tour-dates/v1/shows  + header X-Tour-Key
                          → external consumers (bots, scripts, partners)
```

## Two credentials — they are not interchangeable

| | **Ingest secret** | **Read key** |
| --- | --- | --- |
| Header | `X-Tour-Secret` | `X-Tour-Key` (or `Authorization: Bearer`) |
| Can do | Everything: replace the whole calendar, publish drafts, **trash event posts**, rewrite bios and images | Read `GET /shows`. Nothing else. |
| Who holds it | The Python job, and nobody else | Anyone you want to give read access |

**Never hand out the ingest secret.** It is a write credential — anyone holding
it can delete the site's event posts. The read key exists precisely so you never
have to. They have separate permission callbacks in the plugin; a read key sent
to any write endpoint gets a 403.

## Install

1. Zip the `tour-calendar/` directory and upload via **Plugins → Add New →
   Upload Plugin**, or copy it into `wp-content/plugins/`. Activate it.
2. Go to **Settings → Tour Calendar**. Copy the **Ingest secret** and note the
   **Ingest URL** (e.g. `https://example.com/wp-json/tour-dates/v1/ingest`).
   - Optional, more secure: pin the secret in `wp-config.php` instead of the DB:
     `define( 'TOUR_DATES_SECRET', 'your-long-random-string' );`
3. Create a page (e.g. `/tour-dates`), add the shortcode `[tour-calendar]`, and
   set the page visibility:
   - **Password protected** — one shared password to give artists, or
   - restrict to logged-in users (any membership/role plugin), or use the
     shortcode attribute `[tour-calendar require_login="yes"]`.

## Wire up the Python job

Set two env vars (locally in `.env`, and as GitHub Actions **secrets** — the
workflow already passes them through):

```
OUTPUT_WEBSITE_URL=https://example.com/wp-json/tour-dates/v1/ingest
OUTPUT_WEBSITE_SECRET=<the ingest secret from Settings → Tour Calendar>
```

The next `python main.py` run will push the data automatically. To push
on demand without a full run, POST the saved JSON:

```bash
curl -X POST "$OUTPUT_WEBSITE_URL" \
  -H "X-Tour-Secret: $OUTPUT_WEBSITE_SECRET" \
  -H "Content-Type: application/json" \
  --data @/tmp/tour_dates.json
```

A 200 response looks like `{"ok":true,"stored":214,"generated_at":"…"}`.

## Read-only API

For bots, scripts and partners who need the dates programmatically.

```
GET /wp-json/tour-dates/v1/shows
X-Tour-Key: <read key from Settings → Tour Calendar>
```

`Authorization: Bearer <key>` works too, for clients that default to it. The key
is **not** accepted as a query parameter — URLs end up in server and proxy logs.

### Parameters

All optional, all combined with AND.

| Param | Meaning |
| --- | --- |
| `artist` | Comma-separated act names. Matched loosely, so `The Dolly Show`, `the dolly show` and `the-dolly-show` are the same thing. Must be the whole name — `Dolly` alone matches nothing. |
| `from` / `to` | Inclusive `YYYY-MM-DD` bounds. A malformed date is a **400**, not a silently-dropped filter. |
| `region` | Comma-separated state codes, e.g. `TX,NY`. |
| `q` | Free-text substring across artist, venue and city. |
| `limit` | Rows to return. Default 1000, capped at 5000. |
| `offset` | Paging offset. Results are sorted by date, so paging is stable. |

### Response

```json
{
  "generated_at": "2026-09-08T06:12:44+00:00",
  "total": 312,
  "count": 2,
  "shows": [
    {
      "artist": "Bohemian Queen",
      "date": "2026-10-02",
      "venue": "Arcada Theatre",
      "city": "St. Charles",
      "region": "IL",
      "country": "US",
      "ticket_url": "https://arcadalive.com/event/…",
      "start_time": "20:00",
      "source": "bandsintown",
      "raw_id": "…",
      "title": ""
    }
  ]
}
```

`total` is how many shows matched the filters; `count` is how many came back in
this response. If `total > count`, page with `offset`.

Notes for whoever is consuming this:

- `artist` is the **display name** (what the public sees), not the internal roster name.
- `date` is `YYYY-MM-DD`; `start_time` is 24-hour `HH:MM`, or `""` when unknown.
- `region` is a US state code. The roster is US-only apart from the cruise acts.
- `ticket_url` may be `""` — a contracted show can be real and confirmed with no
  ticket link published yet. Don't treat a missing link as a missing show.
- The feed is **upcoming shows only**; past dates are dropped upstream.
- It refreshes **once a week**. Poll `generated_at` to see whether anything changed
  rather than re-pulling on a timer.
- Private/corporate bookings and cruise port calls are already excluded — see below.

### Examples

```bash
# Everything
curl -H "X-Tour-Key: $READ_KEY" \
  "https://example.com/wp-json/tour-dates/v1/shows"

# One act, this autumn
curl -H "X-Tour-Key: $READ_KEY" \
  "https://example.com/wp-json/tour-dates/v1/shows?artist=bohemian-queen&from=2026-09-01&to=2026-11-30"

# Two states, first 50
curl -H "Authorization: Bearer $READ_KEY" \
  "https://example.com/wp-json/tour-dates/v1/shows?region=TX,NY&limit=50"
```

### What the API does not expose

Private and corporate bookings (`config.is_private_booking`) and cruise-ship port
calls (`config.is_cruise_sailing`) are stripped by
`outputs/website.py::write_website()` before the payload is ever pushed here, so
they are absent from the stored data and therefore from this endpoint. The
endpoint does not re-filter — it inherits those exclusions. This is why the manual
paste below matters.

### Rotating the key

**Settings → Tour Calendar → Regenerate read key**. It takes effect immediately
and every current holder starts getting 403s, so tell them first. To keep the key
out of the database entirely, pin it in `wp-config.php`:

```php
define( 'TOUR_DATES_READ_KEY', 'your-long-random-string' );
```

## Manual fallback

If the weekly push ever fails, **Settings → Tour Calendar → Manual data paste**
accepts the payload JSON directly.

⚠️ **Paste the pushed payload, not `tour_dates.json` from disk.** `write_json()`
serializes shows *before* `write_website()` strips private/corporate bookings, so
that file still contains them — pasting it would publish private dates to the
public calendar and to the read API. Use the JSON from the `curl` push above.

## The four copy formats

Ported from `outputs/doc.py` (keep them in sync if the Doc format changes):

| Button        | Source in doc.py            | Contents                                        |
| ------------- | --------------------------- | ----------------------------------------------- |
| Email dates   | `_build_email_text`         | Booked shows, month headers, `Weekday\nCity, ST`|
| Zone lists    | `EMAIL_ZONES` grouping      | Same, grouped by geographic zone                |
| Open dates    | `_assemble_doc_sections`    | Season/month groups + OPEN fill-in days (±2)    |
| Simple list   | —                           | `Weekday, Month D, YYYY — Venue, City, ST` (+ links variant) |

All formats operate on the **currently filtered** shows (selected artists +
search + region). "Open dates" highlights fill-in days in the calendar when a
**single** artist is selected.

## Tests

The read endpoint's query parsing, filtering and credential check live in
`includes/read-filters.php`, which is deliberately free of WordPress calls so it
can be tested without a WP bootstrap or a test framework:

```bash
php wordpress-plugin/tour-calendar/tests/read-api-test.php
```

Keep that file pure. If a change there needs a WordPress function, it belongs in
the REST handler in `tour-calendar.php` instead.

## Notes

- **Vanilla JS, not Alpine.** Zero dependencies keeps it robust for a permanent,
  heavily-used fixture (no CDN load, no CSP issues, survives Noxe theme updates).
- Data shape is the unchanged contract from `outputs/json_output.py::write_json`.
- Cache busting is handled by the plugin version in the asset URLs; bump
  `TOUR_CALENDAR_VERSION` when you edit the assets.
