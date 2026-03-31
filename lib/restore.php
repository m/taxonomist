<?php
/**
 * Restore taxonomy state from a backup file.
 *
 * Run via: wp eval-file restore.php
 * Set TAXONOMIST_BACKUP env var to the backup file path.
 *
 * @package Taxonomist
 */

$backup_file = getenv( 'TAXONOMIST_BACKUP' );
if ( ! $backup_file || ! file_exists( $backup_file ) ) {
	WP_CLI::error( 'Set TAXONOMIST_BACKUP env var to the backup file path' );
}

$backup = json_decode( file_get_contents( $backup_file ), true ); // phpcs:ignore WordPress.WP.AlternativeFunctions.file_system_operations_file_get_contents
if ( ! $backup ) {
	WP_CLI::error( 'Failed to parse backup file' );
}

WP_CLI::log( 'Restoring from backup: ' . $backup['timestamp'] );
WP_CLI::log( 'Posts: ' . $backup['total_posts'] . ', Categories: ' . $backup['total_categories'] );

// Step 1: Recreate any missing categories.
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
foreach ( $backup['categories'] as $cat ) {
	if ( ! isset( $existing_slugs[ $cat['slug'] ] ) ) {
		$result = wp_insert_term(
			$cat['name'],
			'category',
			array(
				'slug'        => $cat['slug'],
				'description' => $cat['description'],
				'parent'      => $cat['parent'],
			)
		);
		if ( ! is_wp_error( $result ) ) {
			$slug_to_id[ $cat['slug'] ] = $result['term_id'];
			WP_CLI::log( 'Recreated category: ' . $cat['name'] . ' (was term_id ' . $cat['term_id'] . ')' );
		} else {
			WP_CLI::warning( 'Failed to recreate ' . $cat['name'] . ': ' . $result->get_error_message() );
		}
	}
}

// Step 2: Restore every post's categories.
$restored = 0;
$errors   = 0;
foreach ( $backup['post_categories'] as $pc ) {
	$post_id    = $pc['post_id'];
	$target_ids = array();

	foreach ( $pc['category_slugs'] as $slug ) {
		if ( isset( $slug_to_id[ $slug ] ) ) {
			$target_ids[] = $slug_to_id[ $slug ];
		} else {
			WP_CLI::warning( "Category slug '$slug' not found for post $post_id" );
		}
	}

	if ( ! empty( $target_ids ) ) {
		wp_set_post_categories( $post_id, $target_ids );
		++$restored;
	} else {
		++$errors;
	}

	if ( 0 === $restored % 500 && $restored > 0 ) {
		WP_CLI::log( "Restored $restored posts..." );
	}
}

// Step 3: Recount.
WP_CLI::runcommand( 'term recount category' );

WP_CLI::success( "Restored $restored posts. Errors: $errors" );
