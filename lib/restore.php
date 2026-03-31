<?php
/**
 * Restore taxonomy state from a backup file.
 *
 * Reads a backup JSON created by backup.php and restores the exact
 * category state: recreates any categories that were deleted, then
 * sets every post's categories back to their original assignments.
 *
 * Uses category slugs (not IDs) for matching, since term IDs may change
 * when categories are deleted and recreated.
 *
 * Usage:
 *   TAXONOMIST_BACKUP=/path/to/backup.json wp eval-file restore.php
 *
 * Environment variables:
 *   TAXONOMIST_BACKUP  Path to the backup JSON file created by backup.php.
 *                      Required — the script will exit with an error if unset.
 *
 * @package Taxonomist
 */

$backup_file = getenv( 'TAXONOMIST_BACKUP' );
if ( ! $backup_file || ! file_exists( $backup_file ) ) {
	WP_CLI::error( 'Set TAXONOMIST_BACKUP env var to the backup file path' );
}

$backup = json_decode( file_get_contents( $backup_file ), true ); // phpcs:ignore WordPress.WP.AlternativeFunctions.file_get_contents_file_get_contents -- local file
if ( ! $backup ) {
	WP_CLI::error( 'Failed to parse backup file' );
}

WP_CLI::log( 'Restoring from backup: ' . $backup['timestamp'] );
WP_CLI::log( 'Posts: ' . $backup['total_posts'] . ', Categories: ' . $backup['total_categories'] );

// Step 1: Recreate any categories that were deleted since the backup.
// Build a slug-to-ID map of currently existing categories, then fill in
// gaps from the backup data.
$existing_slugs = array();
$existing_terms = get_terms(
	array(
		'taxonomy'   => 'category',
		'hide_empty' => false,
	)
);
foreach ( $existing_terms as $t ) {
	$existing_slugs[ $t->slug ] = $t->term_id;
}

$slug_to_id = $existing_slugs;
foreach ( $backup['categories'] as $category ) {
	if ( ! isset( $existing_slugs[ $category['slug'] ] ) ) {
		$result = wp_insert_term(
			$category['name'],
			'category',
			array(
				'slug'        => $category['slug'],
				'description' => $category['description'],
				'parent'      => $category['parent'],
			)
		);
		if ( ! is_wp_error( $result ) ) {
			$slug_to_id[ $category['slug'] ] = $result['term_id'];
			WP_CLI::log( 'Recreated category: ' . $category['name'] . ' (was term_id ' . $category['term_id'] . ')' );
		} else {
			WP_CLI::warning( 'Failed to recreate ' . $category['name'] . ': ' . $result->get_error_message() );
		}
	}
}

// Step 2: Restore every post's categories by resolving slugs to current IDs.
$restored    = 0;
$error_count = 0;
foreach ( $backup['post_categories'] as $pc ) {
	$current_post_id = $pc['post_id'];
	$target_ids      = array();

	foreach ( $pc['category_slugs'] as $slug ) {
		if ( isset( $slug_to_id[ $slug ] ) ) {
			$target_ids[] = $slug_to_id[ $slug ];
		} else {
			WP_CLI::warning( "Category slug '$slug' not found for post $current_post_id" );
		}
	}

	if ( ! empty( $target_ids ) ) {
		wp_set_post_categories( $current_post_id, $target_ids );
		++$restored;
	} else {
		++$error_count;
	}

	if ( 0 === $restored % 500 && $restored > 0 ) {
		WP_CLI::log( "Restored $restored posts..." );
	}
}

// Step 3: Recount term usage to fix any stale counts.
WP_CLI::runcommand( 'term recount category' );

WP_CLI::success( "Restored $restored posts. Errors: $error_count" );
