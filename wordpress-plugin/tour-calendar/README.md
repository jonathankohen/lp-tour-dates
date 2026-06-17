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
                     → [tour-calendar] shortcode prints it inline
                          → assets/app.js renders calendar / list / copy tools
```

There is **no public read endpoint**. The JSON is only printed on a page that
contains the `[tour-calendar]` shortcode, so access control is simply the
visibility you set on that page.

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

## Manual fallback

If the weekly push ever fails, **Settings → Tour Calendar → Manual data paste**
accepts the contents of `tour_dates.json` directly.

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

## Notes

- **Vanilla JS, not Alpine.** Zero dependencies keeps it robust for a permanent,
  heavily-used fixture (no CDN load, no CSP issues, survives Noxe theme updates).
- Data shape is the unchanged contract from `outputs/json_output.py::write_json`.
- Cache busting is handled by the plugin version in the asset URLs; bump
  `TOUR_CALENDAR_VERSION` when you edit the assets.
