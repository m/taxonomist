<?php
/**
 * Create a complete backup of the current taxonomy state.
 * Run via: wp eval-file backup.php
 * Outputs a JSON file with every post's categories and all term data.
 */

$output_file = getenv('TAXONOMIST_OUTPUT') ?: '/tmp/taxonomist-backup.json';

// Export all category terms
$terms = get_terms(['taxonomy' => 'category', 'hide_empty' => false]);
$term_data = [];
foreach ($terms as $t) {
    $term_data[] = [
        'term_id' => $t->term_id,
        'name' => $t->name,
        'slug' => $t->slug,
        'description' => $t->description,
        'count' => $t->count,
        'parent' => $t->parent,
    ];
}

// Export every post's category assignments
$posts = get_posts([
    'numberposts' => -1,
    'post_status' => 'publish',
    'post_type' => 'post',
]);

$post_cats = [];
foreach ($posts as $p) {
    $cat_ids = wp_get_post_categories($p->ID);
    $cat_slugs = wp_get_post_categories($p->ID, ['fields' => 'slugs']);
    $post_cats[] = [
        'post_id' => $p->ID,
        'post_title' => $p->post_title,
        'category_ids' => $cat_ids,
        'category_slugs' => array_values($cat_slugs),
    ];
}

$backup = [
    'timestamp' => date('Y-m-d H:i:s'),
    'site_url' => get_site_url(),
    'total_posts' => count($post_cats),
    'total_categories' => count($term_data),
    'categories' => $term_data,
    'post_categories' => $post_cats,
];

file_put_contents($output_file, json_encode($backup, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE));
WP_CLI::success("Backup saved to $output_file (" . count($post_cats) . " posts, " . count($term_data) . " categories)");
