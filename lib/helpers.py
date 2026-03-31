"""
Taxonomist helper functions for local data processing.

These run on the user's machine (not on WordPress) and handle:
- Splitting exported posts into batches for parallel AI analysis
- Aggregating analysis results from multiple agent batches
- Validating data formats (export JSON, suggestion JSON, backup JSON, log TSV)
- Generating summary statistics from analysis results
"""

import json
import os
from collections import Counter


def split_into_batches(posts, batch_size=50):
    """
    Split a list of posts into fixed-size batches for parallel analysis.

    Each batch is small enough for a single AI agent to process in one
    context window, but large enough to minimize overhead from many agents.

    Args:
        posts: List of post dicts from the export JSON.
        batch_size: Posts per batch. Default 200 balances agent context
                    limits against parallelism overhead.

    Returns:
        List of lists, where each inner list has up to batch_size posts.
    """
    return [posts[i:i + batch_size] for i in range(0, len(posts), batch_size)]


def write_batches(posts, batch_dir, batch_size=50):
    """
    Split posts into batches and write each to a numbered JSON file.

    Creates files like batch-000.json, batch-001.json, etc.

    Args:
        posts: List of post dicts from the export JSON.
        batch_dir: Directory to write batch files into. Created if missing.
        batch_size: Posts per batch.

    Returns:
        List of file paths written.
    """
    os.makedirs(batch_dir, exist_ok=True)
    batches = split_into_batches(posts, batch_size)
    paths = []
    for i, batch in enumerate(batches):
        path = os.path.join(batch_dir, f'batch-{i:03d}.json')
        with open(path, 'w') as f:
            json.dump(batch, f)
        paths.append(path)
    return paths


def aggregate_results(results_dir):
    """
    Combine per-batch result files into a single suggestions list.

    Reads all result-NNN.json files from the directory, merges them,
    and computes category frequency statistics.

    Args:
        results_dir: Directory containing result-NNN.json files.

    Returns:
        Tuple of (all_suggestions, category_counts, new_category_counts)
        where suggestions is a list of dicts, and counts are Counters.
    """
    all_suggestions = []
    cat_counts = Counter()
    new_cat_counts = Counter()

    for filename in sorted(os.listdir(results_dir)):
        if not filename.endswith('.json'):
            continue
        with open(os.path.join(results_dir, filename)) as f:
            batch = json.load(f)
            for post in batch:
                all_suggestions.append(post)
                for cat in post.get('cats', []):
                    cat_counts[cat] += 1
                for cat in post.get('new_cats', []):
                    new_cat_counts[cat] += 1

    return all_suggestions, cat_counts, new_cat_counts


def validate_export(posts):
    """
    Validate that an export JSON has the expected structure.

    Checks that each post has required fields with correct types.
    Returns a list of error strings (empty if valid).

    Args:
        posts: Parsed JSON list from the export file.

    Returns:
        List of validation error strings.
    """
    errors = []
    if not isinstance(posts, list):
        return ['Export must be a JSON array']

    required_fields = {
        'post_id': int,
        'title': str,
        'date': str,
        'content': str,
        'categories': list,
    }

    for i, post in enumerate(posts):
        if not isinstance(post, dict):
            errors.append(f'Post at index {i} is not an object')
            continue
        for field, expected_type in required_fields.items():
            if field not in post:
                errors.append(f'Post ID {post.get("post_id", f"index {i}")}: missing "{field}"')
            elif not isinstance(post[field], expected_type):
                errors.append(
                    f'Post ID {post.get("post_id", f"index {i}")}: '
                    f'"{field}" should be {expected_type.__name__}, '
                    f'got {type(post[field]).__name__}'
                )

    return errors


def validate_suggestions(suggestions):
    """
    Validate that a suggestions JSON has the expected structure.

    Each entry must have an integer "post_id" and a list "cats".
    "new_cats" is optional.

    Args:
        suggestions: Parsed JSON list from a result file.

    Returns:
        List of validation error strings.
    """
    errors = []
    if not isinstance(suggestions, list):
        return ['Suggestions must be a JSON array']

    for i, entry in enumerate(suggestions):
        if not isinstance(entry, dict):
            errors.append(f'Entry at index {i} is not an object')
            continue
        if 'post_id' not in entry:
            errors.append(f'Entry at index {i}: missing "post_id"')
        elif not isinstance(entry['post_id'], int):
            errors.append(f'Entry at index {i}: "post_id" must be int')
        if 'cats' not in entry:
            errors.append(f'Post ID {entry.get("post_id", f"index {i}")}: missing "cats"')
        elif not isinstance(entry['cats'], list):
            errors.append(f'Post ID {entry.get("post_id", f"index {i}")}: "cats" must be list')

    return errors


def validate_backup(backup):
    """
    Validate that a backup JSON has the expected structure.

    Checks for required top-level keys and structure of nested data.

    Args:
        backup: Parsed JSON dict from a backup file.

    Returns:
        List of validation error strings.
    """
    errors = []
    if not isinstance(backup, dict):
        return ['Backup must be a JSON object']

    for key in ('timestamp', 'site_url', 'total_posts', 'total_categories', 'categories', 'post_categories'):
        if key not in backup:
            errors.append(f'Missing required key: "{key}"')

    if 'categories' in backup:
        for i, cat in enumerate(backup['categories']):
            for field in ('term_id', 'name', 'slug'):
                if field not in cat:
                    errors.append(f'Category at index {i}: missing "{field}"')

    if 'post_categories' in backup:
        for i, pc in enumerate(backup['post_categories']):
            for field in ('post_id', 'category_slugs'):
                if field not in pc:
                    errors.append(f'Post mapping at index {i}: missing "{field}"')

    return errors


def parse_change_log(log_path):
    """
    Parse a TSV change log file into a list of change dicts.

    Skips the header row. Each dict contains the TSV column values.

    Args:
        log_path: Path to the TSV log file.

    Returns:
        List of dicts with keys: timestamp, action, post_id, post_title,
        old_categories, new_categories, cats_added, cats_removed.
    """
    changes = []
    with open(log_path) as f:
        header = f.readline().strip().split('\t')
        for line in f:
            values = line.strip().split('\t')
            if len(values) >= len(header):
                changes.append(dict(zip(header, values)))
    return changes
