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

	$fields = array( 'artist', 'date', 'venue', 'city', 'region', 'country', 'ticket_url', 'source', 'raw_id' );
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
