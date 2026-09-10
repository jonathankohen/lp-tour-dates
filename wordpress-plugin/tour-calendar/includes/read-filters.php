<?php

/**
 * Query parsing + filtering for the read-only /shows endpoint.
 *
 * Everything in this file is PURE: plain arrays in, plain arrays out, no
 * WordPress functions, no globals, no I/O. That is deliberate — it lets
 * tests/read-api-test.php exercise the filter logic with `php` alone, with no
 * WordPress bootstrap and no test framework.
 *
 * Keep it that way: if you need a WP function here, the logic belongs in the
 * REST handler instead.
 */

if (! defined('ABSPATH')) {
	// Allow direct include from the test harness, but never from a web request.
	if (PHP_SAPI !== 'cli') {
		exit;
	}
}

/**
 * Decide whether a presented credential may read the shows feed.
 *
 * Split out from the REST permission callback so it can be tested directly —
 * this is the one function standing between a third party's bot and the site.
 *
 * Accepts the read key, or the ingest secret (a strict superset, so the team's
 * own tooling needs no second credential). Every comparison is constant-time,
 * and BOTH candidates are always evaluated so a rejection costs the same either
 * way. An empty configured credential can never be matched by an empty presented
 * one — hash_equals('', '') is true, which without the emptiness guards would
 * let a site that has no read key configured accept a request that sent none.
 *
 * @param string $read_key        Configured read key ('' if unset).
 * @param string $secret          Configured ingest secret ('' if unset).
 * @param string $provided_key    Value presented as the read key.
 * @param string $provided_secret Value presented as the ingest secret.
 * @return bool
 */
function tour_calendar_read_credential_ok($read_key, $secret, $provided_key, $provided_secret)
{
	$by_key    = ('' !== $read_key && '' !== $provided_key && hash_equals($read_key, $provided_key));
	$by_secret = ('' !== $secret && '' !== $provided_secret && hash_equals($secret, $provided_secret));
	return $by_key || $by_secret;
}

/**
 * Pull a bearer token out of an Authorization header.
 *
 * Returns '' when the header is absent or is not a Bearer credential, so a
 * "Basic …" header can never be mistaken for a key.
 */
function tour_calendar_bearer_token($authorization)
{
	$authorization = (string) $authorization;
	if (0 !== stripos($authorization, 'bearer ')) {
		return '';
	}
	return trim(substr($authorization, 7));
}

/**
 * Normalize a name to lowercase alphanumerics for comparison.
 *
 * Collapses the ways a caller might spell an act: "Bohemian Queen",
 * "bohemian queen" and "bohemian-queen" all become "bohemianqueen".
 */
function tour_calendar_read_norm($value)
{
	return preg_replace('/[^a-z0-9]/', '', strtolower((string) $value));
}

/**
 * Strict YYYY-MM-DD check, including that the date actually exists.
 * Rejects "2026-13-01" and "2026-02-30", which a bare regex would pass.
 */
function tour_calendar_is_ymd($value)
{
	$value = (string) $value;
	if (1 !== preg_match('/^(\d{4})-(\d{2})-(\d{2})$/', $value, $m)) {
		return false;
	}
	return checkdate((int) $m[2], (int) $m[3], (int) $m[1]);
}

/**
 * Turn raw query parameters into a normalized argument set.
 *
 * Returns array('error' => 'message') on a malformed value. A bad date is an
 * error rather than a silently-ignored filter: a bot that asks for one month
 * and is handed the whole roster would not notice, and would act on it.
 *
 * @param array $query Raw query params (strings, as they arrive over HTTP).
 * @return array Parsed args, or array('error' => string).
 */
function tour_calendar_parse_read_args(array $query)
{
	$args = array(
		'artists' => array(),
		'regions' => array(),
		'from'    => '',
		'to'      => '',
		'q'       => '',
		'limit'   => TOUR_CALENDAR_READ_LIMIT_DEFAULT,
		'offset'  => 0,
	);

	if (isset($query['artist']) && '' !== trim((string) $query['artist'])) {
		foreach (explode(',', (string) $query['artist']) as $name) {
			$norm = tour_calendar_read_norm($name);
			if ('' !== $norm) {
				$args['artists'][] = $norm;
			}
		}
	}

	if (isset($query['region']) && '' !== trim((string) $query['region'])) {
		foreach (explode(',', (string) $query['region']) as $region) {
			$region = strtoupper(trim($region));
			if ('' !== $region) {
				$args['regions'][] = $region;
			}
		}
	}

	foreach (array('from', 'to') as $bound) {
		if (isset($query[$bound]) && '' !== trim((string) $query[$bound])) {
			$value = trim((string) $query[$bound]);
			if (! tour_calendar_is_ymd($value)) {
				return array('error' => sprintf('Invalid "%s" date "%s" — expected YYYY-MM-DD.', $bound, $value));
			}
			$args[$bound] = $value;
		}
	}

	if ('' !== $args['from'] && '' !== $args['to'] && $args['from'] > $args['to']) {
		return array('error' => 'Invalid date range — "from" is after "to".');
	}

	if (isset($query['q'])) {
		$args['q'] = strtolower(trim((string) $query['q']));
	}

	if (isset($query['limit']) && '' !== trim((string) $query['limit'])) {
		$limit = (int) $query['limit'];
		if ($limit < 1) {
			return array('error' => '"limit" must be a positive integer.');
		}
		$args['limit'] = min($limit, TOUR_CALENDAR_READ_LIMIT_MAX);
	}

	if (isset($query['offset']) && '' !== trim((string) $query['offset'])) {
		$offset = (int) $query['offset'];
		if ($offset < 0) {
			return array('error' => '"offset" must be zero or greater.');
		}
		$args['offset'] = $offset;
	}

	return $args;
}

/**
 * Apply the parsed filters to the stored show rows.
 *
 * Filters are ANDed. Results are sorted by date, then artist, then venue —
 * a stable order matters because ?offset= paging over an unsorted list can
 * repeat or skip rows between calls.
 *
 * @param array $shows Rows as stored by /ingest.
 * @param array $args  Output of tour_calendar_parse_read_args().
 * @return array array('total' => int, 'shows' => array) — total counts matches
 *               BEFORE limit/offset, so a caller can tell there is more.
 */
function tour_calendar_filter_shows(array $shows, array $args)
{
	$matched = array();

	foreach ($shows as $show) {
		if (! is_array($show)) {
			continue;
		}
		$date = isset($show['date']) ? (string) $show['date'] : '';

		if ($args['artists']) {
			$artist = tour_calendar_read_norm(isset($show['artist']) ? $show['artist'] : '');
			if (! in_array($artist, $args['artists'], true)) {
				continue;
			}
		}

		if ($args['regions']) {
			$region = strtoupper(trim(isset($show['region']) ? (string) $show['region'] : ''));
			if (! in_array($region, $args['regions'], true)) {
				continue;
			}
		}

		if ('' !== $args['from'] || '' !== $args['to']) {
			// An undated or malformed row cannot be placed in a range, so a range
			// query excludes it rather than guessing.
			if (! tour_calendar_is_ymd($date)) {
				continue;
			}
			if ('' !== $args['from'] && $date < $args['from']) {
				continue;
			}
			if ('' !== $args['to'] && $date > $args['to']) {
				continue;
			}
		}

		if ('' !== $args['q']) {
			$haystack = strtolower(
				(isset($show['artist']) ? $show['artist'] : '') . ' ' .
					(isset($show['venue']) ? $show['venue'] : '') . ' ' .
					(isset($show['city']) ? $show['city'] : '')
			);
			if (false === strpos($haystack, $args['q'])) {
				continue;
			}
		}

		$matched[] = $show;
	}

	usort(
		$matched,
		function ($a, $b) {
			$cmp = strcmp(isset($a['date']) ? (string) $a['date'] : '', isset($b['date']) ? (string) $b['date'] : '');
			if (0 !== $cmp) {
				return $cmp;
			}
			$cmp = strcmp(isset($a['artist']) ? (string) $a['artist'] : '', isset($b['artist']) ? (string) $b['artist'] : '');
			if (0 !== $cmp) {
				return $cmp;
			}
			return strcmp(isset($a['venue']) ? (string) $a['venue'] : '', isset($b['venue']) ? (string) $b['venue'] : '');
		}
	);

	return array(
		'total' => count($matched),
		'shows' => array_values(array_slice($matched, $args['offset'], $args['limit'])),
	);
}
