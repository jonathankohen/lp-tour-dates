<?php
/**
 * Plugin Name:       Tour Calendar
 * Description:        Sortable calendar + list of aggregated tour dates with one-click copy-paste outreach formats. Data is pushed weekly by the love-automations Python job.
 * Version:           1.0.0
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

if ( ! defined( 'ABSPATH' ) ) {
	exit; // No direct access.
}

define( 'TOUR_CALENDAR_VERSION', '1.0.0' );
define( 'TOUR_CALENDAR_OPTION_PAYLOAD', 'tour_dates_payload' );
define( 'TOUR_CALENDAR_OPTION_GENERATED', 'tour_dates_generated_at' );
define( 'TOUR_CALENDAR_OPTION_SECRET', 'tour_dates_secret' );

/**
 * Resolve the shared ingest secret.
 *
 * Prefer a constant in wp-config.php (define('TOUR_DATES_SECRET', '...'); —
 * not stored in the database, the most secure option). Otherwise fall back to
 * an auto-generated option created on activation.
 */
function tour_calendar_get_secret() {
	if ( defined( 'TOUR_DATES_SECRET' ) && TOUR_DATES_SECRET ) {
		return (string) TOUR_DATES_SECRET;
	}
	return (string) get_option( TOUR_CALENDAR_OPTION_SECRET, '' );
}

/**
 * Generate a secret on activation if one is not already configured.
 */
function tour_calendar_activate() {
	if ( ! defined( 'TOUR_DATES_SECRET' ) && ! get_option( TOUR_CALENDAR_OPTION_SECRET ) ) {
		add_option( TOUR_CALENDAR_OPTION_SECRET, wp_generate_password( 48, false, false ) );
	}
}
register_activation_hook( __FILE__, 'tour_calendar_activate' );

/* -------------------------------------------------------------------------- *
 *  REST: ingest endpoint
 * -------------------------------------------------------------------------- */

add_action( 'rest_api_init', function () {
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
} );

/**
 * Constant-time secret check via the X-Tour-Secret header.
 */
function tour_calendar_rest_ingest_permission( WP_REST_Request $request ) {
	$expected = tour_calendar_get_secret();
	$provided = (string) $request->get_header( 'x-tour-secret' );

	if ( '' === $expected ) {
		return new WP_Error( 'tour_calendar_no_secret', 'Ingest secret is not configured on the server.', array( 'status' => 500 ) );
	}
	if ( ! hash_equals( $expected, $provided ) ) {
		return new WP_Error( 'tour_calendar_forbidden', 'Invalid or missing X-Tour-Secret header.', array( 'status' => 403 ) );
	}
	return true;
}

/**
 * Validate and store the incoming payload.
 *
 * Accepts { "generated_at"?: string, "shows": [ {artist,date,venue,city,
 * region,country,ticket_url,source,raw_id}, ... ] }. Unknown keys on each show
 * are dropped; required keys are coerced to strings.
 */
function tour_calendar_rest_ingest( WP_REST_Request $request ) {
	$body = $request->get_json_params();

	if ( ! is_array( $body ) || ! isset( $body['shows'] ) || ! is_array( $body['shows'] ) ) {
		return new WP_REST_Response( array( 'error' => 'Body must be an object with a "shows" array.' ), 400 );
	}

	$fields = array( 'artist', 'date', 'venue', 'city', 'region', 'country', 'ticket_url', 'source', 'raw_id', 'start_time' );
	$clean  = array();

	foreach ( $body['shows'] as $show ) {
		if ( ! is_array( $show ) ) {
			continue;
		}
		$row = array();
		foreach ( $fields as $f ) {
			$row[ $f ] = isset( $show[ $f ] ) ? (string) $show[ $f ] : '';
		}
		// A show is only meaningful with at least an artist and a date.
		if ( '' === $row['artist'] || '' === $row['date'] ) {
			continue;
		}
		$clean[] = $row;
	}

	$generated_at = isset( $body['generated_at'] ) ? sanitize_text_field( (string) $body['generated_at'] ) : gmdate( 'c' );

	$payload = wp_json_encode(
		array(
			'generated_at' => $generated_at,
			'shows'        => $clean,
		)
	);

	update_option( TOUR_CALENDAR_OPTION_PAYLOAD, $payload, false );
	update_option( TOUR_CALENDAR_OPTION_GENERATED, $generated_at, false );

	return new WP_REST_Response(
		array(
			'ok'           => true,
			'stored'       => count( $clean ),
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
 *   "assets":       { "<artist>": { "image_b64", "image_filename", "description" } }
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
function tour_calendar_rest_publish_events( WP_REST_Request $request ) {
	$body = $request->get_json_params();

	if ( ! is_array( $body ) || ! isset( $body['shows'] ) || ! is_array( $body['shows'] ) ) {
		return new WP_REST_Response( array( 'error' => 'Body must be an object with a "shows" array.' ), 400 );
	}

	$dry_run      = ! empty( $body['dry_run'] );
	$default_time = isset( $body['default_time'] ) ? sanitize_text_field( (string) $body['default_time'] ) : '';
	$limit        = isset( $body['limit'] ) ? max( 0, (int) $body['limit'] ) : 0;
	$assets       = ( isset( $body['assets'] ) && is_array( $body['assets'] ) ) ? $body['assets'] : array();

	// Two indexes built from existing events, kept separate so dry-run planning can
	// claim a day without creating a fake template:
	//   $by_title — normalized title => events, used only to pick an image/body template.
	//   $seen_day — "<normkey>|Y-m-d" => true, used only for act+date dedup.
	$by_title  = array();
	$seen_day  = array();
	$event_ids = get_posts(
		array(
			'post_type'   => 'event',
			'post_status' => array( 'publish', 'future', 'draft', 'pending' ),
			'numberposts' => -1,
			'fields'      => 'ids',
		)
	);
	foreach ( $event_ids as $eid ) {
		$title = get_the_title( $eid );
		$key   = tour_calendar_norm_title( $title );
		$ed    = (int) get_post_meta( $eid, 'event-date', true );
		if ( ! isset( $by_title[ $key ] ) ) {
			$by_title[ $key ] = array( 'title' => $title, 'events' => array() );
		}
		$by_title[ $key ]['events'][] = array( 'id' => (int) $eid, 'date' => $ed );
		if ( $ed ) {
			$seen_day[ $key . '|' . gmdate( 'Y-m-d', $ed ) ] = true;
		}
	}

	$created      = array();
	$skipped      = array();
	$would_create = array();
	$errors       = array();
	$made         = 0; // events created (real) or planned (dry) — what $limit caps.

	foreach ( $body['shows'] as $show ) {
		if ( ! is_array( $show ) ) {
			continue;
		}
		$artist = isset( $show['artist'] ) ? (string) $show['artist'] : '';
		$date   = isset( $show['date'] ) ? (string) $show['date'] : '';
		if ( '' === $artist || '' === $date ) {
			continue;
		}

		$ts            = strtotime( $date ); // local midnight, matching a manual date-picker entry
		$key           = tour_calendar_norm_title( $artist );
		$daykey        = $key . '|' . gmdate( 'Y-m-d', $ts );
		$match         = isset( $by_title[ $key ] ) ? $by_title[ $key ] : null;
		$matched_title = $match ? $match['title'] : '';

		// Dedup: an event of this act already exists (or was made this batch) that day.
		if ( isset( $seen_day[ $daykey ] ) ) {
			$skipped[] = array(
				'artist'        => $artist,
				'date'          => $date,
				'reason'        => 'exists',
				'matched_title' => $matched_title,
			);
			continue;
		}

		$location     = tour_calendar_join_location( $show );
		$link         = isset( $show['ticket_url'] ) ? esc_url_raw( (string) $show['ticket_url'] ) : '';
		// Per-show start time wins; fall back to the batch default (blank unless set).
		$show_time    = isset( $show['start_time'] ) ? sanitize_text_field( (string) $show['start_time'] ) : '';
		$time         = '' !== $show_time ? $show_time : $default_time;
		$title_to_use = $matched_title ? $matched_title : $artist;
		$template_id  = ( $match && ! empty( $match['events'] ) ) ? (int) $match['events'][0]['id'] : 0;
		$has_drive    = isset( $assets[ $artist ] ) && is_array( $assets[ $artist ] );

		// Resolve the real body text, preferring the act's Drive description and
		// falling back to a template event's content.
		$content     = '';
		$body_source = 'none';
		if ( $has_drive && ! empty( $assets[ $artist ]['description'] ) ) {
			$content     = tour_calendar_text_to_blocks( (string) $assets[ $artist ]['description'] );
			$body_source = 'drive';
		}
		if ( '' === $content && $template_id ) {
			$tpost = get_post( $template_id );
			if ( $tpost && '' !== trim( (string) $tpost->post_content ) ) {
				$content     = $tpost->post_content;
				$body_source = 'existing-event';
			}
		}

		// Resolve the image source, preferring the act's Drive image and falling
		// back to the template event's thumbnail.
		$image_source = 'none';
		if ( $has_drive && ! empty( $assets[ $artist ]['image_b64'] ) ) {
			$image_source = 'drive';
		} elseif ( $template_id && has_post_thumbnail( $template_id ) ) {
			$image_source = 'existing-event';
		}

		// Content gate: require BOTH a body and an image. Do NOT claim the day, so a
		// later same-day show that does have content can still be created.
		if ( '' === $content || 'none' === $image_source ) {
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
		if ( $limit > 0 && $made >= $limit ) {
			break;
		}

		// Point the red "Venue Website" button at this show's ticket link. A body
		// copied from a template carries the template's stale link; a Drive-sourced
		// body has no button at all — both are normalized here.
		$content = tour_calendar_apply_ticket_button( $content, $link );

		$plan = array(
			'artist'       => $artist,
			'title'        => $title_to_use,
			'date'         => $date,
			'time'         => $time,
			'location'     => $location,
			'link'         => $link,
			'body_source'  => $body_source,
			'image_source' => $image_source,
		);

		if ( $dry_run ) {
			$would_create[]      = $plan;
			$seen_day[ $daykey ] = true;
			$made++;
			continue;
		}

		$new_id = wp_insert_post(
			array(
				'post_type'    => 'event',
				'post_status'  => 'draft',
				'post_title'   => $title_to_use,
				'post_content' => $content,
			),
			true
		);
		if ( is_wp_error( $new_id ) ) {
			$errors[] = array(
				'artist' => $artist,
				'date'   => $date,
				'error'  => $new_id->get_error_message(),
			);
			continue;
		}

		update_post_meta( $new_id, 'event-date', $ts );
		if ( '' !== $time ) {
			update_post_meta( $new_id, 'event-time', $time );
		}
		update_post_meta( $new_id, 'event-location', $location );
		update_post_meta( $new_id, 'event-link', $link );

		// Featured image: reuse the template event's, else sideload the Drive image.
		if ( 'existing-event' === $image_source ) {
			$tid = get_post_thumbnail_id( $template_id );
			if ( $tid ) {
				set_post_thumbnail( $new_id, $tid );
			}
		} elseif ( 'drive' === $image_source ) {
			$fname  = ! empty( $assets[ $artist ]['image_filename'] ) ? (string) $assets[ $artist ]['image_filename'] : ( $artist . '.jpg' );
			$att_id = tour_calendar_sideload_b64( (string) $assets[ $artist ]['image_b64'], $fname, $new_id );
			if ( $att_id && ! is_wp_error( $att_id ) ) {
				set_post_thumbnail( $new_id, $att_id );
			}
		}

		// Register so later shows of the same act dedup against it and can template off it.
		if ( ! isset( $by_title[ $key ] ) ) {
			$by_title[ $key ] = array( 'title' => $title_to_use, 'events' => array() );
		}
		$by_title[ $key ]['events'][] = array( 'id' => (int) $new_id, 'date' => $ts );
		$seen_day[ $daykey ]          = true;
		$made++;

		$created[] = array(
			'id'     => (int) $new_id,
			'artist' => $artist,
			'date'   => $date,
		);
	}

	return new WP_REST_Response(
		array(
			'ok'           => true,
			'dry_run'      => $dry_run,
			'created'      => $created,
			'skipped'      => $skipped,
			'would_create' => $would_create,
			'errors'       => $errors,
		),
		200
	);
}

/**
 * Normalize a title for matching: lowercase, strip everything but a-z0-9.
 * "Arrival From Sweden: The Music of ABBA" -> "arrivalfromswedenthemusicofabba".
 */
function tour_calendar_norm_title( $title ) {
	return preg_replace( '/[^a-z0-9]+/', '', strtolower( (string) $title ) );
}

/**
 * Build the event-location string from a show's venue/city/region, dropping
 * any empty parts: "Venue, City, ST".
 */
function tour_calendar_join_location( $show ) {
	$parts = array();
	foreach ( array( 'venue', 'city', 'region' ) as $f ) {
		$val = isset( $show[ $f ] ) ? trim( (string) $show[ $f ] ) : '';
		if ( '' !== $val ) {
			$parts[] = $val;
		}
	}
	return sanitize_text_field( implode( ', ', $parts ) );
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
function tour_calendar_clean_text( $text ) {
	$text = (string) $text;
	$text = str_replace( array( "\r\n", "\r" ), "\n", $text );
	$text = str_replace( array( "\xC2\xA0", "\xE2\x80\xAF" ), ' ', $text ); // NBSP, narrow NBSP
	$text = preg_replace( '/[ \t]+\n/', "\n", $text );      // trailing spaces per line
	$text = preg_replace( '/\n{3,}/', "\n\n", trim( $text ) ); // collapse blank runs

	$paragraphs = array();
	foreach ( preg_split( '/\n\n/', $text ) as $para ) {
		$para = trim( $para );
		// Rejoin a word hyphenated across a line break ("foot-\nstomping" ->
		// "foot-stomping") with no inserted space, before unwrapping the rest.
		$para = preg_replace( '/(\p{L})-\n[ \t]*(\p{L})/u', '$1-$2', $para );
		$para = preg_replace( '/\s*\n\s*/', ' ', $para ); // unwrap remaining soft breaks
		$para = preg_replace( '/[ \t]{2,}/', ' ', $para ); // collapse spaces
		if ( '' !== trim( $para ) ) {
			$paragraphs[] = $para;
		}
	}
	return implode( "\n\n", $paragraphs );
}

/**
 * Convert a plain-text description (e.g. a Drive .txt file) into Gutenberg
 * paragraph blocks. Runs the cleanup pass first, then emits one paragraph block
 * per blank-line-delimited paragraph. A one-line description yields a single
 * paragraph; an empty string yields ''. Sanitized per paragraph with
 * wp_kses_post so the block comments survive (running kses over the whole block
 * markup would strip the <!-- wp:* --> tags).
 */
function tour_calendar_text_to_blocks( $text ) {
	$clean = tour_calendar_clean_text( $text );
	if ( '' === $clean ) {
		return '';
	}
	$blocks = array();
	foreach ( explode( "\n\n", $clean ) as $para ) {
		$blocks[] = "<!-- wp:paragraph -->\n<p>" . wp_kses_post( $para ) . "</p>\n<!-- /wp:paragraph -->";
	}
	return implode( "\n\n", $blocks );
}

/**
 * The red "Venue Website" Gutenberg button block, pointed at $url. Markup mirrors
 * the existing events on the site (vivid-red background, white text, square corners).
 */
function tour_calendar_venue_button_html( $url ) {
	$href = esc_url( $url );
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
function tour_calendar_apply_ticket_button( $content, $url ) {
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

	$content = rtrim( (string) $content );
	if ( '' !== $url ) {
		$content .= tour_calendar_venue_button_html( $url );
	}
	return $content;
}

/**
 * Decode a base64 image, store it in the media library, and attach it to a post.
 * Returns the attachment ID or a WP_Error.
 */
function tour_calendar_sideload_b64( $b64, $filename, $parent_id ) {
	$data = base64_decode( $b64, true );
	if ( false === $data ) {
		return new WP_Error( 'tour_calendar_bad_b64', 'Could not decode image data.' );
	}
	$filename = sanitize_file_name( $filename );
	$upload   = wp_upload_bits( $filename, null, $data );
	if ( ! empty( $upload['error'] ) ) {
		return new WP_Error( 'tour_calendar_upload_failed', $upload['error'] );
	}

	$filetype   = wp_check_filetype( $upload['file'], null );
	$attachment = array(
		'post_mime_type' => $filetype['type'],
		'post_title'     => sanitize_file_name( pathinfo( $filename, PATHINFO_FILENAME ) ),
		'post_content'   => '',
		'post_status'    => 'inherit',
	);
	$att_id = wp_insert_attachment( $attachment, $upload['file'], $parent_id );
	if ( is_wp_error( $att_id ) ) {
		return $att_id;
	}

	require_once ABSPATH . 'wp-admin/includes/image.php';
	$meta = wp_generate_attachment_metadata( $att_id, $upload['file'] );
	wp_update_attachment_metadata( $att_id, $meta );

	return $att_id;
}

/* -------------------------------------------------------------------------- *
 *  Shortcode: [tour-calendar]
 * -------------------------------------------------------------------------- */

add_shortcode( 'tour-calendar', 'tour_calendar_shortcode' );

/**
 * Render the calendar. Attributes:
 *   require_login="yes"  — render nothing (a notice) unless the visitor is
 *                          logged in. Default: rely on page-level visibility.
 */
function tour_calendar_shortcode( $atts ) {
	$atts = shortcode_atts( array( 'require_login' => 'no' ), $atts, 'tour-calendar' );

	if ( 'yes' === strtolower( (string) $atts['require_login'] ) && ! is_user_logged_in() ) {
		return '<p class="tcal-notice">Please log in to view tour dates.</p>';
	}

	$dir = plugin_dir_url( __FILE__ ) . 'assets/';
	$ver = TOUR_CALENDAR_VERSION;
	wp_enqueue_style( 'tour-calendar', $dir . 'app.css', array(), $ver );
	wp_enqueue_script( 'tour-calendar-formats', $dir . 'formats.js', array(), $ver, true );
	wp_enqueue_script( 'tour-calendar-app', $dir . 'app.js', array( 'tour-calendar-formats' ), $ver, true );

	$payload      = get_option( TOUR_CALENDAR_OPTION_PAYLOAD, '' );
	$generated_at = get_option( TOUR_CALENDAR_OPTION_GENERATED, '' );

	if ( '' === $payload ) {
		$payload = wp_json_encode( array( 'generated_at' => '', 'shows' => array() ) );
	}

	// Safe to embed inside a JSON <script>: only break out via "</".
	$inline = str_replace( '</', '<\/', $payload );

	ob_start();
	?>
	<div class="tcal-root" data-generated-at="<?php echo esc_attr( $generated_at ); ?>">
		<script type="application/json" class="tcal-data"><?php echo $inline; // phpcs:ignore WordPress.Security.EscapeOutput.OutputNotEscaped — JSON, already escaped for </ break-out. ?></script>
		<div class="tcal-mount"><noscript>This tour calendar requires JavaScript.</noscript></div>
	</div>
	<?php
	return ob_get_clean();
}

/* -------------------------------------------------------------------------- *
 *  Admin settings page
 * -------------------------------------------------------------------------- */

add_action( 'admin_menu', function () {
	add_options_page(
		'Tour Calendar',
		'Tour Calendar',
		'manage_options',
		'tour-calendar',
		'tour_calendar_settings_page'
	);
} );

function tour_calendar_settings_page() {
	if ( ! current_user_can( 'manage_options' ) ) {
		return;
	}

	// Handle manual JSON paste fallback.
	if ( isset( $_POST['tcal_manual_json'] ) && check_admin_referer( 'tcal_manual_save' ) ) {
		$raw     = wp_unslash( $_POST['tcal_manual_json'] ); // phpcs:ignore WordPress.Security.ValidatedSanitizedInput — validated as JSON below.
		$decoded = json_decode( $raw, true );
		if ( is_array( $decoded ) && isset( $decoded['shows'] ) && is_array( $decoded['shows'] ) ) {
			update_option( TOUR_CALENDAR_OPTION_PAYLOAD, wp_json_encode( $decoded ), false );
			update_option( TOUR_CALENDAR_OPTION_GENERATED, isset( $decoded['generated_at'] ) ? sanitize_text_field( (string) $decoded['generated_at'] ) : gmdate( 'c' ), false );
			echo '<div class="notice notice-success"><p>Saved ' . esc_html( (string) count( $decoded['shows'] ) ) . ' shows.</p></div>';
		} else {
			echo '<div class="notice notice-error"><p>Invalid JSON — expected an object with a "shows" array.</p></div>';
		}
	}

	// Handle secret regeneration.
	if ( isset( $_POST['tcal_regen_secret'] ) && check_admin_referer( 'tcal_regen_secret' ) ) {
		if ( defined( 'TOUR_DATES_SECRET' ) ) {
			echo '<div class="notice notice-warning"><p>Secret is pinned by the TOUR_DATES_SECRET constant in wp-config.php; cannot regenerate here.</p></div>';
		} else {
			update_option( TOUR_CALENDAR_OPTION_SECRET, wp_generate_password( 48, false, false ) );
			echo '<div class="notice notice-success"><p>Generated a new ingest secret. Update OUTPUT_WEBSITE_SECRET in the Python job.</p></div>';
		}
	}

	$secret       = tour_calendar_get_secret();
	$generated_at = get_option( TOUR_CALENDAR_OPTION_GENERATED, '' );
	$payload      = get_option( TOUR_CALENDAR_OPTION_PAYLOAD, '' );
	$count        = 0;
	if ( $payload ) {
		$decoded = json_decode( $payload, true );
		$count   = ( is_array( $decoded ) && isset( $decoded['shows'] ) ) ? count( $decoded['shows'] ) : 0;
	}
	$ingest_url = esc_url( rest_url( 'tour-dates/v1/ingest' ) );
	?>
	<div class="wrap">
		<h1>Tour Calendar</h1>

		<h2>Status</h2>
		<table class="form-table">
			<tr><th>Shows stored</th><td><?php echo esc_html( (string) $count ); ?></td></tr>
			<tr><th>Last updated</th><td><?php echo $generated_at ? esc_html( $generated_at ) : '<em>never</em>'; ?></td></tr>
			<tr><th>Ingest URL</th><td><code><?php echo $ingest_url; ?></code></td></tr>
			<tr><th>Shortcode</th><td><code>[tour-calendar]</code> — add to any (gated) page.</td></tr>
		</table>

		<h2>Ingest secret</h2>
		<p>Send this as the <code>X-Tour-Secret</code> header. Set it as <code>OUTPUT_WEBSITE_SECRET</code> in the Python job's environment.</p>
		<p><input type="text" class="regular-text" readonly value="<?php echo esc_attr( $secret ); ?>" onclick="this.select()"></p>
		<?php if ( defined( 'TOUR_DATES_SECRET' ) ) : ?>
			<p><em>Pinned by the TOUR_DATES_SECRET constant in wp-config.php.</em></p>
		<?php else : ?>
			<form method="post">
				<?php wp_nonce_field( 'tcal_regen_secret' ); ?>
				<input type="hidden" name="tcal_regen_secret" value="1">
				<?php submit_button( 'Regenerate secret', 'secondary' ); ?>
			</form>
		<?php endif; ?>

		<h2>Manual data paste (fallback)</h2>
		<p>If the weekly push fails, paste the contents of <code>tour_dates.json</code> here.</p>
		<form method="post">
			<?php wp_nonce_field( 'tcal_manual_save' ); ?>
			<textarea name="tcal_manual_json" rows="10" class="large-text code" placeholder='{ "generated_at": "...", "shows": [ ... ] }'></textarea>
			<?php submit_button( 'Save pasted JSON' ); ?>
		</form>
	</div>
	<?php
}
