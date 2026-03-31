<?php
/**
 * Apply category changes from an AI-generated suggestions file.
 *
 * Reads a JSON file of per-post category suggestions and applies them.
 * The merge strategy is additive: suggested categories are added to
 * existing ones, and only categories listed in TAXONOMIST_REMOVE_CATS
 * are removed. This preserves manually-assigned categories while layering
 * on AI recommendations.
 *
 * Every change is logged to a TSV file with enough detail to undo it.
 * Run in "preview" mode first (the default) to see what would change.
 *
 * Usage:
 *   TAXONOMIST_SUGGESTIONS=/path/to/suggestions.json wp eval-file apply-changes.php
 *
 * Environment variables:
 *   TAXONOMIST_SUGGESTIONS  Path to the suggestions JSON file. Required.
 *                           Format: [{"id": 123, "cats": ["Tech", "AI"]}, ...]
 *   TAXONOMIST_LOG          Path for the change log TSV.
 *                           Default: /tmp/taxonomist-changes.tsv
 *   TAXONOMIST_MODE         "preview" (default) shows what would change.
 *                           "apply" executes the changes.
 *   TAXONOMIST_REMOVE_CATS  Comma-separated category slugs to strip from
 *                           posts that receive new suggestions. Example:
 *                           "asides,uncategorized" removes the catch-all
 *                           categories when a real category is assigned.
 *
 * Log format (TSV):
 *   timestamp  action  post_id  post_title  old_categories  new_categories  cats_added  cats_removed
 *
 * @package Taxonomist
 */

$suggestions_file = getenv( 'TAXONOMIST_SUGGESTIONS' );
$log_file         = getenv( 'TAXONOMIST_LOG' ) ? getenv( 'TAXONOMIST_LOG' ) : '/tmp/taxonomist-changes.tsv';
$apply_mode       = getenv( 'TAXONOMIST_MODE' ) ? getenv( 'TAXONOMIST_MODE' ) : 'preview';
$remove_cats_str  = getenv( 'TAXONOMIST_REMOVE_CATS' ) ? getenv( 'TAXONOMIST_REMOVE_CATS' ) : '';

if ( ! $suggestions_file || ! file_exists( $suggestions_file ) ) {
	WP_CLI::error( 'Set TAXONOMIST_SUGGESTIONS to the suggestions JSON path' );
}

$suggestions = json_decode( file_get_contents( $suggestions_file ), true ); // phpcs:ignore WordPress.WP.AlternativeFunctions.file_get_contents_file_get_contents -- local file
if ( ! $suggestions ) {
	WP_CLI::error( 'Failed to parse suggestions file' );
}

// Build a case-insensitive lookup from category name to term ID.
// This handles minor casing differences between suggestions and WordPress.
$all_cats   = get_terms(
	array(
		'taxonomy'   => 'category',
		'hide_empty' => false,
	)
);
$cat_lookup = array();
foreach ( $all_cats as $t ) {
	$cat_lookup[ strtolower( $t->name ) ] = $t->term_id;
}

// Parse the list of category slugs to strip from posts when new
// categories are assigned (e.g., removing "Asides" once a real
// category is applied).
$remove_slugs = array_filter( array_map( 'trim', explode( ',', $remove_cats_str ) ) );
$remove_ids   = array();
foreach ( $remove_slugs as $slug ) {
	$found_term = get_term_by( 'slug', $slug, 'category' );
	if ( $found_term ) {
		$remove_ids[] = $found_term->term_id;
	}
}

// Open the change log. Every modification is recorded here so the
// changes can be reviewed or reverted later.
// phpcs:disable WordPress.WP.AlternativeFunctions.file_system_operations_fopen
// phpcs:disable WordPress.WP.AlternativeFunctions.file_system_operations_fwrite
// phpcs:disable WordPress.WP.AlternativeFunctions.file_system_operations_fclose
$log = fopen( $log_file, 'w' );
fwrite( $log, "timestamp\taction\tpost_id\tpost_title\told_categories\tnew_categories\tcats_added\tcats_removed\n" );

$changes     = 0;
$skipped     = 0;
$error_count = 0;

foreach ( $suggestions as $suggestion ) {
	$current_post_id = $suggestion['id'];
	$suggested_names = isset( $suggestion['cats'] ) ? $suggestion['cats'] : array();

	// Skip posts with no suggestions — nothing to do.
	if ( empty( $suggested_names ) ) {
		++$skipped;
		continue;
	}

	$current_post = get_post( $current_post_id );
	if ( ! $current_post ) {
		++$error_count;
		continue;
	}

	// Snapshot the current category state for logging.
	$current_ids   = wp_get_post_categories( $current_post_id );
	$current_names = array();
	foreach ( $current_ids as $cid ) {
		$t = get_term( $cid, 'category' );
		if ( $t && ! is_wp_error( $t ) ) {
			$current_names[ $cid ] = $t->name;
		}
	}

	// Keep current categories except those in the remove list.
	// This preserves any manually-assigned categories.
	$kept_ids = array();
	foreach ( $current_ids as $cid ) {
		if ( ! in_array( $cid, $remove_ids, true ) ) {
			$kept_ids[] = $cid;
		}
	}

	// Resolve suggestion names to term IDs via case-insensitive lookup.
	$suggested_ids = array();
	foreach ( $suggested_names as $name ) {
		$key = strtolower( $name );
		if ( isset( $cat_lookup[ $key ] ) ) {
			$suggested_ids[] = $cat_lookup[ $key ];
		}
	}

	// Merge: union of kept existing categories and new suggestions.
	$new_ids = array_values( array_unique( array_merge( array_values( $kept_ids ), $suggested_ids ) ) );
	if ( empty( $new_ids ) ) {
		continue;
	}

	// Skip if nothing actually changed (same categories before and after).
	$sorted_current = $current_ids;
	$sorted_new     = $new_ids;
	sort( $sorted_current );
	sort( $sorted_new );
	if ( $sorted_current === $sorted_new ) {
		++$skipped;
		continue;
	}

	// Calculate the diff for the change log.
	$added_ids   = array_diff( $new_ids, $current_ids );
	$removed_ids = array_diff( $current_ids, $new_ids );

	$added_names = array();
	foreach ( $added_ids as $aid ) {
		$t = get_term( $aid, 'category' );
		if ( $t ) {
			$added_names[] = $t->name;
		}
	}

	$removed_names = array();
	foreach ( $removed_ids as $rid ) {
		$removed_names[] = isset( $current_names[ $rid ] ) ? $current_names[ $rid ] : '?';
	}

	$new_names = array();
	foreach ( $new_ids as $nid ) {
		$t = get_term( $nid, 'category' );
		if ( $t ) {
			$new_names[] = $t->name;
		}
	}

	// Write the change to the log before applying it.
	$ts         = gmdate( 'Y-m-d H:i:s' );
	$post_title = str_replace( "\t", ' ', $current_post->post_title );
	fwrite(
		$log,
		"$ts\tSET_CATS\t$current_post_id\t$post_title\t" .
		implode( '|', array_values( $current_names ) ) . "\t" .
		implode( '|', $new_names ) . "\t" .
		implode( '|', $added_names ) . "\t" .
		implode( '|', $removed_names ) . "\n"
	);

	// Only write to the database in "apply" mode.
	if ( 'apply' === $apply_mode ) {
		wp_set_post_categories( $current_post_id, $new_ids );
	}

	++$changes;
	if ( 0 === $changes % 200 ) {
		WP_CLI::log( "Processed $changes changes..." );
	}
}

fclose( $log );
// phpcs:enable

$verb = ( 'apply' === $apply_mode ) ? 'Applied' : 'Would apply';
WP_CLI::success( "$verb $changes changes. Skipped: $skipped. Errors: $error_count." );
WP_CLI::log( 'Log: ' . $log_file );
