/**
 * lp-tour-dates-api — a mirror of the Tour Calendar plugin's read-only API that
 * does not sit behind the GoDaddy host's Imunify360 bot-protection.
 *
 *   POST …/ingest   X-Tour-Secret: <secret>   { generated_at?, shows: [...] }
 *                   Written by outputs/website.py on every pipeline run — the
 *                   same sanitized payload it POSTs to WordPress.
 *   GET  …/shows    X-Tour-Key: <key>  (or Authorization: Bearer <key>)
 *                   Same filters, same response shape as the WordPress route.
 *
 * Routing is by the LAST path segment only, so a consumer can point at
 *   https://<worker>/wp-json/tour-dates/v1/shows
 * and keep their WordPress-shaped path, or use the short /shows.
 *
 * Bindings (wrangler.toml / `wrangler secret put`):
 *   TOUR_DATES          KV namespace — one key, "payload", the stored JSON.
 *   TOUR_READ_KEY       secret — the read key handed to consumers.
 *   TOUR_INGEST_SECRET  secret — the write secret; only the pipeline holds it.
 *
 * The read key is never accepted in the query string: URLs land in access logs
 * and Referer headers, which is how a long-lived shared key leaks.
 */

import { bearerToken, filterShows, parseReadArgs, readCredentialOk, timingSafeEqual } from './filters.js';

const PAYLOAD_KEY = 'payload';
const SHOW_FIELDS = ['artist', 'date', 'venue', 'city', 'region', 'country', 'ticket_url', 'source', 'raw_id', 'start_time', 'title'];

function json(body, status = 200, headers = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      'Content-Type': 'application/json; charset=UTF-8',
      // Authenticated responses: keep them out of shared/proxy caches.
      'Cache-Control': 'no-store',
      'X-Robots-Tag': 'noindex',
      ...headers,
    },
  });
}

/** Mirrors the plugin's WP_Error shape so consumers see identical errors. */
function wpError(code, message, status) {
  return json({ code, message, data: { status } }, status);
}

function lastSegment(pathname) {
  const parts = pathname.split('/').filter(Boolean);
  return parts.length ? parts[parts.length - 1] : '';
}

/**
 * Validate and normalize an ingest body exactly as the plugin does: unknown keys
 * on each show are dropped, known keys are coerced to strings, and a row without
 * both an artist and a date is skipped.
 */
export function cleanPayload(body) {
  if (body === null || typeof body !== 'object' || Array.isArray(body) || !Array.isArray(body.shows)) {
    return null;
  }
  const clean = [];
  for (const show of body.shows) {
    if (show === null || typeof show !== 'object' || Array.isArray(show)) {
      continue;
    }
    const row = {};
    for (const f of SHOW_FIELDS) {
      row[f] = show[f] === undefined || show[f] === null ? '' : String(show[f]);
    }
    if (row.artist === '' || row.date === '') {
      continue;
    }
    clean.push(row);
  }
  const generatedAt = body.generated_at === undefined || body.generated_at === null
    ? new Date().toISOString()
    : String(body.generated_at).trim().slice(0, 64);
  return { generated_at: generatedAt, shows: clean };
}

async function handleIngest(request, env) {
  if (request.method !== 'POST' && request.method !== 'PUT') {
    return json({ error: 'Method not allowed.' }, 405, { Allow: 'POST, PUT' });
  }
  const secret = String(env.TOUR_INGEST_SECRET ?? '');
  const provided = request.headers.get('x-tour-secret') ?? '';
  // The read key is deliberately NOT accepted here — it must never write.
  if (secret === '' || provided === '' || !timingSafeEqual(secret, provided)) {
    return wpError('tour_calendar_forbidden', 'Invalid or missing ingest secret.', 403);
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: 'Body must be an object with a "shows" array.' }, 400);
  }
  const payload = cleanPayload(body);
  if (!payload) {
    return json({ error: 'Body must be an object with a "shows" array.' }, 400);
  }

  await env.TOUR_DATES.put(PAYLOAD_KEY, JSON.stringify(payload));
  return json({ ok: true, stored: payload.shows.length, generated_at: payload.generated_at });
}

async function handleShows(request, env, url) {
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    return json({ error: 'Method not allowed.' }, 405, { Allow: 'GET, HEAD' });
  }
  const readKey = String(env.TOUR_READ_KEY ?? '');
  const secret = String(env.TOUR_INGEST_SECRET ?? '');
  if (readKey === '' && secret === '') {
    return wpError('tour_calendar_no_key', 'No read key is configured on the server.', 500);
  }

  let provided = request.headers.get('x-tour-key') ?? '';
  if (provided === '') {
    provided = bearerToken(request.headers.get('authorization'));
  }
  const providedSecret = request.headers.get('x-tour-secret') ?? '';
  if (!readCredentialOk(readKey, secret, provided, providedSecret)) {
    return wpError(
      'tour_calendar_forbidden',
      'Invalid or missing read key. Send it as the X-Tour-Key header or as Authorization: Bearer <key>.',
      403,
    );
  }

  const args = parseReadArgs(Object.fromEntries(url.searchParams));
  if (args.error) {
    return json({ error: args.error }, 400);
  }

  const stored = await env.TOUR_DATES.get(PAYLOAD_KEY, { type: 'json' });
  const shows = stored && Array.isArray(stored.shows) ? stored.shows : [];
  const generatedAt = stored && stored.generated_at ? String(stored.generated_at) : '';

  const result = filterShows(shows, args);
  const response = json({
    generated_at: generatedAt,
    total: result.total,
    count: result.shows.length,
    shows: result.shows,
  });
  return request.method === 'HEAD' ? new Response(null, response) : response;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    switch (lastSegment(url.pathname)) {
      case 'shows':
        return handleShows(request, env, url);
      case 'ingest':
        return handleIngest(request, env);
      default:
        return wpError('rest_no_route', 'No route was found matching the URL and request method.', 404);
    }
  },
};
