/**
 * Port of wordpress-plugin/tour-calendar/tests/read-api-test.php, check for check.
 * If a case is added on one side, add it on the other.
 *
 *     npm test   (from cloudflare-worker/)
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  READ_LIMIT_DEFAULT, READ_LIMIT_MAX, bearerToken, filterShows, parseReadArgs, readCredentialOk,
} from '../src/filters.js';

/** The fixture roster: two acts, a range of dates, one deliberately undated row. */
const SHOWS = [
  { artist: 'Bohemian Queen', date: '2026-10-02', venue: 'Arcada Theatre', city: 'St. Charles', region: 'IL', ticket_url: 'https://arcada.example/1' },
  { artist: 'Bohemian Queen', date: '2026-09-15', venue: 'Plaza Theatre', city: 'El Paso', region: 'TX', ticket_url: '' },
  { artist: 'The Dolly Show', date: '2026-09-15', venue: 'Grand Ole Hall', city: 'Nashville', region: 'TN', ticket_url: '' },
  { artist: 'The Dolly Show', date: '2026-11-20', venue: 'Beacon', city: 'New York', region: 'NY', ticket_url: '' },
  { artist: 'The Platters', date: '', venue: 'With Love Tour', city: '', region: '', ticket_url: '' },
];

function run(shows, query) {
  const args = parseReadArgs(query);
  return args.error ? args : filterShows(shows, args);
}

const keysOf = (result) => result.shows.map((s) => `${s.artist}|${s.date}`);

test('no filters: every row, sorted by date then artist (undated row first)', () => {
  const r = run(SHOWS, {});
  assert.equal(r.total, 5);
  assert.deepEqual(keysOf(r), [
    'The Platters|', 'Bohemian Queen|2026-09-15', 'The Dolly Show|2026-09-15', 'Bohemian Queen|2026-10-02', 'The Dolly Show|2026-11-20',
  ]);
});

test('artist filter: spelling variants all resolve to the same act', () => {
  for (const spelling of ['Bohemian Queen', 'bohemian queen', 'bohemian-queen', 'BohemianQueen']) {
    assert.equal(run(SHOWS, { artist: spelling }).total, 2, spelling);
  }
  assert.equal(run(SHOWS, { artist: 'Nobody At All' }).total, 0, 'unknown act');
  assert.equal(run(SHOWS, { artist: 'bohemian-queen,the-dolly-show' }).total, 4, 'comma-separated acts are ORed');
  assert.equal(run(SHOWS, { artist: 'Queen' }).total, 0, 'a partial name does NOT match');
});

test('date range is inclusive on both ends and drops the undated row', () => {
  assert.deepEqual(keysOf(run(SHOWS, { from: '2026-10-02' })), ['Bohemian Queen|2026-10-02', 'The Dolly Show|2026-11-20']);
  assert.equal(run(SHOWS, { to: '2026-09-15' }).total, 2);
  assert.equal(run(SHOWS, { from: '2026-09-01', to: '2026-10-31' }).total, 3);
  assert.equal(run(SHOWS, { from: '2000-01-01' }).total, 4);
});

test('date validation: a bad date is an error, never a silently-ignored filter', () => {
  assert.deepEqual(run(SHOWS, { from: '10/02/2026' }), { error: 'Invalid "from" date "10/02/2026" — expected YYYY-MM-DD.' });
  assert.ok(run(SHOWS, { from: '2026-13-01' }).error, 'impossible month');
  assert.ok(run(SHOWS, { to: '2026-02-30' }).error, 'impossible day (Feb 30)');
  assert.equal(run(SHOWS, { to: '2028-02-29' }).error, undefined, 'leap day is valid');
  assert.deepEqual(run(SHOWS, { from: '2026-12-01', to: '2026-01-01' }), { error: 'Invalid date range — "from" is after "to".' });
  assert.equal(run(SHOWS, { from: '' }).total, 5, 'empty date param is ignored, not an error');
});

test('region and free-text filters', () => {
  assert.equal(run(SHOWS, { region: 'tx' }).total, 1, 'region is case-insensitive');
  assert.equal(run(SHOWS, { region: 'TX,NY' }).total, 2, 'multiple regions');
  assert.equal(run(SHOWS, { q: 'arcada' }).total, 1, 'q matches a venue');
  assert.equal(run(SHOWS, { q: 'NASHVILLE' }).total, 1, 'q matches a city, case-insensitively');
  assert.equal(run(SHOWS, { artist: 'bohemian-queen', region: 'TX' }).total, 1, 'filters are ANDed');
});

test('paging: total is the match count, count is what came back', () => {
  const r = run(SHOWS, { limit: '2' });
  assert.equal(r.shows.length, 2);
  assert.equal(r.total, 5);
  const pages = [0, 2, 4].map((offset) => keysOf(run(SHOWS, { limit: '2', offset: String(offset) })));
  assert.deepEqual(pages.flat(), keysOf(run(SHOWS, {})), 'pages do not overlap');
  assert.deepEqual(run(SHOWS, { offset: '99' }).shows, []);
  assert.equal(parseReadArgs({ limit: '999999' }).limit, READ_LIMIT_MAX);
  assert.equal(parseReadArgs({}).limit, READ_LIMIT_DEFAULT);
  assert.ok(run(SHOWS, { limit: '0' }).error, 'limit 0 is rejected');
  assert.ok(run(SHOWS, { offset: '-1' }).error, 'negative offset is rejected');
  assert.ok(run(SHOWS, { limit: 'abc' }).error, 'non-numeric limit is rejected (PHP casts it to 0)');
});

test('degenerate input', () => {
  assert.deepEqual(run([], {}), { total: 0, shows: [] });
  assert.equal(run([], { artist: 'x', from: '2026-01-01' }).total, 0);
  assert.equal(run(['garbage', null, 7, SHOWS[0]], {}).total, 1, 'non-object rows are skipped');
  assert.equal(run([{ artist: 'X' }], { q: 'x' }).total, 1, 'rows missing fields do not throw');
  assert.equal(run(undefined, {}).total, 0, 'a missing payload is an empty roster');
});

test('auth: the one function between a third party\'s bot and the site', () => {
  const KEY = 'read-key-aaaaaaaaaaaaaaaaaaaa';
  const SEC = 'ingest-secret-bbbbbbbbbbbb';
  assert.equal(readCredentialOk(KEY, SEC, KEY, ''), true, 'correct read key');
  assert.equal(readCredentialOk(KEY, SEC, '', SEC), true, 'ingest secret is a superset');
  assert.equal(readCredentialOk(KEY, SEC, 'nope', ''), false);
  assert.equal(readCredentialOk(KEY, SEC, '', ''), false, 'no credential');
  assert.equal(readCredentialOk(KEY, SEC, '', KEY), false, 'read key in the secret slot');
  assert.equal(readCredentialOk(KEY, SEC, SEC, ''), false, 'secret in the key slot');
  assert.equal(readCredentialOk(KEY, SEC, KEY.slice(0, -1), ''), false, 'a prefix of the key');
  assert.equal(readCredentialOk(KEY, SEC, `${KEY} `, ''), false, 'key plus trailing space');
  // The empty-vs-empty trap: an unconfigured deployment must not authenticate a request that sent nothing.
  assert.equal(readCredentialOk('', SEC, '', ''), false);
  assert.equal(readCredentialOk('', '', '', ''), false);
  assert.equal(readCredentialOk('', '', 'guess', 'guess'), false);
  assert.equal(readCredentialOk(undefined, undefined, undefined, undefined), false, 'missing bindings reject everything');
});

test('bearer parsing', () => {
  const KEY = 'read-key-aaaaaaaaaaaaaaaaaaaa';
  assert.equal(bearerToken(`Bearer ${KEY}`), KEY);
  assert.equal(bearerToken(`bearer ${KEY}`), KEY, 'scheme is case-insensitive');
  assert.equal(bearerToken(`Bearer   ${KEY}  `), KEY, 'surrounding whitespace trimmed');
  assert.equal(bearerToken(`Basic ${KEY}`), '', 'Basic auth is not a bearer token');
  assert.equal(bearerToken(''), '');
  assert.equal(bearerToken(null), '', 'absent header');
  assert.equal(bearerToken('Bearer '), '', 'scheme with no token');
  assert.equal(bearerToken('bearerish'), '', 'a token that merely starts with "bearer" is not unwrapped');
});
