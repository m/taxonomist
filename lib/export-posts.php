<?php
/**
 * Export all published posts with full content and categories to JSON.
 *
 * Streams posts one-by-one to avoid memory issues on large sites.
 * Output is a JSON array where each element contains the post ID, title,
 * publication date, full text content (HTML stripped), assigned category
 * names, and permalink URL.
 *
 * Usage:
 *   wp eval-file export-posts.php
 *
 * Environment variables:
 *   TAXONOMIST_OUTPUT  Path for the output JSON file.
 *                      Default: /tmp/taxonomist-export.json
 *
 * Output format:
 *   [
 *     {
 *       "id": 123,
 *       "title": "Post Title",
 *       "date": "2024-01-15 10:30:00",
 *       "content": "Full post text with HTML stripped...",
 *       "categories": ["WordPress", "Tech"],
 *       "url": "https://example.com/2024/01/post-title/"
 *     },
 *     ...
 *   ]
 *
 * @package Taxonomist
 */

$output_file = getenv( 'TAXONOMIST_OUTPUT' ) ? getenv( 'TAXONOMIST_OUTPUT' ) : '/tmp/taxonomist-export.json';

// Open the output file and begin the JSON array.
// We stream posts individually rather than building the full array in memory,
// which allows this to work on sites with tens of thousands of posts.
// phpcs:disable WordPress.WP.AlternativeFunctions.file_system_operations_fopen
// phpcs:disable WordPress.WP.AlternativeFunctions.file_system_operations_fwrite
// phpcs:disable WordPress.WP.AlternativeFunctions.file_system_operations_fclose
$fp = fopen( $output_file, 'w' );
fwrite( $fp, "[\n" );

// Fetch published posts in chunks to avoid memory exhaustion.
$posts_per_page = 100;
$current_page   = 1;
$total_exported = 0;
$first          = true;

while ( true ) {
	$query_args = array(
		'posts_per_page' => $posts_per_page,
		'paged'          => $current_page,
		'post_status'    => 'publish',
		'post_type'      => 'post',
		'orderby'        => 'ID',
		'order'          => 'ASC',
	);

	$all_posts = get_posts( $query_args );

	if ( empty( $all_posts ) ) {
		break;
	}

	foreach ( $all_posts as $p ) {
		// Comma-separate entries, but not before the first one.
		if ( ! $first ) {
			fwrite( $fp, ",\n" );
		}
		$first = false;

		// Get both category names (for AI analysis readability) and slugs
		// (as stable identifiers that survive renames). The apply script
		// resolves suggestions by slug, not name, to prevent drift.
		$cat_names = wp_get_post_categories( $p->ID, array( 'fields' => 'names' ) );
		$cat_slugs = wp_get_post_categories( $p->ID, array( 'fields' => 'slugs' ) );

		// Strip HTML and collapse whitespace for clean plain-text content.
		// Full content is preserved (not truncated) for accurate AI analysis.
		$content = wp_strip_all_tags( $p->post_content );
		$content = preg_replace( '/\s+/', ' ', $content );
		$content = trim( $content );

		$row = wp_json_encode(
			array(
				'id'             => $p->ID,
				'title'          => html_entity_decode( $p->post_title, ENT_QUOTES, 'UTF-8' ),
				'date'           => $p->post_date,
				'content'        => $content,
				'categories'     => array_values( $cat_names ),
				'category_slugs' => array_values( $cat_slugs ),
				'url'            => get_permalink( $p->ID ),
			)
		);

		fwrite( $fp, $row );
		++$total_exported;

		// Progress reporting every 500 posts.
		if ( 0 === $total_exported % 500 ) {
			WP_CLI::log( "Exported $total_exported posts..." );
		}
	}

	// Crucial: flush WordPress's internal cache after each page to free up memory.
	wp_cache_flush();
	++$current_page;
}

fwrite( $fp, "\n]" );
fclose( $fp );
// phpcs:enable

WP_CLI::success( "Exported $total_exported posts to $output_file" );
