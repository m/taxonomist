<?php
/**
 * Apply category changes from a suggestions JSON file.
 *
 * Run via: wp eval-file apply-changes.php
 *
 * Environment variables:
 *   TAXONOMIST_SUGGESTIONS - path to suggestions JSON file
 *   TAXONOMIST_LOG         - path to write change log TSV
 *   TAXONOMIST_MODE        - "preview" (default) or "apply"
 *   TAXONOMIST_REMOVE_CATS - comma-separated category slugs to remove (e.g., "asides,uncategorized")
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

// Build category lookup.
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

// Categories to remove from posts.
$remove_slugs = array_filter( array_map( 'trim', explode( ',', $remove_cats_str ) ) );
$remove_ids   = array();
foreach ( $remove_slugs as $slug ) {
	$found_term = get_term_by( 'slug', $slug, 'category' );
	if ( $found_term ) {
		$remove_ids[] = $found_term->term_id;
	}
}

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
	if ( empty( $suggested_names ) ) {
		++$skipped;
		continue;
	}

	$current_post = get_post( $current_post_id );
	if ( ! $current_post ) {
		++$error_count;
		continue;
	}

	$current_ids   = wp_get_post_categories( $current_post_id );
	$current_names = array();
	foreach ( $current_ids as $cid ) {
		$t = get_term( $cid, 'category' );
		if ( $t && ! is_wp_error( $t ) ) {
			$current_names[ $cid ] = $t->name;
		}
	}

	// Keep current categories except those in the remove list.
	$kept_ids = array();
	foreach ( $current_ids as $cid ) {
		if ( ! in_array( $cid, $remove_ids, true ) ) {
			$kept_ids[] = $cid;
		}
	}

	// Resolve suggestions to term IDs.
	$suggested_ids = array();
	foreach ( $suggested_names as $name ) {
		$key = strtolower( $name );
		if ( isset( $cat_lookup[ $key ] ) ) {
			$suggested_ids[] = $cat_lookup[ $key ];
		}
	}

	// Union.
	$new_ids = array_values( array_unique( array_merge( array_values( $kept_ids ), $suggested_ids ) ) );
	if ( empty( $new_ids ) ) {
		continue;
	}

	// Check if changed.
	$sorted_current = $current_ids;
	$sorted_new     = $new_ids;
	sort( $sorted_current );
	sort( $sorted_new );
	if ( $sorted_current === $sorted_new ) {
		++$skipped;
		continue;
	}

	// Build diff for log.
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
