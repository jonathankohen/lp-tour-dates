/**
 * End-to-end tests for the fetch handler with an in-memory KV stand-in.
 * Node 20's global Request/Response are the same Web APIs Workers use.
 *
 *     npm test   (from cloudflare-worker/)
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import worker from '../src/index.js';

const KEY = 'read-key-aaaaaaaaaaaaaaaaaaaa';
const SEC = 'ingest-secret-bbbbbbbbbbbb';
const BASE = 'https://tour-dates.example';

function fakeKv(initial = {}) {
  const store = new Map(Object.entries(initial));
  return {
    store,
    async get(key, opts) {
      const v = store.get(key);
      if (v === undefined) return null;
      return opts && opts.type === 'json' ? JSON.parse(v) : v;
    },
    async put(key, value) {
      store.set(key, value);
    },
  };
}

function env(overrides = {}) {
  return { TOUR_DATES: fakeKv(), TOUR_READ_KEY: KEY, TOUR_INGEST_SECRET: SEC, ...overrides };
}

const call = (e, path, init = {}) => worker.fetch(new Request(BASE + path, init), e);

const PAYLOAD = {
  generated_at: '2026-09-15T12:00:00+00:00',
  shows: [
    { artist: 'Bohemian Queen', date: '2026-10-02', venue: 'Arcada Theatre', city: 'St. Charles', region: 'IL', ticket_url: 'https://arcada.example/1', extra: 'dropped' },
    { artist: 'The Dolly Show', date: '2026-09-15', venue: 'Grand Ole Hall', city: 'Nashville', region: 'TN' },
    { artist: '', date: '2026-09-15', venue: 'no artist' },
    { artist: 'No Date', date: '' },
    'garbage',
  ],
};

async function ingest(e, body = PAYLOAD, headers = { 'X-Tour-Secret': SEC }) {
  return call(e, '/wp-json/tour-dates/v1/ingest', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...headers },
    body: typeof body === 'string' ? body : JSON.stringify(body),
  });
}

test('ingest then read: the WordPress-shaped path and the short path both serve the stored payload', async () => {
  const e = env();
  const r = await ingest(e);
  assert.equal(r.status, 200);
  assert.deepEqual(await r.json(), { ok: true, stored: 2, generated_at: PAYLOAD.generated_at });

  for (const path of ['/wp-json/tour-dates/v1/shows?limit=1', '/shows?limit=1']) {
    const res = await call(e, path, { headers: { 'X-Tour-Key': KEY, Accept: 'application/json' } });
    assert.equal(res.status, 200, path);
    assert.equal(res.headers.get('cache-control'), 'no-store');
    assert.equal(res.headers.get('content-type'), 'application/json; charset=UTF-8');
    const body = await res.json();
    assert.equal(body.generated_at, PAYLOAD.generated_at);
    assert.equal(body.total, 2);
    assert.equal(body.count, 1);
    assert.equal(body.shows[0].artist, 'The Dolly Show', 'sorted by date');
    assert.equal(body.shows[0].extra, undefined, 'unknown keys are dropped at ingest');
    assert.equal(body.shows[0].ticket_url, '', 'known keys are always present');
  }
});

test('read auth: X-Tour-Key, Bearer and the ingest secret are accepted; nothing else is', async () => {
  const e = env();
  await ingest(e);
  const ok = async (headers) => (await call(e, '/shows', { headers })).status;
  assert.equal(await ok({ 'X-Tour-Key': KEY }), 200);
  assert.equal(await ok({ Authorization: `Bearer ${KEY}` }), 200);
  assert.equal(await ok({ 'X-Tour-Secret': SEC }), 200, 'ingest secret is a superset');
  assert.equal(await ok({}), 403, 'no credential');
  assert.equal(await ok({ 'X-Tour-Key': 'wrong' }), 403);
  assert.equal(await ok({ Authorization: `Basic ${KEY}` }), 403);
  assert.equal(await ok({ 'X-Tour-Key': SEC }), 403, 'secret in the key slot');
  const forbidden = await call(e, '/shows', {});
  assert.deepEqual(await forbidden.json(), {
    code: 'tour_calendar_forbidden',
    message: 'Invalid or missing read key. Send it as the X-Tour-Key header or as Authorization: Bearer <key>.',
    data: { status: 403 },
  });
});

test('the key is never accepted in the query string', async () => {
  const e = env();
  await ingest(e);
  assert.equal((await call(e, `/shows?key=${KEY}`)).status, 403);
  assert.equal((await call(e, `/shows?x-tour-key=${KEY}`)).status, 403);
  assert.equal((await call(e, `/shows?token=${KEY}`)).status, 403);
});

test('filters and errors pass through the handler', async () => {
  const e = env();
  await ingest(e);
  const h = { 'X-Tour-Key': KEY };
  const res = await call(e, '/shows?artist=bohemian-queen&region=il', { headers: h });
  assert.equal((await res.json()).total, 1);
  const bad = await call(e, '/shows?from=10/02/2026', { headers: h });
  assert.equal(bad.status, 400);
  assert.deepEqual(await bad.json(), { error: 'Invalid "from" date "10/02/2026" — expected YYYY-MM-DD.' });
});

test('an empty store reads as an empty roster, not an error', async () => {
  const res = await call(env(), '/shows', { headers: { 'X-Tour-Key': KEY } });
  assert.equal(res.status, 200);
  assert.deepEqual(await res.json(), { generated_at: '', total: 0, count: 0, shows: [] });
});

test('unconfigured deployment refuses to serve', async () => {
  const res = await call(env({ TOUR_READ_KEY: undefined, TOUR_INGEST_SECRET: undefined }), '/shows', { headers: { 'X-Tour-Key': '' } });
  assert.equal(res.status, 500);
  assert.equal((await res.json()).code, 'tour_calendar_no_key');
});

test('ingest auth: only the ingest secret writes; the read key never does', async () => {
  const e = env();
  assert.equal((await ingest(e, PAYLOAD, {})).status, 403, 'no credential');
  assert.equal((await ingest(e, PAYLOAD, { 'X-Tour-Key': KEY })).status, 403, 'read key');
  assert.equal((await ingest(e, PAYLOAD, { 'X-Tour-Secret': SEC.slice(0, -1) })).status, 403, 'prefix of the secret');
  assert.equal(e.TOUR_DATES.store.size, 0, 'nothing was written');
  const unconfigured = env({ TOUR_INGEST_SECRET: '' });
  assert.equal((await ingest(unconfigured, PAYLOAD, { 'X-Tour-Secret': '' })).status, 403, 'empty-vs-empty is rejected');
});

test('ingest validation', async () => {
  const e = env();
  assert.equal((await ingest(e, 'not json')).status, 400);
  assert.equal((await ingest(e, { shows: 'nope' })).status, 400);
  assert.equal((await ingest(e, [])).status, 400);
  assert.equal(e.TOUR_DATES.store.size, 0);
  const noStamp = await ingest(e, { shows: PAYLOAD.shows });
  assert.match((await noStamp.json()).generated_at, /^\d{4}-\d{2}-\d{2}T/, 'generated_at defaults to now');
});

test('a second ingest replaces the whole dataset', async () => {
  const e = env();
  await ingest(e);
  await ingest(e, { generated_at: 'later', shows: [{ artist: 'Only One', date: '2027-01-01' }] });
  const body = await (await call(e, '/shows', { headers: { 'X-Tour-Key': KEY } })).json();
  assert.equal(body.generated_at, 'later');
  assert.deepEqual(body.shows.map((s) => s.artist), ['Only One']);
});

test('method and route handling', async () => {
  const e = env();
  assert.equal((await call(e, '/shows', { method: 'POST', headers: { 'X-Tour-Key': KEY } })).status, 405);
  assert.equal((await call(e, '/ingest', { method: 'GET', headers: { 'X-Tour-Secret': SEC } })).status, 405);
  assert.equal((await call(e, '/')).status, 404);
  assert.equal((await call(e, '/wp-json/tour-dates/v1/publish-events', { method: 'POST' })).status, 404, 'write routes other than ingest do not exist here');
  const head = await call(e, '/shows', { method: 'HEAD', headers: { 'X-Tour-Key': KEY } });
  assert.equal(head.status, 200);
  assert.equal(await head.text(), '');
});
