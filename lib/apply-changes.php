<?php
/**
 * Apply category or tag changes from an AI-generated suggestions file.
 *
 * Reads a JSON file of per-post category suggestions and applies them.
 * Categories are resolved by **term ID** to prevent drift between the
 * export/analysis and apply phases.
 *
 * The merge strategy is additive: suggested categories are added to
 * existing ones, and only categories listed in TAXONOMIST_REMOVE_CATS
 * are removed. This preserves manually-assigned categories while layering
 * on AI recommendations.
 *
 * With TAXONOMIST_TAXONOMY=post_tag the script applies tag suggestions
 * instead (agents/tags.md). Tag mode differs from category mode in three
 * ways, matching WpcomAdapter.set_post_tags():
 *   - Each suggestion's "tags" list is the post's complete new tag set, not
 *     an addition. Tags not in the list are removed, which is how merges
 *     and retires take effect. TAXONOMIST_REMOVE_CATS is not used.
 *   - An empty "tags" list clears the post's tags. Tags have no default
 *     term, so a post with no tags is a normal end state. Because of this,
 *     an entry with no "tags" key is rejected rather than skipped.
 *   - There is no default-term safety check.
 * Tag IDs are passed to WordPress as integers, which it resolves by ID, so
 * the name-to-slug collision that causes `tags_drift` on WordPress.com
 * cannot happen here.
 *
 * Every change is logged to a TSV file with enough detail to undo it.
 * Run in "preview" mode first (the default) to see what would change.
 *
 * Usage:
 *   TAXONOMIST_SUGGESTIONS=/path/to/suggestions.json wp eval-file apply-changes.php
 *
 * Environment variables:
 *   TAXONOMIST_SUGGESTIONS  Path to the suggestions JSON file. Required.
 *                           Format: [{"post_id": 123, "cats": [4, 9]}, ...]
 *                           (tag mode: [{"post_id": 123, "tags": [31]}, ...])
 *                           Values in "cats" / "tags" must be integer term
 *                           IDs — digit-strings are rejected so the
 *                           apply step catches type-confusion bugs rather
 *                           than silently coercing them.
 *   TAXONOMIST_TAXONOMY     "category" (default) or "post_tag".
 *   TAXONOMIST_LOG          Path for the change log TSV.
 *                           Default: /tmp/taxonomist-changes.tsv
 *   TAXONOMIST_MODE         "preview" (default) shows what would change.
 *                           "apply" executes the changes.
 *   TAXONOMIST_REMOVE_CATS  Comma-separated category term IDs to strip from
 *                           posts that receive new suggestions. Example:
 *                           "17,23" removes catch-all categories when a
 *                           real category is assigned. Category mode only.
 *
 * Log format (TSV), category mode:
 *   timestamp  action  post_id  post_title  old_categories  new_categories  cats_added  cats_removed
 *
 * Log format (TSV), tag mode (same columns as WpcomAdapter.TAG_LOG_HEADER):
 *   timestamp  action  post_id  post_title  old_tags  new_tags  tags_added  tags_removed  old_tag_slugs  new_tag_slugs
 *
 * @package Taxonomist
 */

$suggestions_file = getenv( 'TAXONOMIST_SUGGESTIONS' );
$log_file         = getenv( 'TAXONOMIST_LOG' ) ? getenv( 'TAXONOMIST_LOG' ) : '/tmp/taxonomist-changes.tsv';
$apply_mode       = getenv( 'TAXONOMIST_MODE' ) ? getenv( 'TAXONOMIST_MODE' ) : 'preview';
$remove_cats_str  = getenv( 'TAXONOMIST_REMOVE_CATS' ) ? getenv( 'TAXONOMIST_REMOVE_CATS' ) : '';
$target_taxonomy  = getenv( 'TAXONOMIST_TAXONOMY' ) ? getenv( 'TAXONOMIST_TAXONOMY' ) : 'category';

if ( ! in_array( $target_taxonomy, array( 'category', 'post_tag' ), true ) ) {
	WP_CLI::error( 'TAXONOMIST_TAXONOMY must be "category" or "post_tag", got: ' . $target_taxonomy );
}
$is_tag_mode = ( 'post_tag' === $target_taxonomy );
$refs_key    = $is_tag_mode ? 'tags' : 'cats';
$term_label  = $is_tag_mode ? 'tag' : 'category';
$term_plural = $is_tag_mode ? 'tags' : 'categories';

if ( $is_tag_mode && '' !== $remove_cats_str ) {
	WP_CLI::error( 'TAXONOMIST_REMOVE_CATS is not used in tag mode. Leave a tag out of a post\'s "tags" list to remove it.' );
}

if ( ! $suggestions_file || ! file_exists( $suggestions_file ) ) {
	WP_CLI::error( 'Set TAXONOMIST_SUGGESTIONS to the suggestions JSON path' );
}

$suggestions_json = file_get_contents( $suggestions_file ); // phpcs:ignore WordPress.WP.AlternativeFunctions.file_get_contents_file_get_contents -- local file
if ( false === $suggestions_json ) {
	WP_CLI::error( 'Failed to read suggestions file' );
}
$suggestions = json_decode( $suggestions_json, true );
if ( null === $suggestions && JSON_ERROR_NONE !== json_last_error() ) {
	WP_CLI::error( 'Failed to parse suggestions file' );
}
if ( ! is_array( $suggestions ) ) {
	WP_CLI::error( 'Suggestions file must decode to a JSON array' );
}

// Build live term lookups. Term IDs are the canonical identifier for
// analysis/apply, while names are used only for logs.
$all_terms = get_terms(
	array(
		'taxonomy'   => $target_taxonomy,
		'hide_empty' => false,
	)
);
if ( is_wp_error( $all_terms ) ) {
	WP_CLI::error( 'Failed to list ' . $target_taxonomy . ' terms: ' . $all_terms->get_error_message() );
}
$id_to_name = array();
foreach ( $all_terms as $t ) {
	$id_to_name[ $t->term_id ] = $t->name;
}

// Parse the list of category IDs to strip from posts.
$remove_refs        = array_filter( array_map( 'trim', explode( ',', $remove_cats_str ) ), 'strlen' );
$remove_ids         = array();
$invalid_remove_ids = array();
foreach ( $remove_refs as $ref ) {
	if ( ctype_digit( $ref ) ) {
		$remove_ids[] = (int) $ref;
	} else {
		$invalid_remove_ids[] = $ref;
	}
}

if ( ! empty( $invalid_remove_ids ) ) {
	WP_CLI::error(
		'TAXONOMIST_REMOVE_CATS must contain category term IDs, got: ' .
		implode( ', ', $invalid_remove_ids )
	);
}

$unknown_remove_ids = array();
foreach ( $remove_ids as $remove_id ) {
	if ( ! isset( $id_to_name[ $remove_id ] ) ) {
		$unknown_remove_ids[] = $remove_id;
	}
}
if ( ! empty( $unknown_remove_ids ) ) {
	WP_CLI::error(
		'TAXONOMIST_REMOVE_CATS includes IDs that do not exist in the live taxonomy: ' .
		implode( ', ', $unknown_remove_ids )
	);
}

// Safety check: refuse to strip the default category. If the user wants to
// retire the default, they must change the default_category setting first.
// Tags have no default term, and $remove_ids is always empty in tag mode.
$default_cat_id = (int) get_option( 'default_category' );
if ( ! $is_tag_mode && in_array( $default_cat_id, $remove_ids, true ) ) {
	$default_term = get_term( $default_cat_id, 'category' );
	$default_name = $default_term ? $default_term->name : "ID $default_cat_id";
	WP_CLI::error(
		"TAXONOMIST_REMOVE_CATS includes '$default_name' which is the site's default category. " .
		'Change the default category setting first (wp option update default_category NEW_ID), then retry.'
	);
}

// Pre-flight check: verify all suggested term IDs exist in the live
// taxonomy and reject non-integer references. Strict integer typing
// matches validate_suggestions() / validate_tag_suggestions() in
// lib/helpers.py — a digit-string like "42" is treated as a
// type-confusion bug, not a lenient alias.
// Abort early if there are unresolved references — this prevents silent
// data loss from taxonomy drift between export and apply.
$unresolved   = array();
$invalid_refs = array();
$missing_tags = array();
foreach ( $suggestions as $index => $suggestion ) {
	// In tag mode a missing or non-list "tags" would clear the post's tags,
	// so treat it as a malformed entry instead of an empty list.
	if ( $is_tag_mode && ( ! isset( $suggestion['tags'] ) || ! is_array( $suggestion['tags'] ) ) ) {
		$missing_tags[] = isset( $suggestion['post_id'] ) ? 'post ' . wp_json_encode( $suggestion['post_id'] ) : 'entry ' . $index;
		continue;
	}
	$suggested_refs = isset( $suggestion[ $refs_key ] ) ? $suggestion[ $refs_key ] : array();
	foreach ( $suggested_refs as $ref ) {
		if ( ! is_int( $ref ) ) {
			$invalid_refs[] = wp_json_encode( $ref );
			continue;
		}
		if ( ! isset( $id_to_name[ $ref ] ) ) {
			$unresolved[ $ref ] = true;
		}
	}
}
if ( ! empty( $missing_tags ) ) {
	WP_CLI::error(
		'Tag mode needs a "tags" list on every suggestion (use [] to clear a post\'s tags). Missing or not a list for: ' .
		implode( ', ', $missing_tags )
	);
}
if ( ! empty( $invalid_refs ) ) {
	WP_CLI::error(
		"Suggestions must use integer $term_label term IDs in \"$refs_key\". Invalid values: " .
		implode( ', ', array_unique( $invalid_refs ) )
	);
}
if ( ! empty( $unresolved ) ) {
	$list = implode( ', ', array_keys( $unresolved ) );
	WP_CLI::error(
		"Taxonomy drift detected: these $term_label IDs from the suggestions do not exist in the live site: $list. " .
		"Re-export and re-analyze, or create the missing $term_plural first."
	);
}

// Open the change log.
// phpcs:disable WordPress.WP.AlternativeFunctions.file_system_operations_fopen
// phpcs:disable WordPress.WP.AlternativeFunctions.file_system_operations_fclose
$log = fopen( $log_file, 'w' );
if ( false === $log ) {
	WP_CLI::error( 'Failed to open log file for writing: ' . $log_file );
}
// Use fputcsv for secure TSV logging (handles internal tabs/newlines).
if ( $is_tag_mode ) {
	$log_header = array( 'timestamp', 'action', 'post_id', 'post_title', 'old_tags', 'new_tags', 'tags_added', 'tags_removed', 'old_tag_slugs', 'new_tag_slugs' );
} else {
	$log_header = array( 'timestamp', 'action', 'post_id', 'post_title', 'old_categories', 'new_categories', 'cats_added', 'cats_removed' );
}
$header_written = fputcsv( $log, $log_header, "\t" );
if ( false === $header_written ) {
	fclose( $log ); // phpcs:ignore WordPress.WP.AlternativeFunctions.file_system_operations_fclose
	WP_CLI::error( 'Failed to write log header to ' . $log_file );
}

$changes     = 0;
$skipped     = 0;
$error_count = 0;

foreach ( $suggestions as $suggestion ) {
	$current_post_id = isset( $suggestion['post_id'] ) ? $suggestion['post_id'] : $suggestion['id'];
	$suggested_refs  = isset( $suggestion[ $refs_key ] ) ? $suggestion[ $refs_key ] : array();

	// An empty category list means "no suggestion". An empty tag list is a
	// request to clear the post's tags, so it falls through.
	if ( ! $is_tag_mode && empty( $suggested_refs ) ) {
		++$skipped;
		continue;
	}

	$current_post = get_post( $current_post_id );
	if ( ! $current_post ) {
		++$error_count;
		WP_CLI::warning( 'Post not found: ID ' . $current_post_id );
		continue;
	}

	// Snapshot current state for logging.
	if ( $is_tag_mode ) {
		$current_ids = wp_get_post_tags( $current_post_id, array( 'fields' => 'ids' ) );
		if ( is_wp_error( $current_ids ) ) {
			++$error_count;
			WP_CLI::warning( 'Failed to read tags for post ID ' . $current_post_id );
			continue;
		}
		$current_ids = array_map( 'intval', $current_ids );
	} else {
		$current_ids = wp_get_post_categories( $current_post_id );
	}
	$current_names = array();
	$current_slugs = array();
	foreach ( $current_ids as $cid ) {
		$t = get_term( $cid, $target_taxonomy );
		if ( $t && ! is_wp_error( $t ) ) {
			$current_names[ $cid ] = $t->name;
			$current_slugs[ $cid ] = $t->slug;
		}
	}

	// Suggestion refs are already guaranteed to be integers by the
	// pre-flight validation above, so they can be used as term IDs directly.
	$suggested_ids = $suggested_refs;

	if ( $is_tag_mode ) {
		// Replace: the suggestion is the post's complete tag set.
		$new_ids = array_values( array_unique( $suggested_ids ) );
	} else {
		// Keep current categories except those in the remove list.
		$kept_ids = array();
		foreach ( $current_ids as $cid ) {
			if ( ! in_array( $cid, $remove_ids, true ) ) {
				$kept_ids[] = $cid;
			}
		}

		// Merge: union of kept existing + new suggestions.
		// If empty (all categories removed and no suggestions), WordPress will
		// assign the default category — this is correct native behavior.
		$new_ids = array_values( array_unique( array_merge( array_values( $kept_ids ), $suggested_ids ) ) );
	}

	// Skip if nothing changed.
	$sorted_current = $current_ids;
	$sorted_new     = $new_ids;
	sort( $sorted_current );
	sort( $sorted_new );
	if ( $sorted_current === $sorted_new ) {
		++$skipped;
		continue;
	}

	// Calculate diff for the log.
	$added_ids   = array_diff( $new_ids, $current_ids );
	$removed_ids = array_diff( $current_ids, $new_ids );

	$added_names = array();
	foreach ( $added_ids as $aid ) {
		$t = get_term( $aid, $target_taxonomy );
		if ( $t ) {
			$added_names[] = $t->name;
		}
	}

	$removed_names = array();
	foreach ( $removed_ids as $rid ) {
		$removed_names[] = isset( $current_names[ $rid ] ) ? $current_names[ $rid ] : '?';
	}

	$new_names = array();
	$new_slugs = array();
	foreach ( $new_ids as $nid ) {
		$t = get_term( $nid, $target_taxonomy );
		if ( $t ) {
			$new_names[] = $t->name;
			$new_slugs[] = $t->slug;
		}
	}

	$ts      = gmdate( 'Y-m-d H:i:s' );
	$log_row = array(
		$ts,
		$is_tag_mode ? 'SET_TAGS' : 'SET_CATS',
		$current_post_id,
		$current_post->post_title,
		implode( '|', array_values( $current_names ) ),
		implode( '|', $new_names ),
		implode( '|', $added_names ),
		implode( '|', $removed_names ),
	);
	if ( $is_tag_mode ) {
		$log_row[] = implode( '|', array_values( $current_slugs ) );
		$log_row[] = implode( '|', $new_slugs );
	}

	if ( 'apply' === $apply_mode ) {
		if ( $is_tag_mode ) {
			// Integer IDs are matched by term ID, never by name or slug, and
			// an empty list removes every tag from the post.
			$set_result = wp_set_post_terms( $current_post_id, $new_ids, 'post_tag', false );
		} else {
			$set_result = wp_set_post_categories( $current_post_id, $new_ids );
		}
		if ( is_wp_error( $set_result ) || false === $set_result ) {
			++$error_count;
			WP_CLI::warning( 'Failed to set ' . $term_plural . ' for post ID ' . $current_post_id );
			continue;
		}
	}

	$log_result = fputcsv( $log, $log_row, "\t" );
	if ( false === $log_result ) {
		fclose( $log ); // phpcs:ignore WordPress.WP.AlternativeFunctions.file_system_operations_fclose
		WP_CLI::error( 'Failed to write change log row for post ID ' . $current_post_id );
	}

	++$changes;
	if ( 0 === $changes % 200 ) {
		WP_CLI::log( "Processed $changes changes..." );
		wp_cache_flush();
	}
}

fclose( $log );
// phpcs:enable

$verb    = ( 'apply' === $apply_mode ) ? 'Applied' : 'Would apply';
$summary = "$verb $changes changes. Skipped: $skipped. Errors: $error_count.";
WP_CLI::log( 'Log: ' . $log_file );

// A failed post keeps its old tags and gets no log row, so deleting a retired
// tag after this run would lose the assignment. Exit non-zero in tag mode so
// the run can't pass for a success.
if ( $is_tag_mode && $error_count > 0 ) {
	WP_CLI::error( "$summary Do not delete any retired tags until the failed posts are fixed." );
}
WP_CLI::success( $summary );
