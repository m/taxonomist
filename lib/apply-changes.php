<?php
/**
 * Apply category changes from a suggestions JSON file.
 * Run via: wp eval-file apply-changes.php
 *
 * Environment variables:
 *   TAXONOMIST_SUGGESTIONS - path to suggestions JSON file
 *   TAXONOMIST_LOG - path to write change log (TSV)
 *   TAXONOMIST_MODE - "preview" (default) or "apply"
 *   TAXONOMIST_REMOVE_OLD - "yes" to remove categories not in suggestions (default: "no")
 *   TAXONOMIST_REMOVE_CATS - comma-separated category slugs to remove (e.g., "asides,uncategorized")
 */

$suggestions_file = getenv('TAXONOMIST_SUGGESTIONS');
$log_file = getenv('TAXONOMIST_LOG') ?: '/tmp/taxonomist-changes.tsv';
$mode = getenv('TAXONOMIST_MODE') ?: 'preview';
$remove_cats_str = getenv('TAXONOMIST_REMOVE_CATS') ?: '';

if (!$suggestions_file || !file_exists($suggestions_file)) {
    WP_CLI::error("Set TAXONOMIST_SUGGESTIONS to the suggestions JSON path");
}

$suggestions = json_decode(file_get_contents($suggestions_file), true);
if (!$suggestions) {
    WP_CLI::error("Failed to parse suggestions file");
}

// Build category lookup
$all_cats = get_terms(['taxonomy' => 'category', 'hide_empty' => false]);
$cat_lookup = [];
foreach ($all_cats as $t) {
    $cat_lookup[strtolower($t->name)] = $t->term_id;
}

// Categories to remove from posts
$remove_slugs = array_filter(array_map('trim', explode(',', $remove_cats_str)));
$remove_ids = [];
foreach ($remove_slugs as $slug) {
    $term = get_term_by('slug', $slug, 'category');
    if ($term) $remove_ids[] = $term->term_id;
}

$log = fopen($log_file, 'w');
fwrite($log, "timestamp\taction\tpost_id\tpost_title\told_categories\tnew_categories\tcats_added\tcats_removed\n");

$changes = 0;
$skipped = 0;
$errors = 0;

foreach ($suggestions as $s) {
    $post_id = $s['id'];
    $suggested_names = $s['cats'] ?? [];
    if (empty($suggested_names)) {
        $skipped++;
        continue;
    }

    $post = get_post($post_id);
    if (!$post) {
        $errors++;
        continue;
    }

    $current_ids = wp_get_post_categories($post_id);
    $current_names = [];
    foreach ($current_ids as $cid) {
        $t = get_term($cid, 'category');
        if ($t && !is_wp_error($t)) $current_names[$cid] = $t->name;
    }

    // Keep current categories except those in the remove list
    $kept_ids = array_filter($current_ids, function ($cid) use ($remove_ids) {
        return !in_array($cid, $remove_ids);
    });

    // Resolve suggestions to term IDs
    $suggested_ids = [];
    foreach ($suggested_names as $name) {
        $key = strtolower($name);
        if (isset($cat_lookup[$key])) {
            $suggested_ids[] = $cat_lookup[$key];
        }
    }

    // Union
    $new_ids = array_values(array_unique(array_merge(array_values($kept_ids), $suggested_ids)));
    if (empty($new_ids)) continue;

    // Check if changed
    $sorted_current = $current_ids;
    $sorted_new = $new_ids;
    sort($sorted_current);
    sort($sorted_new);
    if ($sorted_current === $sorted_new) {
        $skipped++;
        continue;
    }

    // Log
    $added = array_diff($new_ids, $current_ids);
    $removed = array_diff($current_ids, $new_ids);
    $added_names = array_map(function ($id) { $t = get_term($id, 'category'); return $t ? $t->name : '?'; }, $added);
    $removed_names = array_map(function ($id) use ($current_names) { return $current_names[$id] ?? '?'; }, $removed);
    $new_names = array_map(function ($id) { $t = get_term($id, 'category'); return $t ? $t->name : '?'; }, $new_ids);

    $ts = date('Y-m-d H:i:s');
    $title = str_replace("\t", ' ', $post->post_title);
    fwrite($log, "$ts\tSET_CATS\t$post_id\t$title\t" .
        implode('|', array_values($current_names)) . "\t" .
        implode('|', $new_names) . "\t" .
        implode('|', $added_names) . "\t" .
        implode('|', $removed_names) . "\n");

    if ($mode === 'apply') {
        wp_set_post_categories($post_id, $new_ids);
    }

    $changes++;
    if ($changes % 200 === 0) {
        WP_CLI::log("Processed $changes changes...");
    }
}

fclose($log);

$verb = $mode === 'apply' ? 'Applied' : 'Would apply';
WP_CLI::success("$verb $changes changes. Skipped: $skipped. Errors: $errors.");
WP_CLI::log("Log: $log_file");
