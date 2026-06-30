<?php

/**
 * Plugin Name:       Tour Calendar
 * Description:        Sortable calendar + list of aggregated tour dates with one-click copy-paste outreach formats. Data is pushed weekly by the love-automations Python job.
 * Version:           1.3.3
 * Author:            Love Productions
 * License:           GPL-2.0-or-later
 * Requires at least: 5.8
 * Requires PHP:      7.4
 *
 * Integration overview
 * --------------------
 * 1. The Python job (outputs/website.py::write_website) POSTs
 *        { "generated_at": "...", "shows": [ ... ] }
 *    to  /wp-json/tour-dates/v1/ingest  with header  X-Tour-Secret: <secret>.
 * 2. This plugin validates the secret, stores the payload in a wp_option.
 * 3. The [tour-calendar] shortcode prints that payload inline as JSON and loads
 *    the self-contained front-end (assets/app.js) which renders everything in
 *    the browser. No public read endpoint exists — access control is whatever
 *    visibility you set on the page that hosts the shortcode.
 */

if (! defined('ABSPATH')) {
	exit; // No direct access.
}

define('TOUR_CALENDAR_VERSION', '1.3.3');
define('TOUR_CALENDAR_OPTION_PAYLOAD', 'tour_dates_payload');
define('TOUR_CALENDAR_OPTION_GENERATED', 'tour_dates_generated_at');
define('TOUR_CALENDAR_OPTION_SECRET', 'tour_dates_secret');

/**
 * Resolve the shared ingest secret.
 *
 * Prefer a constant in wp-config.php (define('TOUR_DATES_SECRET', '...'); —
 * not stored in the database, the most secure option). Otherwise fall back to
 * an auto-generated option created on activation.
 */
function tour_calendar_get_secret()
{
	if (defined('TOUR_DATES_SECRET') && TOUR_DATES_SECRET) {
		return (string) TOUR_DATES_SECRET;
	}
	return (string) get_option(TOUR_CALENDAR_OPTION_SECRET, '');
}

/**
 * Generate a secret on activation if one is not already configured.
 */
function tour_calendar_activate()
{
	if (! defined('TOUR_DATES_SECRET') && ! get_option(TOUR_CALENDAR_OPTION_SECRET)) {
		add_option(TOUR_CALENDAR_OPTION_SECRET, wp_generate_password(48, false, false));
	}
}
register_activation_hook(__FILE__, 'tour_calendar_activate');

/* -------------------------------------------------------------------------- *
 *  REST: ingest endpoint
 * -------------------------------------------------------------------------- */

add_action('rest_api_init', function () {
	register_rest_route(
		'tour-dates/v1',
		'/ingest',
		array(
			'methods'             => 'POST',
			'callback'            => 'tour_calendar_rest_ingest',
			'permission_callback' => 'tour_calendar_rest_ingest_permission',
		)
	);
	// Creates VS Event List `event` posts for shows not already on the site.
	register_rest_route(
		'tour-dates/v1',
		'/publish-events',
		array(
			'methods'             => 'POST',
			'callback'            => 'tour_calendar_rest_publish_events',
			'permission_callback' => 'tour_calendar_rest_ingest_permission',
		)
	);
	// Reports (and, when asked, trashes) duplicate events — same act + same date.
	register_rest_route(
		'tour-dates/v1',
		'/cleanup-duplicates',
		array(
			'methods'             => 'POST',
			'callback'            => 'tour_calendar_rest_cleanup_duplicates',
			'permission_callback' => 'tour_calendar_rest_ingest_permission',
		)
	);
	// Rewrites the body (bio) of existing events for one or more acts.
	register_rest_route(
		'tour-dates/v1',
		'/update-descriptions',
		array(
			'methods'             => 'POST',
			'callback'            => 'tour_calendar_rest_update_descriptions',
			'permission_callback' => 'tour_calendar_rest_ingest_permission',
		)
	);
	// Updates the ticket link (event-link meta + "Venue Website" button) on existing
	// events, matched per show by act + date. Covers drafts as well as published.
	register_rest_route(
		'tour-dates/v1',
		'/update-links',
		array(
			'methods'             => 'POST',
			'callback'            => 'tour_calendar_rest_update_links',
			'permission_callback' => 'tour_calendar_rest_ingest_permission',
		)
	);
	// Read-only listing of existing events (title, date, status, link) for the audit.
	register_rest_route(
		'tour-dates/v1',
		'/list-events',
		array(
			'methods'             => 'POST',
			'callback'            => 'tour_calendar_rest_list_events',
			'permission_callback' => 'tour_calendar_rest_ingest_permission',
		)
	);
	// Trash specific `event` posts by ID (surgical cleanup the title/date-keyed tools can't target).
	register_rest_route(
		'tour-dates/v1',
		'/trash-events',
		array(
			'methods'             => 'POST',
			'callback'            => 'tour_calendar_rest_trash_events',
			'permission_callback' => 'tour_calendar_rest_ingest_permission',
		)
	);
});

/**
 * Constant-time secret check via the X-Tour-Secret header.
 */
function tour_calendar_rest_ingest_permission(WP_REST_Request $request)
{
	$expected = tour_calendar_get_secret();
	$provided = (string) $request->get_header('x-tour-secret');

	if ('' === $expected) {
		return new WP_Error('tour_calendar_no_secret', 'Ingest secret is not configured on the server.', array('status' => 500));
	}
	if (! hash_equals($expected, $provided)) {
		return new WP_Error('tour_calendar_forbidden', 'Invalid or missing X-Tour-Secret header.', array('status' => 403));
	}
	return true;
}

/**
 * Validate and store the incoming payload.
 *
 * Accepts { "generated_at"?: string, "shows": [ {artist,date,venue,city,
 * region,country,ticket_url,source,raw_id,start_time,title}, ... ] }. Unknown
 * keys on each show are dropped; required keys are coerced to strings.
 */
function tour_calendar_rest_ingest(WP_REST_Request $request)
{
	$body = $request->get_json_params();

	if (! is_array($body) || ! isset($body['shows']) || ! is_array($body['shows'])) {
		return new WP_REST_Response(array('error' => 'Body must be an object with a "shows" array.'), 400);
	}

	$fields = array('artist', 'date', 'venue', 'city', 'region', 'country', 'ticket_url', 'source', 'raw_id', 'start_time', 'title');
	$clean  = array();

	foreach ($body['shows'] as $show) {
		if (! is_array($show)) {
			continue;
		}
		$row = array();
		foreach ($fields as $f) {
			$row[$f] = isset($show[$f]) ? (string) $show[$f] : '';
		}
		// A show is only meaningful with at least an artist and a date.
		if ('' === $row['artist'] || '' === $row['date']) {
			continue;
		}
		$clean[] = $row;
	}

	$generated_at = isset($body['generated_at']) ? sanitize_text_field((string) $body['generated_at']) : gmdate('c');

	$payload = wp_json_encode(
		array(
			'generated_at' => $generated_at,
			'shows'        => $clean,
		)
	);

	update_option(TOUR_CALENDAR_OPTION_PAYLOAD, $payload, false);
	update_option(TOUR_CALENDAR_OPTION_GENERATED, $generated_at, false);

	return new WP_REST_Response(
		array(
			'ok'           => true,
			'stored'       => count($clean),
			'generated_at' => $generated_at,
		),
		200
	);
}

/* -------------------------------------------------------------------------- *
 *  REST: publish-events endpoint (VS Event List)
 * -------------------------------------------------------------------------- */

/**
 * Create VS Event List `event` posts (as drafts) for shows not already on the site.
 *
 * Body: {
 *   "dry_run":      bool,            // when true, plan only — write nothing
 *   "default_time": "20:00",         // 24h string written to the event-time meta
 *   "limit":        int,             // cap on events created/planned (0 = unlimited)
 *   "shows":        [ {artist,date,venue,city,region,country,ticket_url,...} ],
 *   "assets":       { "<artist>": { "image_b64", "image_filename", "description" } },
 *   "categories":   { "<artist>": [ "Tributes", "Concerts" ] }  // existing event_cat names
 * }
 *
 * New events are created as DRAFTS for staff review. A show is skipped when an
 * existing event of the same act lands on the same calendar day (dedup), or when
 * we can't give it BOTH a body and an image (content gate). Body/image come from
 * the act's Drive asset when present, else an existing event of the same act.
 *
 * VS Event List meta keys (verify against one live event before bulk use):
 *   event-date (Unix timestamp), event-time (string), event-location, event-link.
 */
function tour_calendar_rest_publish_events(WP_REST_Request $request)
{
	$body = $request->get_json_params();

	if (! is_array($body) || ! isset($body['shows']) || ! is_array($body['shows'])) {
		return new WP_REST_Response(array('error' => 'Body must be an object with a "shows" array.'), 400);
	}

	// Image sideloading + EWWW optimization can outlast the default 30s execution
	// cap. Lift it where the host allows (no-op when disabled); the client also
	// chunks requests so this is belt-and-suspenders.
	if (function_exists('set_time_limit')) {
		@set_time_limit(0);
	}

	$dry_run      = ! empty($body['dry_run']);
	$default_time = isset($body['default_time']) ? sanitize_text_field((string) $body['default_time']) : '';
	$limit        = isset($body['limit']) ? max(0, (int) $body['limit']) : 0;
	$assets       = (isset($body['assets']) && is_array($body['assets'])) ? $body['assets'] : array();
	// New events are drafts for staff review by default; the residency migration creates
	// them published so the public calendar swaps with no gap.
	$post_status  = (isset($body['publish_status']) && 'publish' === $body['publish_status']) ? 'publish' : 'draft';
	// When set, after creating each residency (range) event, trash the act's individual
	// single events that fall inside that month's range at the same venue — the one-time
	// migration from one-event-per-show to one-event-per-month.
	$replace_residencies = ! empty($body['replace_residency_singles']);
	// Per-act `event_cat` term names, keyed by artist. Resolved to existing terms
	// at assign time (see tour_calendar_assign_event_cats) — unknown names dropped.
	$categories   = (isset($body['categories']) && is_array($body['categories'])) ? $body['categories'] : array();

	// Two indexes built from existing events, kept separate so dry-run planning can
	// claim a day without creating a fake template:
	//   $by_title — normalized title => events, used only to pick an image/body template.
	//   $seen_day — "<normkey>|Y-m-d" => true, used only for act+date dedup.
	$by_title  = array();
	$seen_day  = array();
	$events_by_key      = array(); // key => list of {id, day, start_day, is_range, location} — for residency update/trash.
	$residency_by_start = array(); // "key|Y-m-d(start)" => event id, for existing range events (idempotent update).
	$event_ids = get_posts(
		array(
			'post_type'   => 'event',
			'post_status' => array('publish', 'future', 'draft', 'pending'),
			'numberposts' => -1,
			'fields'      => 'ids',
		)
	);
	foreach ($event_ids as $eid) {
		// Use the RAW stored title, not get_the_title(): the_title runs wptexturize,
		// which turns " & " into " &#038; " — the normalizer would then keep the stray
		// digits "038" and the dedup key would never match a show built from the raw
		// artist name. That mismatch silently duplicated every "&" act on each run.
		$title = get_post_field('post_title', $eid);
		$key   = tour_calendar_norm_title($title);
		$ed    = (int) get_post_meta($eid, 'event-date', true);
		// VS Event List stores a date RANGE as event-start-date (start) + event-date (end).
		$esd   = (int) get_post_meta($eid, 'event-start-date', true);
		// "Protected" = one of OUR monthly residency events: identify it by the explicit
		// flag we stamp on it (event-tour-residency), NOT by the presence of event-start-date
		// — a plain single event can carry a stray start-date (set in the WP admin) and must
		// still be replaceable. Genuine multi-day ranges (start != end) are also protected, so
		// the first post-migration run updates them in place rather than duplicating.
		$is_res_event = ('1' === (string) get_post_meta($eid, 'event-tour-residency', true));
		$is_multiday  = ($esd && gmdate('Y-m-d', $esd) !== gmdate('Y-m-d', $ed));
		$protected    = $is_res_event || $is_multiday;
		$start_ts     = $esd ? $esd : $ed;
		if (! isset($by_title[$key])) {
			$by_title[$key] = array('title' => $title, 'events' => array());
		}
		$by_title[$key]['events'][] = array('id' => (int) $eid, 'date' => $start_ts);
		if ($ed) {
			$seen_day[$key . '|' . gmdate('Y-m-d', $ed)] = true;
		}
		// A residency event lives under its START day — which is what an incoming residency show
		// is keyed by — so register it for an idempotent in-place update on re-run.
		if ($protected && $start_ts) {
			$seen_day[$key . '|' . gmdate('Y-m-d', $start_ts)] = true;
			$residency_by_start[$key . '|' . gmdate('Y-m-d', $start_ts)] = (int) $eid;
		}
		$events_by_key[$key][] = array(
			'id'        => (int) $eid,
			'day'       => $ed ? gmdate('Y-m-d', $ed) : '',
			'protected' => $protected,
			'location'  => (string) get_post_meta($eid, 'event-location', true),
		);
	}

	$created         = array();
	$skipped         = array();
	$would_create    = array();
	$errors          = array();
	$residency_plans = array(); // residency events made this request: {key, location, start_day, end_day, id} — drives the single-event trash pass.
	$made            = 0; // events created (real) or planned (dry) — what $limit caps.
	$drive_thumb     = array(); // act key => sideloaded attachment id, reused within this request.

	foreach ($body['shows'] as $show) {
		if (! is_array($show)) {
			continue;
		}
		$artist = isset($show['artist']) ? (string) $show['artist'] : '';
		$date   = isset($show['date']) ? (string) $show['date'] : '';
		if ('' === $artist || '' === $date) {
			continue;
		}

		$ts            = strtotime($date); // local midnight, matching a manual date-picker entry
		$key           = tour_calendar_norm_title($artist);
		$daykey        = $key . '|' . gmdate('Y-m-d', $ts);
		$match         = isset($by_title[$key]) ? $by_title[$key] : null;
		$matched_title = $match ? $match['title'] : '';
		$cats          = (isset($categories[$artist]) && is_array($categories[$artist])) ? $categories[$artist] : array();

		// Residency: one date-range event for a month of shows at the same venue. `date` is
		// the range start, `end_date` the end; per-date times are listed in the body, so the
		// single event-time is left blank.
		$is_residency    = ! empty($show['is_residency']);
		$end_date        = isset($show['end_date']) ? (string) $show['end_date'] : '';
		$residency_dates = ($is_residency && isset($show['residency_dates']) && is_array($show['residency_dates'])) ? $show['residency_dates'] : array();

		// Dedup / idempotency. A residency event is keyed by its START day: it UPDATES the
		// existing range event there (if any) instead of duplicating, and — unlike a normal
		// show — is NOT blocked by an individual single-day event sitting on that day (those
		// singles are exactly what the residency replaces). A normal show still skips when its
		// day is already taken, reconciling categories on the event that owns it.
		$update_id = 0;
		if ($is_residency) {
			$update_id = isset($residency_by_start[$daykey]) ? (int) $residency_by_start[$daykey] : 0;
		} elseif (isset($seen_day[$daykey])) {
			$categorized = array();
			if (! $dry_run && $match && ! empty($cats)) {
				foreach ($match['events'] as $ev) {
					if ((int) $ev['date'] && gmdate('Y-m-d', (int) $ev['date']) === gmdate('Y-m-d', $ts)) {
						$categorized = tour_calendar_reconcile_event_cats((int) $ev['id'], $cats);
						break;
					}
				}
			}
			$skipped[] = array(
				'artist'        => $artist,
				'date'          => $date,
				'reason'        => 'exists',
				'matched_title' => $matched_title,
				'categorized'   => $categorized,
			);
			continue;
		}

		$location     = tour_calendar_join_location($show);
		$link         = isset($show['ticket_url']) ? esc_url_raw((string) $show['ticket_url']) : '';
		// Distinctive tokens of the residency VENUE (not its full location) — used to match the
		// old single events to trash even when their stored location string differs (full address
		// vs "Venue, City, ST"). Venue-only so a shared city word can't cause a cross-venue match.
		$res_venue_tokens = $is_residency ? tour_calendar_venue_tokens(isset($show['venue']) ? (string) $show['venue'] : '') : array();
		// Per-show start time wins; fall back to the batch default (blank unless set).
		$show_time    = isset($show['start_time']) ? sanitize_text_field((string) $show['start_time']) : '';
		$time         = $is_residency ? '' : ('' !== $show_time ? $show_time : $default_time);
		// An explicit per-show title wins; otherwise reuse a matched event's title
		// (so re-runs don't fork "&" acts) and fall back to the bare artist name.
		$show_title   = isset($show['title']) ? sanitize_text_field((string) $show['title']) : '';
		$title_to_use = '' !== $show_title ? $show_title : ($matched_title ? $matched_title : $artist);
		$template_id  = ($match && ! empty($match['events'])) ? (int) $match['events'][0]['id'] : 0;
		$has_drive    = isset($assets[$artist]) && is_array($assets[$artist]);

		// Resolve the real body text, preferring the act's Drive description and
		// falling back to a template event's content.
		$content     = '';
		$body_source = 'none';
		if ($has_drive && ! empty($assets[$artist]['description'])) {
			$content     = tour_calendar_text_to_blocks((string) $assets[$artist]['description']);
			$body_source = 'drive';
		}
		if ('' === $content && $template_id) {
			$tpost = get_post($template_id);
			if ($tpost && '' !== trim((string) $tpost->post_content)) {
				$content     = $tpost->post_content;
				$body_source = 'existing-event';
			}
		}

		// Resolve the image source, preferring the act's Drive image and falling
		// back to the template event's thumbnail.
		$image_source = 'none';
		if ($has_drive && ! empty($assets[$artist]['image_b64'])) {
			$image_source = 'drive';
		} elseif ($template_id && has_post_thumbnail($template_id)) {
			$image_source = 'existing-event';
		}

		// Content gate: require BOTH a body and an image. Do NOT claim the day, so a
		// later same-day show that does have content can still be created.
		if ('' === $content || 'none' === $image_source) {
			$skipped[] = array(
				'artist'       => $artist,
				'date'         => $date,
				'reason'       => 'no_content',
				'body_source'  => $body_source,
				'image_source' => $image_source,
			);
			continue;
		}

		// Limit: stop once N events have been made (created or planned).
		if ($limit > 0 && $made >= $limit) {
			break;
		}

		// For a residency, list every show date (with its time) above the bio — the single
		// event-time is blank, so this is where the per-date schedule lives.
		if ($is_residency && ! empty($residency_dates)) {
			$dates_block = tour_calendar_residency_dates_block($residency_dates);
			if ('' !== $dates_block) {
				$content = $dates_block . $content;
			}
		}

		// Point the red "Venue Website" button at this show's ticket link. A body
		// copied from a template carries the template's stale link; a Drive-sourced
		// body has no button at all — both are normalized here.
		$content = tour_calendar_apply_ticket_button($content, $link);

		$plan = array(
			'artist'       => $artist,
			'title'        => $title_to_use,
			'date'         => $date,
			'end_date'     => $is_residency ? $end_date : '',
			'is_residency' => $is_residency,
			'action'       => $update_id ? 'update' : 'create',
			'time'         => $time,
			'location'     => $location,
			'link'         => $link,
			'categories'   => array_values($cats),
			'body_source'  => $body_source,
			'image_source' => $image_source,
		);
		// Range end timestamp (residency only); single events stay on their one day.
		$end_ts = ($is_residency && '' !== $end_date) ? strtotime($end_date) : 0;

		if ($dry_run) {
			$would_create[]    = $plan;
			$seen_day[$daykey] = true;
			if ($is_residency) {
				$residency_plans[] = array(
					'key'          => $key,
					'location'     => $location,
					'venue_tokens' => $res_venue_tokens,
					'start_day'    => gmdate('Y-m-d', $ts),
					'end_day'      => $end_ts ? gmdate('Y-m-d', $end_ts) : gmdate('Y-m-d', $ts),
					'id'           => $update_id,
				);
			}
			$made++;
			continue;
		}

		// Create a fresh draft, or update the existing range event in place (idempotent re-run).
		if ($update_id) {
			$res    = wp_update_post(array('ID' => $update_id, 'post_title' => $title_to_use, 'post_content' => $content, 'post_status' => $post_status), true);
			$new_id = is_wp_error($res) ? $res : $update_id;
		} else {
			$new_id = wp_insert_post(
				array(
					'post_type'    => 'event',
					'post_status'  => $post_status,
					'post_title'   => $title_to_use,
					'post_content' => $content,
				),
				true
			);
		}
		if (is_wp_error($new_id)) {
			$errors[] = array(
				'artist' => $artist,
				'date'   => $date,
				'error'  => $new_id->get_error_message(),
			);
			continue;
		}

		// A residency event spans a range: event-start-date = start ($ts), event-date = end
		// ($end_ts, computed above). VS Event List treats event-date as the END of a multi-day
		// event, and a missing/equal start-date as a single day — so normal events keep writing
		// only event-date.
		if ($end_ts) {
			update_post_meta($new_id, 'event-start-date', $ts);
			update_post_meta($new_id, 'event-date', $end_ts);
		} else {
			update_post_meta($new_id, 'event-date', $ts);
		}
		// Stamp our residency events so re-runs match them for idempotent update and never
		// trash them — independent of the start/end-date meta (see the index pass above).
		if ($is_residency) {
			update_post_meta($new_id, 'event-tour-residency', '1');
		}
		if ('' !== $time) {
			update_post_meta($new_id, 'event-time', $time);
		}
		update_post_meta($new_id, 'event-location', $location);
		update_post_meta($new_id, 'event-link', $link);

		// Categorize: assign the act's existing event_cat terms (unknown names dropped).
		$applied_cats = tour_calendar_assign_event_cats($new_id, $cats);

		// Featured image: reuse the template event's, else sideload the Drive image.
		if ('existing-event' === $image_source) {
			$tid = get_post_thumbnail_id($template_id);
			if ($tid) {
				set_post_thumbnail($new_id, $tid);
			}
		} elseif ('drive' === $image_source) {
			// Sideload an act's Drive image at most once: reuse the attachment for
			// later shows of the same act (cached this request, or pulled from an
			// event of the act created in an earlier chunk/run). Avoids the timeout
			// and stops the media library filling with duplicate copies.
			$reuse_id = 0;
			if (isset($drive_thumb[$key])) {
				$reuse_id = $drive_thumb[$key];
			} elseif ($template_id) {
				$reuse_id = (int) get_post_thumbnail_id($template_id);
			}
			if ($reuse_id) {
				set_post_thumbnail($new_id, $reuse_id);
				$drive_thumb[$key] = $reuse_id;
			} else {
				$fname  = ! empty($assets[$artist]['image_filename']) ? (string) $assets[$artist]['image_filename'] : ($artist . '.jpg');
				$att_id = tour_calendar_sideload_b64((string) $assets[$artist]['image_b64'], $fname, $new_id);
				if ($att_id && ! is_wp_error($att_id)) {
					set_post_thumbnail($new_id, $att_id);
					$drive_thumb[$key] = (int) $att_id;
				}
			}
		}

		// Register so later shows of the same act dedup against it and can template off it.
		if (! isset($by_title[$key])) {
			$by_title[$key] = array('title' => $title_to_use, 'events' => array());
		}
		$by_title[$key]['events'][] = array('id' => (int) $new_id, 'date' => $ts);
		$seen_day[$daykey]          = true;
		if ($is_residency) {
			$residency_plans[] = array(
				'key'          => $key,
				'location'     => $location,
				'venue_tokens' => $res_venue_tokens,
				'start_day'    => gmdate('Y-m-d', $ts),
				'end_day'      => $end_ts ? gmdate('Y-m-d', $end_ts) : gmdate('Y-m-d', $ts),
				'id'           => (int) $new_id,
			);
		}
		$made++;

		$created[] = array(
			'id'         => (int) $new_id,
			'artist'     => $artist,
			'date'       => $date,
			'action'     => $update_id ? 'updated' : 'created',
			'categories' => $applied_cats,
		);
	}

	// Residency migration: trash the act's individual single events that fall inside a
	// freshly-made range (same act + same venue), so one-event-per-month replaces
	// one-event-per-show. Only runs when the caller opts in. Range events are never
	// trashed; a single is matched by act key, its day landing within [start_day, end_day],
	// and the residency venue sharing a distinctive token with the single's stored location
	// (so a full-address single still matches a "Venue, City, ST" residency). Exact-location
	// equality is the fallback when the residency venue yields no distinctive token.
	$trashed     = array();
	$would_trash = array();
	if ($replace_residencies && ! empty($residency_plans)) {
		$handled = array(); // event ids already trashed/planned, so overlapping plans don't double-count.
		foreach ($residency_plans as $rp) {
			$normloc     = tour_calendar_norm_loc($rp['location']);
			$res_tokens  = isset($rp['venue_tokens']) && is_array($rp['venue_tokens']) ? $rp['venue_tokens'] : array();
			$candidates  = isset($events_by_key[$rp['key']]) ? $events_by_key[$rp['key']] : array();
			foreach ($candidates as $cand) {
				if ($cand['protected'] || '' === $cand['day']) {
					continue; // never trash one of our residency events, or one with no date
				}
				if ((int) $cand['id'] === (int) $rp['id'] || isset($handled[(int) $cand['id']])) {
					continue;
				}
				if ($cand['day'] < $rp['start_day'] || $cand['day'] > $rp['end_day']) {
					continue; // outside this month's range
				}
				// Same venue? Prefer a distinctive-token overlap; fall back to exact location.
				if (! empty($res_tokens)) {
					$same_venue = ! empty(array_intersect($res_tokens, tour_calendar_venue_tokens($cand['location'])));
				} else {
					$same_venue = (tour_calendar_norm_loc($cand['location']) === $normloc);
				}
				if (! $same_venue) {
					continue; // different venue — leave it alone
				}
				$handled[(int) $cand['id']] = true;
				$row = array('id' => (int) $cand['id'], 'date' => $cand['day'], 'replaced_by' => (int) $rp['id']);
				if ($dry_run) {
					$would_trash[] = $row;
				} else {
					wp_trash_post((int) $cand['id']);
					$trashed[] = $row;
				}
			}
		}
	}

	return new WP_REST_Response(
		array(
			'ok'           => true,
			'dry_run'      => $dry_run,
			'created'      => $created,
			'skipped'      => $skipped,
			'would_create' => $would_create,
			'errors'       => $errors,
			'trashed'      => $trashed,
			'would_trash'  => $would_trash,
		),
		200
	);
}

/**
 * REST: find duplicate events (same normalized act title + same calendar day) and,
 * when dry_run is false, trash the surplus — keeping one "best" event per group.
 *
 * Body: {
 *   "dry_run":      bool,   // default TRUE — report only, change nothing
 *   "force_delete": bool    // default false — when actually cleaning, permanently
 *                           // delete instead of moving to Trash (recoverable)
 * }
 *
 * Grouping mirrors the publish dedup exactly: tour_calendar_norm_title(raw title) +
 * gmdate('Y-m-d', event-date). Events with no event-date can't be dated, so they're
 * reported as a count but never trashed. Keep-policy within a group (best first):
 *   1. status: publish > future > pending > draft   (keep something live)
 *   2. has BOTH a featured image and body content    (most complete)
 *   3. more event_cat terms                          (preserve categorization)
 *   4. oldest (smallest post ID)                     (the original)
 */
function tour_calendar_rest_cleanup_duplicates(WP_REST_Request $request)
{
	$body         = $request->get_json_params();
	$dry_run      = is_array($body) ? ! empty($body['dry_run']) : true;
	if (! is_array($body) || ! array_key_exists('dry_run', $body)) {
		$dry_run = true; // default safe
	}
	$force_delete = is_array($body) && ! empty($body['force_delete']);

	if (function_exists('set_time_limit')) {
		@set_time_limit(0);
	}

	$event_ids = get_posts(
		array(
			'post_type'   => 'event',
			'post_status' => array('publish', 'future', 'draft', 'pending'),
			'numberposts' => -1,
			'fields'      => 'ids',
		)
	);

	$groups       = array(); // "normkey|Y-m-d" => list of event meta
	$no_date      = 0;
	$status_rank  = array('publish' => 0, 'future' => 1, 'pending' => 2, 'draft' => 3);

	foreach ($event_ids as $eid) {
		$eid   = (int) $eid;
		$title = get_post_field('post_title', $eid);
		$ed    = (int) get_post_meta($eid, 'event-date', true);
		if (! $ed) {
			$no_date++;
			continue;
		}
		$key  = tour_calendar_norm_title($title) . '|' . gmdate('Y-m-d', $ed);
		$post = get_post($eid);
		$cats = wp_get_object_terms($eid, 'event_cat', array('fields' => 'names'));
		if (is_wp_error($cats)) {
			$cats = array();
		}
		$groups[$key][] = array(
			'id'         => $eid,
			'title'      => $title,
			'status'     => $post ? $post->post_status : '',
			'date'       => gmdate('Y-m-d', $ed),
			'has_image'  => has_post_thumbnail($eid),
			'has_body'   => $post && '' !== trim((string) $post->post_content),
			'categories' => array_values($cats),
			'_rank'      => isset($status_rank[$post ? $post->post_status : '']) ? $status_rank[$post->post_status] : 9,
		);
	}

	$report   = array();
	$trashed  = array();
	$dup_events = 0;

	foreach ($groups as $key => $events) {
		if (count($events) < 2) {
			continue;
		}
		// Sort best-keep first by the policy above.
		usort(
			$events,
			function ($a, $b) {
				if ($a['_rank'] !== $b['_rank']) {
					return $a['_rank'] <=> $b['_rank'];
				}
				$ca = ($a['has_image'] && $a['has_body']) ? 0 : 1;
				$cb = ($b['has_image'] && $b['has_body']) ? 0 : 1;
				if ($ca !== $cb) {
					return $ca <=> $cb;
				}
				if (count($a['categories']) !== count($b['categories'])) {
					return count($b['categories']) <=> count($a['categories']);
				}
				return $a['id'] <=> $b['id'];
			}
		);
		$keep  = array_shift($events);
		$trash = $events;
		$dup_events += count($trash);

		foreach ($trash as $t) {
			unset($t['_rank']);
			if (! $dry_run) {
				$ok = $force_delete ? (bool) wp_delete_post($t['id'], true) : (bool) wp_trash_post($t['id']);
				if ($ok) {
					$trashed[] = $t['id'];
				}
			}
		}
		unset($keep['_rank']);
		$report[] = array(
			'date'  => $keep['date'],
			'keep'  => $keep,
			'trash' => array_map(
				function ($t) {
					unset($t['_rank']);
					return $t;
				},
				$trash
			),
		);
	}

	return new WP_REST_Response(
		array(
			'ok'               => true,
			'dry_run'          => $dry_run,
			'force_delete'     => $force_delete,
			'scanned'          => count($event_ids),
			'no_event_date'    => $no_date,
			'duplicate_groups' => count($report),
			'duplicate_events' => $dup_events,
			'trashed'          => $trashed,
			'groups'           => $report,
		),
		200
	);
}

/* -------------------------------------------------------------------------- *
 *  REST: update-descriptions endpoint
 * -------------------------------------------------------------------------- */

/**
 * Rewrite the body (bio) of EXISTING events for one or more acts, from a fresh
 * plain-text description. Used to refresh a bio after the act's source document
 * changes, without recreating events.
 *
 * Body: {
 *   "dry_run":      bool,                       // when true, plan only — write nothing
 *   "descriptions": { "<artist>": "<plain text bio>" },
 *   "statuses":     [ "publish", "draft", ... ] // optional; default publish+draft+future+pending
 * }
 *
 * Events are matched to an act exactly like the publish dedup: by normalized
 * post title (tour_calendar_norm_title) == normalized artist name. The new body
 * is built once per act with tour_calendar_text_to_blocks(); each event's own
 * "Venue Website" button (from its event-link meta) is re-applied so per-event
 * ticket links are preserved. Only post_content is touched — featured image,
 * event meta (date/time/location/link), categories, and status are left as-is.
 * An act whose description is blank (or produces no paragraphs) is skipped, so a
 * bad/empty file can never wipe a bio.
 */
function tour_calendar_rest_update_descriptions(WP_REST_Request $request)
{
	$body = $request->get_json_params();

	if (! is_array($body) || ! isset($body['descriptions']) || ! is_array($body['descriptions'])) {
		return new WP_REST_Response(array('error' => 'Body must be an object with a "descriptions" map.'), 400);
	}

	if (function_exists('set_time_limit')) {
		@set_time_limit(0);
	}

	$dry_run  = ! empty($body['dry_run']);
	$statuses = (isset($body['statuses']) && is_array($body['statuses']) && $body['statuses'])
		? array_map('sanitize_key', $body['statuses'])
		: array('publish', 'draft', 'future', 'pending');

	// Build the new body once per act, keyed by normalized name. Acts whose text is
	// blank or yields no paragraphs are recorded as skipped and never matched.
	$blocks_by_key = array();      // normkey => Gutenberg block markup
	$artist_by_key = array();      // normkey => display artist name (for reporting)
	$skipped       = array();
	foreach ($body['descriptions'] as $artist => $desc) {
		$artist = (string) $artist;
		$key    = tour_calendar_norm_title($artist);
		if ('' === $key) {
			continue;
		}
		$blocks = tour_calendar_text_to_blocks((string) $desc);
		if ('' === $blocks) {
			$skipped[] = array('artist' => $artist, 'reason' => 'empty_description');
			continue;
		}
		$blocks_by_key[$key] = $blocks;
		$artist_by_key[$key] = $artist;
	}

	$updated         = array();
	$errors          = array();
	$matched_keys    = array();

	if ($blocks_by_key) {
		$event_ids = get_posts(
			array(
				'post_type'   => 'event',
				'post_status' => $statuses,
				'numberposts' => -1,
				'fields'      => 'ids',
			)
		);

		foreach ($event_ids as $eid) {
			$eid   = (int) $eid;
			$title = get_post_field('post_title', $eid);
			$key   = tour_calendar_norm_title($title);
			if (! isset($blocks_by_key[$key])) {
				continue;
			}
			$matched_keys[$key] = true;

			$post   = get_post($eid);
			$status = $post ? $post->post_status : '';
			// Preserve this event's own ticket button.
			$link    = (string) get_post_meta($eid, 'event-link', true);
			$content = tour_calendar_apply_ticket_button($blocks_by_key[$key], $link);

			if ($dry_run) {
				$updated[] = array('id' => $eid, 'artist' => $artist_by_key[$key], 'status' => $status, 'title' => $title);
				continue;
			}

			$res = wp_update_post(array('ID' => $eid, 'post_content' => $content), true);
			if (is_wp_error($res)) {
				$errors[] = array('id' => $eid, 'artist' => $artist_by_key[$key], 'error' => $res->get_error_message());
				continue;
			}
			$updated[] = array('id' => $eid, 'artist' => $artist_by_key[$key], 'status' => $status, 'title' => $title);
		}
	}

	// Split the acts we had a body for into those that matched at least one event
	// and those that matched none — so a name mismatch (folder vs. event title)
	// doesn't pass silently.
	$matched   = array();
	$unmatched = array();
	foreach ($artist_by_key as $key => $artist) {
		if (isset($matched_keys[$key])) {
			$matched[] = $artist;
		} else {
			$unmatched[] = $artist;
		}
	}

	return new WP_REST_Response(
		array(
			'ok'                => true,
			'dry_run'           => $dry_run,
			'updated'           => $updated,
			'skipped'           => $skipped,
			'errors'            => $errors,
			'matched_artists'   => $matched,
			'unmatched_artists' => $unmatched,
		),
		200
	);
}

/* -------------------------------------------------------------------------- *
 *  REST: update-links endpoint
 * -------------------------------------------------------------------------- */

/**
 * Update the ticket link on EXISTING events, matched per show by act + date. Used to
 * push corrected venue-direct links onto event posts (including drafts) without
 * recreating them.
 *
 * Body: {
 *   "dry_run":  bool,                          // when true, plan only — write nothing
 *   "links":    [ {"artist":..,"date":"YYYY-MM-DD","ticket_url":..,"force":bool}, ... ],
 *   "statuses": [ "publish", "draft", ... ]    // optional; default publish+draft+future+pending
 * }
 *
 * Matching mirrors the publish dedup: tour_calendar_norm_title(title) + '|' +
 * gmdate('Y-m-d', event-date) == norm_title(artist) + '|' + date. For each match the
 * event-link meta is set and the "Venue Website" button in the body is re-applied to the
 * new URL (tour_calendar_apply_ticket_button) — so an event with NO link/button gets one
 * added. Per-link "force" controls what happens when an event already has a *different*
 * non-empty link: force=true overwrites it (a corrected/broken link); force=false leaves
 * it alone (reported as "kept") and only fills events whose link is empty. An event
 * already pointing at the given URL is reported as unchanged.
 */
function tour_calendar_rest_update_links(WP_REST_Request $request)
{
	$body = $request->get_json_params();

	if (! is_array($body) || ! isset($body['links']) || ! is_array($body['links'])) {
		return new WP_REST_Response(array('error' => 'Body must be an object with a "links" array.'), 400);
	}

	if (function_exists('set_time_limit')) {
		@set_time_limit(0);
	}

	$dry_run  = ! empty($body['dry_run']);
	$statuses = (isset($body['statuses']) && is_array($body['statuses']) && $body['statuses'])
		? array_map('sanitize_key', $body['statuses'])
		: array('publish', 'draft', 'future', 'pending');

	// Build "<normkey>|Y-m-d" => array('url'=>, 'force'=>). Last entry wins on a collision.
	$want_by_key = array();
	foreach ($body['links'] as $row) {
		if (! is_array($row)) {
			continue;
		}
		$artist = isset($row['artist']) ? (string) $row['artist'] : '';
		$date   = isset($row['date']) ? substr((string) $row['date'], 0, 10) : '';
		$url    = isset($row['ticket_url']) ? esc_url_raw((string) $row['ticket_url']) : '';
		$key    = tour_calendar_norm_title($artist);
		if ('' === $key || '' === $date || '' === $url) {
			continue;
		}
		$want_by_key[$key . '|' . $date] = array('url' => $url, 'force' => ! empty($row['force']));
	}

	$updated   = array();
	$added     = array();
	$unchanged = array();
	$kept      = array();
	$errors    = array();
	$matched   = array();

	if ($want_by_key) {
		$event_ids = get_posts(
			array(
				'post_type'   => 'event',
				'post_status' => $statuses,
				'numberposts' => -1,
				'fields'      => 'ids',
			)
		);

		foreach ($event_ids as $eid) {
			$eid = (int) $eid;
			$ed  = (int) get_post_meta($eid, 'event-date', true);
			if (! $ed) {
				continue;
			}
			$key = tour_calendar_norm_title(get_post_field('post_title', $eid)) . '|' . gmdate('Y-m-d', $ed);
			if (! isset($want_by_key[$key])) {
				continue;
			}
			$matched[$key] = true;
			$url           = $want_by_key[$key]['url'];
			$force         = $want_by_key[$key]['force'];
			$post          = get_post($eid);
			$status        = $post ? $post->post_status : '';
			$current       = (string) get_post_meta($eid, 'event-link', true);
			$is_add        = ('' === $current);

			if ($current === $url) {
				$unchanged[] = array('id' => $eid, 'status' => $status, 'url' => $url);
				continue;
			}
			// An existing, different, non-empty link is only replaced when forced
			// (e.g. a corrected/broken link). Otherwise leave it as-is.
			if (! $is_add && ! $force) {
				$kept[] = array('id' => $eid, 'status' => $status, 'url' => $current);
				continue;
			}

			$entry = array('id' => $eid, 'status' => $status, 'old' => $current, 'url' => $url);
			if ($dry_run) {
				if ($is_add) {
					$added[] = $entry;
				} else {
					$updated[] = $entry;
				}
				continue;
			}

			$content = tour_calendar_apply_ticket_button((string) ($post ? $post->post_content : ''), $url);
			$res     = wp_update_post(array('ID' => $eid, 'post_content' => $content), true);
			if (is_wp_error($res)) {
				$errors[] = array('id' => $eid, 'error' => $res->get_error_message());
				continue;
			}
			update_post_meta($eid, 'event-link', $url);
			if ($is_add) {
				$added[] = $entry;
			} else {
				$updated[] = $entry;
			}
		}
	}

	// Report which requested keys matched no event, so a name/date mismatch isn't silent.
	$unmatched = array();
	foreach ($want_by_key as $key => $_want) {
		if (! isset($matched[$key])) {
			$unmatched[] = $key;
		}
	}

	return new WP_REST_Response(
		array(
			'ok'        => true,
			'dry_run'   => $dry_run,
			'updated'   => $updated,
			'added'     => $added,
			'unchanged' => $unchanged,
			'kept'      => $kept,
			'unmatched' => $unmatched,
			'errors'    => $errors,
		),
		200
	);
}

/* -------------------------------------------------------------------------- *
 *  REST: list-events endpoint (read-only)
 * -------------------------------------------------------------------------- */

/**
 * Return every `event` post with the fields needed to reconcile against an external
 * source: id, title (act), status, date (Y-m-d from event-date), ticket link, location.
 * Read-only — writes nothing.
 *
 * Body (optional): { "statuses": [ "publish", "draft", ... ] }  // default all four
 */
function tour_calendar_rest_list_events(WP_REST_Request $request)
{
	$body     = $request->get_json_params();
	$statuses = (is_array($body) && isset($body['statuses']) && is_array($body['statuses']) && $body['statuses'])
		? array_map('sanitize_key', $body['statuses'])
		: array('publish', 'draft', 'future', 'pending');

	$ids = get_posts(
		array(
			'post_type'   => 'event',
			'post_status' => $statuses,
			'numberposts' => -1,
			'fields'      => 'ids',
		)
	);

	$events = array();
	foreach ($ids as $eid) {
		$eid = (int) $eid;
		$ed  = (int) get_post_meta($eid, 'event-date', true);
		$events[] = array(
			'id'       => $eid,
			'title'    => get_post_field('post_title', $eid),
			'status'   => get_post_status($eid),
			'date'     => $ed ? gmdate('Y-m-d', $ed) : '',
			'link'     => (string) get_post_meta($eid, 'event-link', true),
			'location' => (string) get_post_meta($eid, 'event-location', true),
		);
	}

	return new WP_REST_Response(array('ok' => true, 'events' => $events), 200);
}

/**
 * REST: trash specific `event` posts by ID. For surgical cleanup the title/date-keyed
 * tools can't target (e.g. a duplicate or an off-roster-titled event).
 *
 * Body: { "ids": [int,...], "dry_run": bool, "force_delete": bool }
 *
 * Safety: only posts of type `event` are touched — any other ID (wrong type, missing,
 * already trashed) is reported under "skipped", never acted on. wp_trash_post moves to
 * Trash (recoverable); force_delete permanently deletes. dry_run plans only.
 */
function tour_calendar_rest_trash_events(WP_REST_Request $request)
{
	$body = $request->get_json_params();
	if (! is_array($body) || ! isset($body['ids']) || ! is_array($body['ids'])) {
		return new WP_REST_Response(array('error' => 'Body must be an object with an "ids" array.'), 400);
	}
	$dry_run = ! empty($body['dry_run']);
	$force   = ! empty($body['force_delete']);

	$trashed = array();
	$skipped = array();
	$errors  = array();
	foreach ($body['ids'] as $raw) {
		$id   = (int) $raw;
		$post = $id ? get_post($id) : null;
		if (! $post || 'event' !== $post->post_type) {
			$skipped[] = array('id' => $id, 'reason' => $post ? ('not_an_event:' . $post->post_type) : 'not_found');
			continue;
		}
		if ('trash' === $post->post_status && ! $force) {
			$skipped[] = array('id' => $id, 'reason' => 'already_trashed', 'title' => $post->post_title);
			continue;
		}
		$row = array('id' => $id, 'title' => $post->post_title, 'status' => $post->post_status);
		if ($dry_run) {
			$trashed[] = $row;
			continue;
		}
		$res = $force ? wp_delete_post($id, true) : wp_trash_post($id);
		if ($res) {
			$row['action'] = $force ? 'deleted' : 'trashed';
			$trashed[]     = $row;
		} else {
			$errors[] = array('id' => $id, 'error' => 'wp_' . ($force ? 'delete' : 'trash') . '_post returned falsy');
		}
	}

	return new WP_REST_Response(
		array(
			'ok'      => true,
			'dry_run' => $dry_run,
			'force'   => $force,
			'trashed' => $trashed,
			'skipped' => $skipped,
			'errors'  => $errors,
		),
		200
	);
}

/**
 * Normalize a title for matching: lowercase, strip everything but a-z0-9.
 * "Arrival From Sweden: The Music of ABBA" -> "arrivalfromswedenthemusicofabba".
 */
function tour_calendar_norm_title($title)
{
	// Decode entities first (&#038; / &amp; -> &) so an HTML-encoded title and the raw
	// artist string normalize identically — otherwise the "&" entity's digits survive
	// the strip and break matching.
	$title = html_entity_decode((string) $title, ENT_QUOTES | ENT_HTML5, 'UTF-8');
	return preg_replace('/[^a-z0-9]+/', '', strtolower($title));
}

/**
 * Assign existing `event_cat` terms (matched by exact name) to an event. Terms that
 * don't already exist on the site are skipped — this NEVER creates new categories,
 * so a typo or unexpected name in the payload can't spawn a stray term. Replaces any
 * existing terms on the post. Returns the term names actually applied.
 */
function tour_calendar_assign_event_cats($post_id, $names)
{
	if (! is_array($names)) {
		return array();
	}
	$term_ids = array();
	$applied  = array();
	foreach ($names as $name) {
		$name = trim((string) $name);
		if ('' === $name) {
			continue;
		}
		$term = get_term_by('name', $name, 'event_cat');
		if ($term && ! is_wp_error($term)) {
			$term_ids[] = (int) $term->term_id;
			$applied[]  = $term->name;
		}
	}
	if ($term_ids) {
		wp_set_object_terms($post_id, $term_ids, 'event_cat', false);
	}
	return $applied;
}

/**
 * Reconcile an EXISTING event's categories: ADD any of the act's mapped terms that are
 * missing, without removing terms already on the event. This brings the back catalogue
 * into line on a re-run (e.g. an event tagged only "Magic" gains the act's other terms)
 * while preserving anything set by hand. Only existing terms are added (a name with no
 * term is skipped), and no write happens when nothing is missing. Returns names added.
 */
function tour_calendar_reconcile_event_cats($post_id, $names)
{
	if (! is_array($names) || empty($names)) {
		return array();
	}
	$existing = wp_get_object_terms($post_id, 'event_cat', array('fields' => 'names'));
	if (is_wp_error($existing)) {
		return array();
	}
	$have      = array_map('strtolower', $existing);
	$add_ids   = array();
	$added     = array();
	foreach ($names as $name) {
		$name = trim((string) $name);
		if ('' === $name || in_array(strtolower($name), $have, true)) {
			continue;
		}
		$term = get_term_by('name', $name, 'event_cat');
		if ($term && ! is_wp_error($term)) {
			$add_ids[] = (int) $term->term_id;
			$added[]   = $term->name;
		}
	}
	if ($add_ids) {
		wp_set_object_terms($post_id, $add_ids, 'event_cat', true); // append, don't replace
	}
	return $added;
}

/**
 * Build the event-location string from a show's venue/city/region, dropping
 * any empty parts: "Venue, City, ST".
 */
function tour_calendar_join_location($show)
{
	$parts = array();
	foreach (array('venue', 'city', 'region') as $f) {
		$val = isset($show[$f]) ? trim((string) $show[$f]) : '';
		if ('' !== $val) {
			$parts[] = $val;
		}
	}
	return sanitize_text_field(implode(', ', $parts));
}

/**
 * Normalize an event-location string for equality matching (lowercase, collapse
 * whitespace, trim). Used by the residency migration to confirm a single event sits at
 * the same venue as the range event before trashing it.
 */
function tour_calendar_norm_loc($loc)
{
	return strtolower(trim(preg_replace('/\s+/', ' ', (string) $loc)));
}

/**
 * Distinctive lowercased tokens of a venue/location string: alphanumeric words of length
 * >= 4 that aren't generic venue words. Mirrors aggregation._venue_tokens in the Python
 * job so the residency trash-match identifies the same venue across spelling/format
 * differences (e.g. "54 Below, 254 W 54th St…" vs a residency venue "54 BELOW" -> "below").
 */
function tour_calendar_venue_tokens($s)
{
	static $stop = array(
		'the', 'and', 'for', 'theatre', 'theater', 'center', 'centre', 'performing',
		'arts', 'hall', 'stage', 'room', 'live', 'park', 'amphitheater', 'amphitheatre',
		'casino', 'resort', 'hotel',
	);
	$tokens = array();
	foreach (preg_split('/[^a-z0-9]+/', strtolower((string) $s), -1, PREG_SPLIT_NO_EMPTY) as $t) {
		if (strlen($t) >= 4 && ! in_array($t, $stop, true)) {
			$tokens[$t] = true;
		}
	}
	return array_keys($tokens);
}

/**
 * Cleanup pass for a plain-text description before it becomes paragraph blocks.
 * Tidies the mess typical of hand-made .txt files so a stray line break doesn't
 * survive into the published event:
 *   - CRLF / CR line endings        -> LF
 *   - non-breaking / stray spaces   -> normal spaces
 *   - trailing spaces on each line  -> trimmed
 *   - 3+ blank lines                -> a single blank line (one paragraph break)
 *   - hard-wrapped paragraphs       -> unwrapped: single line breaks within a
 *                                      paragraph are joined back into flowing text,
 *                                      and runs of spaces are collapsed
 * Paragraphs are delimited by blank lines, so deliberate structure is preserved
 * while arbitrary mid-sentence wrapping is removed. Returns '' for empty input.
 */
function tour_calendar_clean_text($text)
{
	$text = (string) $text;
	$text = str_replace(array("\r\n", "\r"), "\n", $text);
	$text = str_replace(array("\xC2\xA0", "\xE2\x80\xAF"), ' ', $text); // NBSP, narrow NBSP
	$text = preg_replace('/[ \t]+\n/', "\n", $text);      // trailing spaces per line
	$text = preg_replace('/\n{3,}/', "\n\n", trim($text)); // collapse blank runs

	$paragraphs = array();
	foreach (preg_split('/\n\n/', $text) as $para) {
		$para = trim($para);
		// Rejoin a word hyphenated across a line break ("foot-\nstomping" ->
		// "foot-stomping") with no inserted space, before unwrapping the rest.
		$para = preg_replace('/(\p{L})-\n[ \t]*(\p{L})/u', '$1-$2', $para);
		$para = preg_replace('/\s*\n\s*/', ' ', $para); // unwrap remaining soft breaks
		$para = preg_replace('/[ \t]{2,}/', ' ', $para); // collapse spaces
		if ('' !== trim($para)) {
			$paragraphs[] = $para;
		}
	}
	return implode("\n\n", $paragraphs);
}

/**
 * Convert a plain-text description (e.g. a Drive .txt file) into Gutenberg
 * paragraph blocks. Runs the cleanup pass first, then emits one paragraph block
 * per blank-line-delimited paragraph. A one-line description yields a single
 * paragraph; an empty string yields ''. Sanitized per paragraph with
 * wp_kses_post so the block comments survive (running kses over the whole block
 * markup would strip the <!-- wp:* --> tags).
 */
function tour_calendar_text_to_blocks($text)
{
	$clean = tour_calendar_clean_text($text);
	if ('' === $clean) {
		return '';
	}
	$blocks = array();
	foreach (explode("\n\n", $clean) as $para) {
		$blocks[] = "<!-- wp:paragraph -->\n<p>" . wp_kses_post($para) . "</p>\n<!-- /wp:paragraph -->";
	}
	return implode("\n\n", $blocks);
}

/**
 * Build a "Show Dates" heading + paragraph listing each residency date (and its time)
 * as Gutenberg blocks, for the top of a monthly residency event's body. $dates is the
 * payload's residency_dates: [ {date: "YYYY-MM-DD", start_time: "7:00 PM"|""}, ... ].
 * Returns '' when no valid dates are present.
 */
function tour_calendar_residency_dates_block($dates)
{
	$lines = array();
	foreach ((array) $dates as $d) {
		if (! is_array($d)) {
			continue;
		}
		$iso = isset($d['date']) ? (string) $d['date'] : '';
		$ts  = '' !== $iso ? strtotime($iso) : false;
		if (! $ts) {
			continue;
		}
		$label = gmdate('D, M j, Y', $ts);
		$tm    = isset($d['start_time']) ? trim((string) $d['start_time']) : '';
		$lines[] = '' !== $tm ? esc_html($label . ' · ' . $tm) : esc_html($label);
	}
	if (empty($lines)) {
		return '';
	}
	$heading = "<!-- wp:heading {\"level\":3} -->\n<h3>Show Dates</h3>\n<!-- /wp:heading -->";
	$body    = "<!-- wp:paragraph -->\n<p>" . implode('<br>', $lines) . "</p>\n<!-- /wp:paragraph -->";
	return $heading . "\n\n" . $body . "\n\n";
}

/**
 * The red "Venue Website" Gutenberg button block, pointed at $url. Markup mirrors
 * the existing events on the site (vivid-red background, white text, square corners).
 */
function tour_calendar_venue_button_html($url)
{
	$href = esc_url($url);
	// Canonical *stored* block markup. Render-only classes (is-layout-flex,
	// wp-block-buttons-is-layout-flex, has-link-color) are deliberately omitted —
	// WordPress adds those at render time, and including them makes Gutenberg flag
	// the block as invalid ("Resolve Block").
	return "\n<!-- wp:buttons -->\n"
		. '<div class="wp-block-buttons">'
		. '<!-- wp:button {"backgroundColor":"vivid-red","textColor":"white","style":{"border":{"radius":"0px"}}} -->'
		. "\n" . '<div class="wp-block-button"><a class="wp-block-button__link has-white-color has-vivid-red-background-color has-text-color has-background wp-element-button" href="' . $href . '" style="border-radius:0px">Venue Website</a></div>' . "\n"
		. '<!-- /wp:button -->'
		. '</div>'
		. "\n<!-- /wp:buttons -->\n";
}

/**
 * Normalize the "Venue Website" button in a post body: strip any existing one
 * (block-comment form or bare-HTML form) and, when a ticket URL is given, append
 * a fresh button pointing at it. With no URL the button is simply removed so we
 * never leave a stale/404 link behind.
 */
function tour_calendar_apply_ticket_button($content, $url)
{
	// Commented block form (what the editor stores), tempered so it can't run past
	// one block's closing comment before reaching the "Venue Website" label. The
	// opening comment may carry attributes — e.g. <!-- wp:buttons {"layout":...} -->
	// for a centered button — so allow anything up to the closing "-->"; matching
	// only the bare "wp:buttons -->" leaves attributed buttons behind, which then
	// surface as invalid blocks in the editor.
	$content = preg_replace(
		'#<!--\s*wp:buttons\b[^>]*-->(?:(?!<!--\s*/wp:buttons\s*-->).)*?Venue Website.*?<!--\s*/wp:buttons\s*-->#is',
		'',
		$content
	);
	// Bare rendered form (no block comments). Allow extra classes on both the
	// wrapper and the button div (render-time layout classes, has-link-color, etc.).
	$content = preg_replace(
		'#<div class="wp-block-buttons[^"]*">\s*<div class="wp-block-button[^"]*">\s*<a[^>]*>\s*Venue Website\s*</a>\s*</div>\s*</div>#is',
		'',
		$content
	);

	$content = rtrim((string) $content);
	if ('' !== $url) {
		$content .= tour_calendar_venue_button_html($url);
	}
	return $content;
}

/**
 * Decode a base64 image, store it in the media library, and attach it to a post.
 * Returns the attachment ID or a WP_Error.
 */
function tour_calendar_sideload_b64($b64, $filename, $parent_id)
{
	$data = base64_decode($b64, true);
	if (false === $data) {
		return new WP_Error('tour_calendar_bad_b64', 'Could not decode image data.');
	}
	$filename = sanitize_file_name($filename);
	$upload   = wp_upload_bits($filename, null, $data);
	if (! empty($upload['error'])) {
		return new WP_Error('tour_calendar_upload_failed', $upload['error']);
	}

	$filetype   = wp_check_filetype($upload['file'], null);
	$attachment = array(
		'post_mime_type' => $filetype['type'],
		'post_title'     => sanitize_file_name(pathinfo($filename, PATHINFO_FILENAME)),
		'post_content'   => '',
		'post_status'    => 'inherit',
	);
	$att_id = wp_insert_attachment($attachment, $upload['file'], $parent_id);
	if (is_wp_error($att_id)) {
		return $att_id;
	}

	require_once ABSPATH . 'wp-admin/includes/image.php';
	$meta = wp_generate_attachment_metadata($att_id, $upload['file']);
	wp_update_attachment_metadata($att_id, $meta);

	return $att_id;
}

/* -------------------------------------------------------------------------- *
 *  Shortcode: [tour-calendar]
 * -------------------------------------------------------------------------- */

add_shortcode('tour-calendar', 'tour_calendar_shortcode');

/**
 * Render the calendar. Attributes:
 *   require_login="yes"  — render nothing (a notice) unless the visitor is
 *                          logged in. Default: rely on page-level visibility.
 */
function tour_calendar_shortcode($atts)
{
	$atts = shortcode_atts(array('require_login' => 'no'), $atts, 'tour-calendar');

	if ('yes' === strtolower((string) $atts['require_login']) && ! is_user_logged_in()) {
		return '<p class="tcal-notice">Please log in to view tour dates.</p>';
	}

	$dir = plugin_dir_url(__FILE__) . 'assets/';
	$ver = TOUR_CALENDAR_VERSION;
	wp_enqueue_style('tour-calendar', $dir . 'app.css', array(), $ver);
	wp_enqueue_script('tour-calendar-formats', $dir . 'formats.js', array(), $ver, true);
	wp_enqueue_script('tour-calendar-app', $dir . 'app.js', array('tour-calendar-formats'), $ver, true);

	$payload      = get_option(TOUR_CALENDAR_OPTION_PAYLOAD, '');
	$generated_at = get_option(TOUR_CALENDAR_OPTION_GENERATED, '');

	if ('' === $payload) {
		$payload = wp_json_encode(array('generated_at' => '', 'shows' => array()));
	}

	// Safe to embed inside a JSON <script>: only break out via "</".
	$inline = str_replace('</', '<\/', $payload);

	ob_start();
?>
	<div class="tcal-root" data-generated-at="<?php echo esc_attr($generated_at); ?>">
		<script type="application/json" class="tcal-data">
			<?php echo $inline; // phpcs:ignore WordPress.Security.EscapeOutput.OutputNotEscaped — JSON, already escaped for </ break-out. 
			?>
		</script>
		<div class="tcal-mount"><noscript>This tour calendar requires JavaScript.</noscript></div>
	</div>
<?php
	return ob_get_clean();
}

/* -------------------------------------------------------------------------- *
 *  Admin settings page
 * -------------------------------------------------------------------------- */

add_action('admin_menu', function () {
	add_options_page(
		'Tour Calendar',
		'Tour Calendar',
		'manage_options',
		'tour-calendar',
		'tour_calendar_settings_page'
	);
});

function tour_calendar_settings_page()
{
	if (! current_user_can('manage_options')) {
		return;
	}

	// Handle manual JSON paste fallback.
	if (isset($_POST['tcal_manual_json']) && check_admin_referer('tcal_manual_save')) {
		$raw     = wp_unslash($_POST['tcal_manual_json']); // phpcs:ignore WordPress.Security.ValidatedSanitizedInput — validated as JSON below.
		$decoded = json_decode($raw, true);
		if (is_array($decoded) && isset($decoded['shows']) && is_array($decoded['shows'])) {
			update_option(TOUR_CALENDAR_OPTION_PAYLOAD, wp_json_encode($decoded), false);
			update_option(TOUR_CALENDAR_OPTION_GENERATED, isset($decoded['generated_at']) ? sanitize_text_field((string) $decoded['generated_at']) : gmdate('c'), false);
			echo '<div class="notice notice-success"><p>Saved ' . esc_html((string) count($decoded['shows'])) . ' shows.</p></div>';
		} else {
			echo '<div class="notice notice-error"><p>Invalid JSON — expected an object with a "shows" array.</p></div>';
		}
	}

	// Handle secret regeneration.
	if (isset($_POST['tcal_regen_secret']) && check_admin_referer('tcal_regen_secret')) {
		if (defined('TOUR_DATES_SECRET')) {
			echo '<div class="notice notice-warning"><p>Secret is pinned by the TOUR_DATES_SECRET constant in wp-config.php; cannot regenerate here.</p></div>';
		} else {
			update_option(TOUR_CALENDAR_OPTION_SECRET, wp_generate_password(48, false, false));
			echo '<div class="notice notice-success"><p>Generated a new ingest secret. Update OUTPUT_WEBSITE_SECRET in the Python job.</p></div>';
		}
	}

	$secret       = tour_calendar_get_secret();
	$generated_at = get_option(TOUR_CALENDAR_OPTION_GENERATED, '');
	$payload      = get_option(TOUR_CALENDAR_OPTION_PAYLOAD, '');
	$count        = 0;
	if ($payload) {
		$decoded = json_decode($payload, true);
		$count   = (is_array($decoded) && isset($decoded['shows'])) ? count($decoded['shows']) : 0;
	}
	$ingest_url = esc_url(rest_url('tour-dates/v1/ingest'));
?>
	<div class="wrap">
		<h1>Tour Calendar</h1>

		<h2>Status</h2>
		<table class="form-table">
			<tr>
				<th>Shows stored</th>
				<td><?php echo esc_html((string) $count); ?></td>
			</tr>
			<tr>
				<th>Last updated</th>
				<td><?php echo $generated_at ? esc_html($generated_at) : '<em>never</em>'; ?></td>
			</tr>
			<tr>
				<th>Ingest URL</th>
				<td><code><?php echo $ingest_url; ?></code></td>
			</tr>
			<tr>
				<th>Shortcode</th>
				<td><code>[tour-calendar]</code> — add to any (gated) page.</td>
			</tr>
		</table>

		<h2>Ingest secret</h2>
		<p>Send this as the <code>X-Tour-Secret</code> header. Set it as <code>OUTPUT_WEBSITE_SECRET</code> in the Python job's environment.</p>
		<p><input type="text" class="regular-text" readonly value="<?php echo esc_attr($secret); ?>" onclick="this.select()"></p>
		<?php if (defined('TOUR_DATES_SECRET')) : ?>
			<p><em>Pinned by the TOUR_DATES_SECRET constant in wp-config.php.</em></p>
		<?php else : ?>
			<form method="post">
				<?php wp_nonce_field('tcal_regen_secret'); ?>
				<input type="hidden" name="tcal_regen_secret" value="1">
				<?php submit_button('Regenerate secret', 'secondary'); ?>
			</form>
		<?php endif; ?>

		<h2>Manual data paste (fallback)</h2>
		<p>If the weekly push fails, paste the contents of <code>tour_dates.json</code> here.</p>
		<form method="post">
			<?php wp_nonce_field('tcal_manual_save'); ?>
			<textarea name="tcal_manual_json" rows="10" class="large-text code" placeholder='{ "generated_at": "...", "shows": [ ... ] }'></textarea>
			<?php submit_button('Save pasted JSON'); ?>
		</form>
	</div>
<?php
}
