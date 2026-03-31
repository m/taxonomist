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


# Token limit for agent Read tool. Override via TAXONOMIST_MAX_BATCH_TOKENS env var.
# Default 8000 gives headroom under the typical 10K limit. The export agent
# should call probe_read_limit() to discover the actual limit at runtime and
# set this via environment variable before calling write_batches().
MAX_BATCH_TOKENS = int(os.environ.get('TAXONOMIST_MAX_BATCH_TOKENS', '8000'))
CHARS_PER_TOKEN = 4      # Conservative estimate for English text.
MAX_BATCH_CHARS = MAX_BATCH_TOKENS * CHARS_PER_TOKEN




def estimate_post_size(post):
    """Estimate the JSON-serialized size of a post in characters."""
    return len(json.dumps(post))


def calculate_batch_size(posts, max_chars=MAX_BATCH_CHARS):
    """
    Calculate the optimal batch size based on actual post content sizes.

    Samples the posts to estimate average size, then calculates how many
    fit under the token limit. Returns at least 5 and at most 200.

    Args:
        posts: List of post dicts from the export JSON.
        max_chars: Maximum total characters per batch file.

    Returns:
        Recommended batch size as an integer.
    """
    if not posts:
        return 50

    # Sample up to 20 posts to estimate average size.
    sample = posts[:20] if len(posts) >= 20 else posts
    avg_size = sum(estimate_post_size(p) for p in sample) / len(sample)

    # Account for JSON array overhead (brackets, commas).
    batch_size = int(max_chars / (avg_size + 2))

    # Clamp between 5 and 200.
    return max(5, min(200, batch_size))


def split_into_batches(posts, batch_size=None):
    """
    Split a list of posts into batches for parallel analysis.

    If batch_size is not provided, it's calculated automatically based
    on the actual content sizes to stay under the agent Read token limit.

    Args:
        posts: List of post dicts from the export JSON.
        batch_size: Posts per batch. If None, calculated automatically.

    Returns:
        List of lists, where each inner list has up to batch_size posts.
    """
    if batch_size is None:
        batch_size = calculate_batch_size(posts)
    return [posts[i:i + batch_size] for i in range(0, len(posts), batch_size)]


def write_batches(posts, batch_dir, batch_size=None):
    """
    Split posts into batches and write each to a numbered JSON file.

    Creates files like batch-000.json, batch-001.json, etc. If batch_size
    is not provided, it's calculated automatically to stay under the agent
    Read token limit (10K tokens).

    Args:
        posts: List of post dicts from the export JSON.
        batch_dir: Directory to write batch files into. Created if missing.
        batch_size: Posts per batch. If None, calculated from content sizes.

    Returns:
        List of file paths written.
    """
    os.makedirs(batch_dir, exist_ok=True)
    if batch_size is None:
        batch_size = calculate_batch_size(posts)
    batches = split_into_batches(posts, batch_size)
    paths = []
    for i, batch in enumerate(batches):
        path = os.path.join(batch_dir, f'batch-{i:03d}.json')
        with open(path, 'w') as f:
            json.dump(batch, f)
        paths.append(path)
    return paths, batch_size


def check_largest_batch(batch_dir, max_chars=MAX_BATCH_CHARS):
    """
    Check whether the largest batch file fits under the token limit.

    Returns (ok, largest_file, largest_chars). If ok is False, batches
    need to be rewritten with a smaller batch size.
    """
    largest_chars = 0
    largest_file = None
    for f in sorted(os.listdir(batch_dir)):
        if not f.endswith('.json'):
            continue
        path = os.path.join(batch_dir, f)
        size = os.path.getsize(path)
        if size > largest_chars:
            largest_chars = size
            largest_file = f
    return largest_chars <= max_chars, largest_file, largest_chars


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
