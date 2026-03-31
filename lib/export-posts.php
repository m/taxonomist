<?php
/**
 * Export all published posts with full content and categories.
 *
 * Run via: wp eval-file export-posts.php
 * Set TAXONOMIST_OUTPUT env var to control output path.
 *
 * @package Taxonomist
 */

$output_file = getenv( 'TAXONOMIST_OUTPUT' ) ? getenv( 'TAXONOMIST_OUTPUT' ) : '/tmp/taxonomist-export.json';

$fp = fopen( $output_file, 'w' );
fwrite( $fp, "[\n" );

$posts = get_posts(
	array(
		'numberposts' => -1,
		'post_status' => 'publish',
		'post_type'   => 'post',
		'orderby'     => 'ID',
		'order'       => 'ASC',
	)
);

$total = count( $posts );
$i     = 0;

foreach ( $posts as $p ) {
	$cats    = wp_get_post_categories( $p->ID, array( 'fields' => 'names' ) );
	$content = wp_strip_all_tags( $p->post_content );
	$content = preg_replace( '/\s+/', ' ', $content );
	$content = trim( $content );

	$row = wp_json_encode(
		array(
			'id'         => $p->ID,
			'title'      => html_entity_decode( $p->post_title, ENT_QUOTES, 'UTF-8' ),
			'date'       => $p->post_date,
			'content'    => $content,
			'categories' => array_values( $cats ),
			'url'        => get_permalink( $p->ID ),
		)
	);

	fwrite( $fp, $row );
	++$i;
	if ( $i < $total ) {
		fwrite( $fp, ",\n" );
	}

	if ( 0 === $i % 500 ) {
		WP_CLI::log( "Exported $i/$total posts..." );
	}
}

fwrite( $fp, "\n]" );
fclose( $fp );
WP_CLI::success( "Exported $total posts to $output_file" );
