<?php
/**
 * Export all published posts with full content and categories.
 * Run via: wp eval-file export-posts.php
 * Writes to stdout as JSON.
 */

$output_file = getenv('TAXONOMIST_OUTPUT') ?: '/tmp/taxonomist-export.json';

$fp = fopen($output_file, 'w');
fwrite($fp, "[\n");

$posts = get_posts([
    'numberposts' => -1,
    'post_status' => 'publish',
    'post_type' => 'post',
    'orderby' => 'ID',
    'order' => 'ASC',
]);

$total = count($posts);
$i = 0;

foreach ($posts as $p) {
    $cats = wp_get_post_categories($p->ID, ['fields' => 'names']);
    $content = strip_tags($p->post_content);
    $content = preg_replace('/\s+/', ' ', $content);
    $content = trim($content);

    $row = json_encode([
        'id' => $p->ID,
        'title' => html_entity_decode($p->post_title, ENT_QUOTES, 'UTF-8'),
        'date' => $p->post_date,
        'content' => $content,
        'categories' => array_values($cats),
        'url' => get_permalink($p->ID),
    ], JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);

    fwrite($fp, $row);
    $i++;
    if ($i < $total) fwrite($fp, ",\n");

    if ($i % 500 === 0) {
        WP_CLI::log("Exported $i/$total posts...");
    }
}

fwrite($fp, "\n]");
fclose($fp);
WP_CLI::success("Exported $total posts to $output_file");
