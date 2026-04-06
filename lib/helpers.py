"""
Taxonomist helper functions for local data processing.

These run on the user's machine (not on WordPress) and handle:
- Splitting exported posts into batches for parallel AI analysis
- Aggregating analysis results from multiple agent batches
- Validating data formats (export JSON, suggestion JSON, backup JSON, log TSV)
- Generating summary statistics from analysis results
"""

import csv
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




def wp_urlencode(params):
    """
    Encode parameters for the WordPress API (form-encoded).

    Handles nested dictionaries and lists by flattening them into the
    'key[subkey][]' format expected by PHP/WordPress. Use this to avoid the
    'stringified list' bug (issue #2) where urllib.parse.urlencode would
    serialize a list value as its Python repr (e.g. "['a', 'b']") and
    WordPress would interpret it as a single literal value.

    The returned string has square brackets URL-encoded as %5B / %5D —
    that is what urllib emits and what goes on the wire. PHP/WordPress
    decodes them transparently, so

        wp_urlencode({"terms": {"kb_category": ["General", "Settings"]}})

    returns

        "terms%5Bkb_category%5D%5B%5D=General&terms%5Bkb_category%5D%5B%5D=Settings"

    which PHP parses as terms[kb_category] = ["General", "Settings"].

    Args:
        params: Dictionary of parameters (can be nested with dicts and
            lists). Scalar leaf values are passed through ``str()``, so
            callers should convert booleans/None to the string form they
            want WordPress to receive.

    Returns:
        URL-encoded query string.

    Raises:
        TypeError: If ``params`` is not a dict. Passing a list, tuple, or
            scalar would silently produce malformed output (e.g. a
            top-level list would emit ``[]=value`` with no key), so the
            boundary is enforced explicitly.
    """
    import urllib.parse
    if not isinstance(params, dict):
        raise TypeError(
            f'wp_urlencode expects a dict, got {type(params).__name__}'
        )
    flattened = []

    def flatten(obj, prefix=''):
        if isinstance(obj, dict):
            for k, v in obj.items():
                new_prefix = f'{prefix}[{k}]' if prefix else k
                flatten(v, new_prefix)
        elif isinstance(obj, list):
            for v in obj:
                # Recurse for list items to handle nested structures (key[][subkey]).
                flatten(v, f'{prefix}[]')
        else:
            flattened.append((prefix, str(obj)))

    flatten(params)
    return urllib.parse.urlencode(flattened)


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

    Before writing, stale batch-NNN.json files are removed so reruns don't
    leave behind extra batches from a previous export.

    Args:
        posts: List of post dicts from the export JSON.
        batch_dir: Directory to write batch files into. Created if missing.
        batch_size: Posts per batch. If None, calculated from content sizes.

    Returns:
        Tuple of (paths_written, batch_size_used).
    """
    os.makedirs(batch_dir, exist_ok=True)
    for filename in os.listdir(batch_dir):
        if filename.startswith('batch-') and filename.endswith('.json'):
            os.remove(os.path.join(batch_dir, filename))

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

    Reads result-NNN.json files from the directory, de-duplicates by post_id,
    and computes category frequency statistics from the final suggestion set.
    If the same post_id appears multiple times, the last file in sorted order
    wins so targeted reruns can replace stale earlier results.

    Args:
        results_dir: Directory containing result-NNN.json files.

    Returns:
        Tuple of (all_suggestions, category_counts, new_category_counts)
        where suggestions is a list of dicts, and counts are Counters.
    """
    suggestions_by_post_id = {}
    unkeyed_suggestions = []

    for filename in sorted(os.listdir(results_dir)):
        if not (filename.startswith('result-') and filename.endswith('.json')):
            continue
        with open(os.path.join(results_dir, filename)) as f:
            batch = json.load(f)
            for post in batch:
                post_id = post.get('post_id')
                if isinstance(post_id, int):
                    suggestions_by_post_id[post_id] = post
                else:
                    unkeyed_suggestions.append(post)

    all_suggestions = list(suggestions_by_post_id.values()) + unkeyed_suggestions
    cat_counts = Counter()
    new_cat_counts = Counter()
    for post in all_suggestions:
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
        'category_ids': list,
        'category_slugs': list,
        'url': str,
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

        if isinstance(post.get('category_ids'), list) and any(type(value) is not int for value in post['category_ids']):
            errors.append(
                f'Post ID {post.get("post_id", f"index {i}")}: '
                '"category_ids" must contain only ints'
            )

        for field in ('categories', 'category_slugs'):
            if isinstance(post.get(field), list) and any(not isinstance(value, str) for value in post[field]):
                errors.append(
                    f'Post ID {post.get("post_id", f"index {i}")}: '
                    f'"{field}" must contain only strings'
                )

    return errors


def validate_suggestions(suggestions):
    """
    Validate that a suggestions JSON has the expected structure.

    Each entry must have an integer "post_id" and a list "cats".
    Values in "cats" must be category term IDs.
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
        elif any(type(cat) is not int for cat in entry['cats']):
            errors.append(f'Post ID {entry.get("post_id", f"index {i}")}: "cats" must contain only ints')
        if 'new_cats' in entry:
            if not isinstance(entry['new_cats'], list):
                errors.append(f'Post ID {entry.get("post_id", f"index {i}")}: "new_cats" must be list')
            elif any(not isinstance(cat, str) for cat in entry['new_cats']):
                errors.append(f'Post ID {entry.get("post_id", f"index {i}")}: "new_cats" must contain only strings')

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

    for key in ('timestamp', 'site_url', 'total_posts', 'total_categories', 'default_category_slug', 'categories', 'post_categories'):
        if key not in backup:
            errors.append(f'Missing required key: "{key}"')

    if 'default_category_slug' in backup and not isinstance(backup['default_category_slug'], str):
        errors.append('"default_category_slug" must be str')

    if 'categories' in backup:
        if not isinstance(backup['categories'], list):
            errors.append('"categories" must be a list')
        else:
            for i, cat in enumerate(backup['categories']):
                for field in ('term_id', 'name', 'slug'):
                    if field not in cat:
                        errors.append(f'Category at index {i}: missing "{field}"')

    if 'post_categories' in backup:
        if not isinstance(backup['post_categories'], list):
            errors.append('"post_categories" must be a list')
        else:
            for i, pc in enumerate(backup['post_categories']):
                for field in ('post_id', 'category_slugs'):
                    if field not in pc:
                        errors.append(f'Post mapping at index {i}: missing "{field}"')
                if isinstance(pc.get('category_slugs'), list) and any(not isinstance(slug, str) for slug in pc['category_slugs']):
                    errors.append(f'Post mapping at index {i}: "category_slugs" must contain only strings')

    return errors


def resolve_category_export_row(categories, *, term_id=None, slug=None, name=None):
    """
    Resolve a category from exported metadata without guessing.

    Delete/update operations must use the exact term_id or slug captured
    during export. Name-only lookups are rejected because duplicate names
    under different parents can map to different slugs.

    Args:
        categories: Parsed JSON list from data/export/categories.json or
            backup["categories"].
        term_id: Exact exported term ID to match.
        slug: Exact exported slug to match.
        name: Optional display name, used only to produce a clearer error
            when a caller tries to resolve by name alone.

    Returns:
        Matching category dict from the export.

    Raises:
        ValueError: If no stable identifier was provided, if duplicates are
            present in the export, or if provided identifiers disagree.
        KeyError: If the requested term_id/slug does not exist in the export.
    """
    if not isinstance(categories, list):
        raise ValueError('categories must be a list of exported category objects')

    if term_id is None and slug is None:
        if name:
            raise ValueError(
                f'Cannot resolve category "{name}" from name alone; '
                'use the exported term_id or exact slug instead.'
            )
        raise ValueError('Provide term_id or slug from the category export')

    match = None

    if term_id is not None:
        id_matches = [
            category for category in categories
            if isinstance(category, dict) and category.get('term_id') == term_id
        ]
        if not id_matches:
            raise KeyError(f'No category found with term_id {term_id}')
        if len(id_matches) > 1:
            raise ValueError(f'Duplicate term_id {term_id} found in category export')
        match = id_matches[0]

    if slug is not None:
        slug_matches = [
            category for category in categories
            if isinstance(category, dict) and category.get('slug') == slug
        ]
        if not slug_matches:
            raise KeyError(f'No category found with slug "{slug}"')
        if len(slug_matches) > 1:
            raise ValueError(f'Duplicate slug "{slug}" found in category export')
        slug_match = slug_matches[0]
        if match is not None and match is not slug_match:
            raise ValueError(
                f'term_id {term_id} and slug "{slug}" resolve to different categories'
            )
        match = slug_match

    return match


def parse_change_log(log_path):
    """
    Parse a TSV change log file into a list of change dicts.

    Uses csv.DictReader so quoted tabs and embedded newlines are handled the
    same way they were written by PHP's fputcsv().

    Args:
        log_path: Path to the TSV log file.

    Returns:
        List of dicts with keys: timestamp, action, post_id, post_title,
        old_categories, new_categories, cats_added, cats_removed.
    """
    with open(log_path, newline='') as f:
        return list(csv.DictReader(f, delimiter='\t'))
