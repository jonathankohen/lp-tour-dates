<?php

/**
 * Tests for the read-only /shows endpoint's query parsing and filtering.
 *
 * No framework and no WordPress — includes/read-filters.php is deliberately pure,
 * so this runs anywhere PHP does:
 *
 *     php wordpress-plugin/tour-calendar/tests/read-api-test.php
 *
 * Exits non-zero on the first failure, so it can be wired into CI as-is.
 */

define('TOUR_CALENDAR_READ_LIMIT_DEFAULT', 1000);
define('TOUR_CALENDAR_READ_LIMIT_MAX', 5000);

require_once __DIR__ . '/../includes/read-filters.php';

$failures = 0;
$checks   = 0;

function check($label, $actual, $expected)
{
	global $failures, $checks;
	$checks++;
	if ($actual === $expected) {
		echo "  ok   $label\n";
		return;
	}
	$failures++;
	echo "  FAIL $label\n";
	echo "       expected: " . json_encode($expected) . "\n";
	echo "       actual:   " . json_encode($actual) . "\n";
}

/** The fixture roster: two acts, a range of dates, one deliberately undated row. */
$SHOWS = array(
	array('artist' => 'Bohemian Queen', 'date' => '2026-10-02', 'venue' => 'Arcada Theatre', 'city' => 'St. Charles', 'region' => 'IL', 'ticket_url' => 'https://arcada.example/1'),
	array('artist' => 'Bohemian Queen', 'date' => '2026-09-15', 'venue' => 'Plaza Theatre', 'city' => 'El Paso', 'region' => 'TX', 'ticket_url' => ''),
	array('artist' => 'The Dolly Show', 'date' => '2026-09-15', 'venue' => 'Grand Ole Hall', 'city' => 'Nashville', 'region' => 'TN', 'ticket_url' => ''),
	array('artist' => 'The Dolly Show', 'date' => '2026-11-20', 'venue' => 'Beacon', 'city' => 'New York', 'region' => 'NY', 'ticket_url' => ''),
	array('artist' => 'The Platters', 'date' => '', 'venue' => 'With Love Tour', 'city' => '', 'region' => '', 'ticket_url' => ''),
);

function run(array $shows, array $query)
{
	$args = tour_calendar_parse_read_args($query);
	if (isset($args['error'])) {
		return $args;
	}
	return tour_calendar_filter_shows($shows, $args);
}

/** Pull just the artist+date of each returned row, for compact assertions. */
function keys_of(array $result)
{
	return array_map(
		function ($s) {
			return $s['artist'] . '|' . $s['date'];
		},
		$result['shows']
	);
}

echo "\nNo filters\n";
$r = run($SHOWS, array());
check('returns every row', $r['total'], 5);
check(
	'sorted by date, then artist (undated row sorts first)',
	keys_of($r),
	array('The Platters|', 'Bohemian Queen|2026-09-15', 'The Dolly Show|2026-09-15', 'Bohemian Queen|2026-10-02', 'The Dolly Show|2026-11-20')
);

echo "\nArtist filter — spelling variants all resolve to the same act\n";
foreach (array('Bohemian Queen', 'bohemian queen', 'bohemian-queen', 'BohemianQueen') as $spelling) {
	$r = run($SHOWS, array('artist' => $spelling));
	check("\"$spelling\" matches 2 shows", $r['total'], 2);
}
check('an unknown act matches nothing', run($SHOWS, array('artist' => 'Nobody At All'))['total'], 0);
check('comma-separated acts are ORed', run($SHOWS, array('artist' => 'bohemian-queen,the-dolly-show'))['total'], 4);
check('a partial name does NOT match', run($SHOWS, array('artist' => 'Queen'))['total'], 0);

echo "\nDate range — inclusive on both ends\n";
check('from= includes its own date', keys_of(run($SHOWS, array('from' => '2026-10-02'))), array('Bohemian Queen|2026-10-02', 'The Dolly Show|2026-11-20'));
check('to= includes its own date', run($SHOWS, array('to' => '2026-09-15'))['total'], 2);
check('from+to bound both ends', run($SHOWS, array('from' => '2026-09-01', 'to' => '2026-10-31'))['total'], 3);
check('a range query drops the undated row', run($SHOWS, array('from' => '2000-01-01'))['total'], 4);

echo "\nDate validation — a bad date is an error, never a silently-ignored filter\n";
check('malformed from', run($SHOWS, array('from' => '10/02/2026')), array('error' => 'Invalid "from" date "10/02/2026" — expected YYYY-MM-DD.'));
check('impossible month', isset(run($SHOWS, array('from' => '2026-13-01'))['error']), true);
check('impossible day (Feb 30)', isset(run($SHOWS, array('to' => '2026-02-30'))['error']), true);
check('reversed range', run($SHOWS, array('from' => '2026-12-01', 'to' => '2026-01-01')), array('error' => 'Invalid date range — "from" is after "to".'));
check('empty date param is ignored, not an error', run($SHOWS, array('from' => ''))['total'], 5);

echo "\nRegion and free-text filters\n";
check('region is case-insensitive', run($SHOWS, array('region' => 'tx'))['total'], 1);
check('multiple regions', run($SHOWS, array('region' => 'TX,NY'))['total'], 2);
check('q matches a venue', run($SHOWS, array('q' => 'arcada'))['total'], 1);
check('q matches a city, case-insensitively', run($SHOWS, array('q' => 'NASHVILLE'))['total'], 1);
check('filters are ANDed', run($SHOWS, array('artist' => 'bohemian-queen', 'region' => 'TX'))['total'], 1);

echo "\nPaging — total is the match count, count is what came back\n";
$r = run($SHOWS, array('limit' => 2));
check('limit caps the rows returned', count($r['shows']), 2);
check('...but total still reports every match', $r['total'], 5);
$page1 = keys_of(run($SHOWS, array('limit' => 2, 'offset' => 0)));
$page2 = keys_of(run($SHOWS, array('limit' => 2, 'offset' => 2)));
$page3 = keys_of(run($SHOWS, array('limit' => 2, 'offset' => 4)));
check('pages do not overlap', array_merge($page1, $page2, $page3), keys_of(run($SHOWS, array())));
check('offset past the end returns empty', run($SHOWS, array('offset' => 99))['shows'], array());
check('limit is capped at the max', tour_calendar_parse_read_args(array('limit' => '999999'))['limit'], TOUR_CALENDAR_READ_LIMIT_MAX);
check('default limit applies when absent', tour_calendar_parse_read_args(array())['limit'], TOUR_CALENDAR_READ_LIMIT_DEFAULT);
check('limit 0 is rejected', isset(run($SHOWS, array('limit' => '0'))['error']), true);
check('negative offset is rejected', isset(run($SHOWS, array('offset' => '-1'))['error']), true);

echo "\nDegenerate input\n";
check('empty payload', run(array(), array()), array('total' => 0, 'shows' => array()));
check('empty payload with filters', run(array(), array('artist' => 'x', 'from' => '2026-01-01'))['total'], 0);
check('non-array rows are skipped', run(array('garbage', null, $SHOWS[0]), array())['total'], 1);
check('rows missing fields do not fatal', run(array(array('artist' => 'X')), array('q' => 'x'))['total'], 1);

echo "\nAuth — the one function between a third party's bot and the site\n";
$KEY = 'read-key-aaaaaaaaaaaaaaaaaaaa';
$SEC = 'ingest-secret-bbbbbbbbbbbb';
check('correct read key is accepted', tour_calendar_read_credential_ok($KEY, $SEC, $KEY, ''), true);
check('correct ingest secret is accepted (superset)', tour_calendar_read_credential_ok($KEY, $SEC, '', $SEC), true);
check('wrong read key is rejected', tour_calendar_read_credential_ok($KEY, $SEC, 'nope', ''), false);
check('no credential at all is rejected', tour_calendar_read_credential_ok($KEY, $SEC, '', ''), false);
check('read key sent in the secret slot is rejected', tour_calendar_read_credential_ok($KEY, $SEC, '', $KEY), false);
check('secret sent in the key slot is rejected', tour_calendar_read_credential_ok($KEY, $SEC, $SEC, ''), false);
check('a prefix of the key is rejected', tour_calendar_read_credential_ok($KEY, $SEC, substr($KEY, 0, -1), ''), false);
check('key plus trailing space is rejected', tour_calendar_read_credential_ok($KEY, $SEC, $KEY . ' ', ''), false);
// The empty-vs-empty trap: hash_equals('', '') is TRUE, so without the emptiness
// guards an unconfigured site would authenticate a request that sent nothing.
check('unconfigured read key + empty presented key is REJECTED', tour_calendar_read_credential_ok('', $SEC, '', ''), false);
check('nothing configured at all rejects everything', tour_calendar_read_credential_ok('', '', '', ''), false);
check('nothing configured still rejects a guess', tour_calendar_read_credential_ok('', '', 'guess', 'guess'), false);

echo "\nBearer parsing\n";
check('standard Bearer', tour_calendar_bearer_token('Bearer ' . $KEY), $KEY);
check('scheme is case-insensitive', tour_calendar_bearer_token('bearer ' . $KEY), $KEY);
check('surrounding whitespace trimmed', tour_calendar_bearer_token('Bearer   ' . $KEY . '  '), $KEY);
check('Basic auth is not a bearer token', tour_calendar_bearer_token('Basic ' . $KEY), '');
check('empty header', tour_calendar_bearer_token(''), '');
check('scheme with no token', tour_calendar_bearer_token('Bearer '), '');
check('a token that merely starts with "bearer" is not unwrapped', tour_calendar_bearer_token('bearerish'), '');

echo "\n" . ($failures ? "FAILED: $failures of $checks checks\n\n" : "PASSED: all $checks checks\n\n");
exit($failures ? 1 : 0);
