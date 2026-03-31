<?php
/**
 * Create a complete backup of the current taxonomy state.
 *
 * Captures two things: (1) every category term with its metadata, and
 * (2) the exact category assignments for every published post. Together
 * these allow a full restore to the pre-change state, including recreating
 * deleted categories and reassigning every post.
 *
 * Usage:
 *   wp eval-file backup.php
 *
 * Environment variables:
 *   TAXONOMIST_OUTPUT  Path for the backup JSON file.
 *                      Default: /tmp/taxonomist-backup.json
 *
 * Output format:
 *   {
 *     "timestamp": "2024-01-15 10:30:00",
 *     "site_url": "https://example.com",
 *     "total_posts": 5672,
 *     "total_categories": 64,
 *     "categories": [
 *       {"term_id": 1, "name": "...", "slug": "...", "description": "...", "count": 42, "parent": 0}
 *     ],
 *     "post_categories": [
 *       {"post_id": 123, "post_title": "...", "category_ids": [1, 5], "category_slugs": ["tech", "ai"]}
 *     ]
 *   }
 *
 * The restore script (restore.php) reads this format to undo all changes.
 * Category slugs are stored alongside IDs because term IDs may differ if
 * categories are deleted and recreated.
 *
 * @package Taxonomist
 */

$output_file = getenv( 'TAXONOMIST_OUTPUT' ) ? getenv( 'TAXONOMIST_OUTPUT' ) : '/tmp/taxonomist-backup.json';

// Export all category terms, including empty ones that might be targets
// for future assignment.
$terms     = get_terms(
	array(
		'taxonomy'   => 'category',
		'hide_empty' => false,
	)
);
$term_data = array();
foreach ( $terms as $t ) {
	$term_data[] = array(
		'term_id'     => $t->term_id,
		'name'        => $t->name,
		'slug'        => $t->slug,
		'description' => $t->description,
		'count'       => $t->count,
		'parent'      => $t->parent,
	);
}

// Export every published post's category assignments.
// Uses keyset pagination (ID > last_id) instead of offset-based paging
// to prevent data loss if posts change during the backup.
$post_cats  = array();
$batch_size = 200;
$last_id    = 0;

while ( true ) {
	$query_args = array(
		'posts_per_page'   => $batch_size,
		'post_status'      => 'publish',
		'post_type'        => 'post',
		'orderby'          => 'ID',
		'order'            => 'ASC',
		'suppress_filters' => false,
	);

	if ( $last_id > 0 ) {
		add_filter(
			'posts_where',
			$keyset_filter = function ( $where ) use ( $last_id ) {
				global $wpdb;
				return $where . $wpdb->prepare( " AND {$wpdb->posts}.ID > %d", $last_id );
			}
		);
	}

	$all_posts = get_posts( $query_args );

	if ( isset( $keyset_filter ) ) {
		remove_filter( 'posts_where', $keyset_filter );
		unset( $keyset_filter );
	}

	if ( empty( $all_posts ) ) {
		break;
	}

	foreach ( $all_posts as $p ) {
		$cat_ids   = wp_get_post_categories( $p->ID );
		$cat_slugs = wp_get_post_categories( $p->ID, array( 'fields' => 'slugs' ) );

		$post_cats[] = array(
			'post_id'        => $p->ID,
			'post_title'     => $p->post_title,
			'category_ids'   => $cat_ids,
			'category_slugs' => array_values( $cat_slugs ),
		);
	}

	$last_id = $all_posts[ count( $all_posts ) - 1 ]->ID;
	wp_cache_flush();
}

// Capture the default category setting so restore can reset it.
$default_cat_id   = (int) get_option( 'default_category' );
$default_cat_term = get_term( $default_cat_id, 'category' );
$default_cat_slug = $default_cat_term ? $default_cat_term->slug : '';

$backup = array(
	'timestamp'             => gmdate( 'Y-m-d H:i:s' ),
	'site_url'              => get_site_url(),
	'total_posts'           => count( $post_cats ),
	'total_categories'      => count( $term_data ),
	'default_category_slug' => $default_cat_slug,
	'categories'            => $term_data,
	'post_categories'       => $post_cats,
);

$backup_json = wp_json_encode( $backup, JSON_PRETTY_PRINT );
if ( false === $backup_json ) {
	WP_CLI::error( 'Failed to JSON-encode backup payload' );
}
$bytes_written = file_put_contents( $output_file, $backup_json ); // phpcs:ignore WordPress.WP.AlternativeFunctions.file_system_operations_file_put_contents
if ( false === $bytes_written ) {
	WP_CLI::error( 'Failed to write backup file: ' . $output_file );
}
WP_CLI::success( 'Backup saved to ' . $output_file . ' (' . count( $post_cats ) . ' posts, ' . count( $term_data ) . ' categories)' );
