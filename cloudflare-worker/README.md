# lp-tour-dates-api — Cloudflare Worker mirror of the read API

## Why this exists

`GET https://www.loveproductions.com/wp-json/tour-dates/v1/shows` sits behind the
GoDaddy host's Imunify360 bot-protection, which blocks automation by source IP and
cannot be told to honor an API key. External consumers (a co-worker's AI bots) were
getting `403 Access denied by Imunify360 bot-protection` from a shared Cloudflare
egress IP. Whitelisting is a GoDaddy support ticket per IP, and the IP rotates.

This Worker serves the same feed from Cloudflare's edge instead. The pipeline already
builds the sanitized payload on the Mac before it reaches WordPress, so it simply
pushes that payload to two places: the WordPress plugin (unchanged) and this Worker.

```
main.py run  →  outputs/website.py::write_website()
                   ├→ POST https://www.loveproductions.com/wp-json/tour-dates/v1/ingest   (WordPress, as before)
                   └→ POST https://<worker>/ingest                                        (this Worker, KV "payload")

consumer     →  GET  https://<worker>/wp-json/tour-dates/v1/shows  + X-Tour-Key   (or /shows)
```

## Contract — identical to the WordPress route

| | WordPress plugin | This Worker |
|---|---|---|
| Read route | `GET …/wp-json/tour-dates/v1/shows` | `GET …/shows` — any path ending in `/shows`, so the WordPress-shaped path also works |
| Read credential | `X-Tour-Key: <key>` or `Authorization: Bearer <key>` | same headers; the key is **never** accepted in the query string |
| Filters | `artist`, `from`, `to`, `region`, `q`, `limit` (default 1000, max 5000), `offset` | same names, same validation, same error strings |
| Response | `{generated_at, total, count, shows}` sorted by date, artist, venue | same |
| Write route | `POST …/ingest` with `X-Tour-Secret` | same; the read key can never write |

`src/filters.js` is a line-for-line port of the plugin's `includes/read-filters.php`,
and `test/filters.test.js` mirrors `tests/read-api-test.php` check for check. **If you
change one side, change the other in the same commit.**

## First-time setup (Jon, once)

Needs Node 22+ (`brew install node@22`, or `nvm install 22`) and a Cloudflare account.

```bash
cd cloudflare-worker
npm install                       # installs wrangler locally
npx wrangler login                # opens the browser once

# 1. Create the KV namespace and paste the id it prints into wrangler.toml
npx wrangler kv namespace create TOUR_DATES

# 2. Set the two secrets. Use the SAME read key that is in WordPress → Settings → Tour
#    Calendar, so the consumer's existing key keeps working. Generate a fresh ingest
#    secret (it does not need to match the WordPress one, but it can).
npx wrangler secret put TOUR_READ_KEY
npx wrangler secret put TOUR_INGEST_SECRET      # e.g. openssl rand -hex 32

# 3. Deploy. Wrangler prints the URL, e.g. https://lp-tour-dates-api.<account>.workers.dev
npx wrangler deploy
```

Then in the pipeline's `.env`:

```
OUTPUT_MIRROR_URL=https://lp-tour-dates-api.<account>.workers.dev/ingest
OUTPUT_MIRROR_SECRET=<the TOUR_INGEST_SECRET you just set>
```

Prime the Worker without waiting for the next scheduled run: any pipeline mode that
calls `write_website()` will do, or push the last on-disk payload by hand:

```bash
curl -sS -X POST -H "X-Tour-Secret: $OUTPUT_MIRROR_SECRET" -H "Content-Type: application/json" \
  --data-binary @/tmp/tour_dates.json "$OUTPUT_MIRROR_URL"
```

**Careful:** `/tmp/tour_dates.json` (`OUTPUT_JSON_PATH`) is written by `write_json()`,
which does NOT strip private bookings — only `write_website()` does. Use the manual
push only to check plumbing, then let a real pipeline run overwrite it.

## Verify

```bash
curl -sS -H "X-Tour-Key: <key>" -H "Accept: application/json" \
  "https://lp-tour-dates-api.<account>.workers.dev/wp-json/tour-dates/v1/shows?limit=1"
# → {"generated_at":"…","total":N,"count":1,"shows":[{…}]}
```

Hand the consumer the new base URL. Nothing else about their integration changes.

## Day to day

```bash
npm test                 # 19 tests, no network, no wrangler needed
npx wrangler deploy      # after editing src/
npx wrangler tail        # live request log
```

Rotate the read key: `npx wrangler secret put TOUR_READ_KEY`, and change it in
WordPress → Settings → Tour Calendar at the same time so the two feeds stay in step.

## Limits

- Freshness equals the last pipeline run, exactly like the WordPress copy.
- The feed is a single KV value; the roster is a few hundred rows, far under KV's 25 MB.
- Free tier: 100k requests/day, which is orders of magnitude above this feed's traffic.
- Only `/shows` and `/ingest` exist here. The plugin's other write routes
  (publish-events, trash-events, …) are deliberately absent.
