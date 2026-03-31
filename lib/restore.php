<?php
/**
 * Restore taxonomy state from a backup file.
 *
 * Performs a full authoritative restore: the category taxonomy after this
 * script runs will exactly match the backup. This means:
 *
 * 1. Recreate any categories that were deleted since the backup.
 * 2. Update existing categories to match backup values (name, slug, description).
 * 3. Restore parent-child hierarchy using a two-pass approach (parents first).
 * 4. Restore every post's category assignments.
 * 5. Delete any categories that were created after the backup.
 * 6. Restore the default_category setting.
 * 7. Recount term usage.
 *
 * Uses category slugs (not IDs) as the stable identifier, since term IDs
 * may change when categories are deleted and recreated.
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

// Build a set of slugs that should exist after restore.
$backup_slugs = array();
// Map old term_id -> slug for resolving parent references.
$old_id_to_slug = array();
foreach ( $backup['categories'] as $category ) {
	$backup_slugs[ $category['slug'] ]      = $category;
	$old_id_to_slug[ $category['term_id'] ] = $category['slug'];
}

// Step 1: Recreate missing categories and update existing ones.
// First pass: create/update without parents (avoids dependency issues).
$existing_terms   = get_terms(
	array(
		'taxonomy'   => 'category',
		'hide_empty' => false,
	)
);
$existing_by_slug = array();
foreach ( $existing_terms as $t ) {
	$existing_by_slug[ $t->slug ] = $t;
}

$slug_to_id = array();
$created    = 0;
$updated    = 0;

foreach ( $backup['categories'] as $category ) {
	$slug = $category['slug'];

	if ( isset( $existing_by_slug[ $slug ] ) ) {
		// Category exists — update its name, description to match backup.
		$existing     = $existing_by_slug[ $slug ];
		$needs_update = (
			$existing->name !== $category['name'] ||
			$existing->description !== $category['description']
		);
		if ( $needs_update ) {
			wp_update_term(
				$existing->term_id,
				'category',
				array(
					'name'        => $category['name'],
					'description' => $category['description'],
				)
			);
			++$updated;
			WP_CLI::log( 'Updated category: ' . $category['name'] );
		}
		$slug_to_id[ $slug ] = $existing->term_id;
	} else {
		// Category was deleted — recreate it without parent for now.
		// WordPress enforces name uniqueness per parent level. Since we
		// insert at root first (parent=0) and fix hierarchy in Step 2,
		// a name collision can occur if the live site already has a
		// root-level category with the same name. Use a temporary name
		// to guarantee insertion, then fix it when we set the parent.
		$temp_name = $category['name'];
		$result    = wp_insert_term(
			$temp_name,
			'category',
			array(
				'slug'        => $category['slug'],
				'description' => $category['description'],
			)
		);
		if ( is_wp_error( $result ) && 'term_exists' === $result->get_error_code() ) {
			// Name collision at root level — use a temporary unique name.
			$temp_name = $category['name'] . '-taxonomist-' . uniqid();
			$result    = wp_insert_term(
				$temp_name,
				'category',
				array(
					'slug'        => $category['slug'] . '-taxonomist-' . uniqid(),
					'description' => $category['description'],
				)
			);
		}
		if ( ! is_wp_error( $result ) ) {
			$slug_to_id[ $slug ] = $result['term_id'];
			++$created;
			WP_CLI::log( 'Recreated category: ' . $category['name'] );
		} else {
			WP_CLI::warning( 'Failed to recreate ' . $category['name'] . ': ' . $result->get_error_message() );
		}
	}
}

// Step 2: Restore parent-child hierarchy.
// Now that all terms exist, set parents using the old_id -> slug -> new_id mapping.
$hierarchy_fixed = 0;
foreach ( $backup['categories'] as $category ) {
	$slug       = $category['slug'];
	$old_parent = $category['parent'];

	if ( ! isset( $slug_to_id[ $slug ] ) ) {
		continue;
	}

	$current_id       = $slug_to_id[ $slug ];
	$target_parent_id = 0;

	if ( $old_parent > 0 && isset( $old_id_to_slug[ $old_parent ] ) ) {
		$parent_slug = $old_id_to_slug[ $old_parent ];
		if ( isset( $slug_to_id[ $parent_slug ] ) ) {
			$target_parent_id = $slug_to_id[ $parent_slug ];
		} else {
			WP_CLI::warning( "Parent slug '$parent_slug' not found for category '$slug'" );
		}
	}

	// Update parent, and also fix name/slug back to backup values.
	// Step 1 may have used a temporary name to avoid root-level collisions
	// — now that the parent is correct, the real name is safe to restore.
	$current_term = get_term( $current_id, 'category' );
	if ( ! $current_term ) {
		continue;
	}

	$needs_fix = (
		(int) $current_term->parent !== $target_parent_id ||
		$current_term->name !== $category['name'] ||
		$current_term->slug !== $category['slug']
	);
	if ( $needs_fix ) {
		wp_update_term(
			$current_id,
			'category',
			array(
				'parent' => $target_parent_id,
				'name'   => $category['name'],
				'slug'   => $category['slug'],
			)
		);
		++$hierarchy_fixed;
	}
}

if ( $hierarchy_fixed > 0 ) {
	WP_CLI::log( "Fixed $hierarchy_fixed parent-child relationships." );
}

// Step 3: Restore every post's categories.
$restored    = 0;
$error_count = 0;
foreach ( $backup['post_categories'] as $pc ) {
	$current_post_id = $pc['post_id'];
	$target_ids      = array();

	foreach ( $pc['category_slugs'] as $slug ) {
		if ( isset( $slug_to_id[ $slug ] ) ) {
			$target_ids[] = $slug_to_id[ $slug ];
		} else {
			WP_CLI::warning( "Category slug '$slug' not found for post ID $current_post_id" );
			++$error_count;
		}
	}

	$current_post = get_post( $current_post_id );
	if ( ! $current_post ) {
		WP_CLI::warning( 'Post ID ' . $current_post_id . ' no longer exists' );
		++$error_count;
		continue;
	}

	$set_result = wp_set_post_categories( $current_post_id, $target_ids );
	if ( is_wp_error( $set_result ) || false === $set_result ) {
		WP_CLI::warning( 'Failed to restore categories for post ID ' . $current_post_id );
		++$error_count;
		continue;
	}
	++$restored;

	if ( 0 === $restored % 500 && $restored > 0 ) {
		WP_CLI::log( "Restored $restored posts..." );
		wp_cache_flush();
	}
}

// Step 4: Delete categories that were created after the backup.
// Refresh the term list and remove any slug not in the backup.
$post_restore_terms = get_terms(
	array(
		'taxonomy'   => 'category',
		'hide_empty' => false,
	)
);
$deleted            = 0;
foreach ( $post_restore_terms as $t ) {
	if ( ! isset( $backup_slugs[ $t->slug ] ) ) {
		// Don't delete the default category — change it first if needed.
		$default_cat = (int) get_option( 'default_category' );
		if ( $t->term_id === $default_cat ) {
			// Find a backup category to use as default instead.
			$first_backup_slug = array_key_first( $backup_slugs );
			if ( $first_backup_slug && isset( $slug_to_id[ $first_backup_slug ] ) ) {
				update_option( 'default_category', $slug_to_id[ $first_backup_slug ] );
			}
		}
		wp_delete_term( $t->term_id, 'category' );
		WP_CLI::log( 'Deleted post-backup category: ' . $t->name );
		++$deleted;
	}
}

// Step 5: Restore the default_category setting.
// The backup stores the old term_id — resolve via slug.
if ( isset( $backup['default_category_slug'] ) ) {
	$default_slug = $backup['default_category_slug'];
	if ( isset( $slug_to_id[ $default_slug ] ) ) {
		update_option( 'default_category', $slug_to_id[ $default_slug ] );
		WP_CLI::log( 'Restored default category: ' . $default_slug );
	}
}

// Step 6: Recount term usage to fix any stale counts.
WP_CLI::runcommand( 'term recount category' );

WP_CLI::success(
	"Restored $restored posts. " .
	"Created $created categories, updated $updated, deleted $deleted. " .
	"Errors: $error_count."
);
