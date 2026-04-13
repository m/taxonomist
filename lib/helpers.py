"""
Taxonomist helper functions for local data processing.

These run on the user's machine (not on WordPress) and handle:
- Splitting exported posts into batches for parallel AI analysis
- Aggregating analysis results from multiple agent batches
- Validating data formats (export JSON, suggestion JSON, backup JSON, log TSV)
- Generating summary statistics from analysis results
"""

import csv
import hashlib
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


def compute_batch_fingerprint(posts):
    """
    Compute a stable fingerprint from the post IDs in the list.

    The fingerprint is a SHA-256 hex digest of the sorted post IDs.
    Used to detect whether the post set has changed since the last
    batch split, so resume logic knows if batches need regenerating.

    Args:
        posts: List of post dicts (each must have a 'post_id' key).

    Returns:
        Hex digest string.
    """
    post_ids = sorted(p.get('post_id', 0) for p in posts)
    return hashlib.sha256(json.dumps(post_ids).encode()).hexdigest()


def batch_manifest_path(batch_dir):
    """Return the path to the batch manifest file in the given directory."""
    return os.path.join(batch_dir, 'batch-manifest.json')


def write_batches(posts, batch_dir, batch_size=None, resume=False):
    """
    Split posts into batches and write each to a numbered JSON file.

    Creates files like batch-000.json, batch-001.json, etc. If batch_size
    is not provided, it's calculated automatically to stay under the agent
    Read token limit (10K tokens).

    When resume=False (default), stale batch-NNN.json files are removed so
    reruns don't leave behind extra batches from a previous export.

    When resume=True, the function checks whether the post set has changed
    since the last split by comparing fingerprints stored in
    batch-manifest.json. If the fingerprint matches, existing batches are
    reused without rewriting. If it doesn't match (or no manifest exists),
    batches are cleared and rewritten.

    Args:
        posts: List of post dicts from the export JSON.
        batch_dir: Directory to write batch files into. Created if missing.
        batch_size: Posts per batch. If None, calculated from content sizes.
        resume: If True, reuse existing batches when the post set is unchanged.

    Returns:
        Tuple of (paths_written, batch_size_used).
    """
    os.makedirs(batch_dir, exist_ok=True)
    manifest_file = batch_manifest_path(batch_dir)

    if resume:
        fingerprint = compute_batch_fingerprint(posts)
        if os.path.exists(manifest_file):
            with open(manifest_file) as f:
                manifest = json.load(f)
            if manifest.get('fingerprint') == fingerprint:
                # Post set unchanged — reuse existing batches.
                existing_batch_size = manifest['batch_size']
                num_batches = manifest['num_batches']
                paths = [
                    os.path.join(batch_dir, f'batch-{i:03d}.json')
                    for i in range(num_batches)
                ]
                return paths, existing_batch_size

    # Clear stale batch files.
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

    # Write manifest for future resume.
    with open(manifest_file, 'w') as f:
        json.dump({
            'fingerprint': compute_batch_fingerprint(posts),
            'batch_size': batch_size,
            'num_batches': len(batches),
        }, f)

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


def find_incomplete_batches(batch_dir, results_dir):
    """
    Find batches that don't have valid corresponding result files.

    For each batch-NNN.json in batch_dir, checks whether a matching
    result-NNN.json exists in results_dir and passes validation. Batches
    with missing or invalid results are returned so they can be re-analyzed.

    Args:
        batch_dir: Directory containing batch-NNN.json files.
        results_dir: Directory containing result-NNN.json files.

    Returns:
        List of batch filenames (e.g., ['batch-001.json']) that need
        re-analysis, sorted by name.
    """
    batch_files = sorted(
        f for f in os.listdir(batch_dir)
        if f.startswith('batch-') and f.endswith('.json')
        and f != 'batch-manifest.json'
    )

    if not os.path.isdir(results_dir):
        return batch_files

    incomplete = []
    for batch_file in batch_files:
        # batch-NNN.json → result-NNN.json
        result_file = 'result-' + batch_file[len('batch-'):]
        result_path = os.path.join(results_dir, result_file)

        if not os.path.exists(result_path):
            incomplete.append(batch_file)
            continue

        try:
            with open(result_path) as f:
                data = json.load(f)
            if validate_suggestions(data):
                # Validation errors — treat as incomplete.
                incomplete.append(batch_file)
                continue

            # Check that every post in the batch has a result entry.
            batch_path = os.path.join(batch_dir, batch_file)
            with open(batch_path) as bf:
                batch_data = json.load(bf)
            batch_ids = {p['post_id'] for p in batch_data}
            result_ids = {e['post_id'] for e in data}
            if not batch_ids.issubset(result_ids):
                incomplete.append(batch_file)
        except (json.JSONDecodeError, OSError):
            incomplete.append(batch_file)

    return incomplete


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
        Dict with 'suggestions' (list of dicts), 'category_counts' (Counter),
        and 'new_category_counts' (Counter).
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

    return {
        'suggestions': all_suggestions,
        'category_counts': cat_counts,
        'new_category_counts': new_cat_counts,
    }


def validate_export(posts):
    """
    Validate that an export JSON has the expected structure.

    Checks that each post has required fields with correct types.

    Args:
        posts: Parsed JSON list from the export file.

    Returns:
        Dict with 'valid' (bool) and 'errors' (list of strings).
    """
    errors = []
    if not isinstance(posts, list):
        return {'valid': False, 'errors': ['Export must be a JSON array']}

    required_fields = {
        'post_id': int,
        'title': str,
        'date': str,
        'content': str,
        'categories': list,
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

        for field in ('categories', 'category_slugs'):
            if isinstance(post.get(field), list) and any(not isinstance(value, str) for value in post[field]):
                errors.append(
                    f'Post ID {post.get("post_id", f"index {i}")}: '
                    f'"{field}" must contain only strings'
                )

    return {'valid': not errors, 'errors': errors}


def validate_suggestions(suggestions):
    """
    Validate that a suggestions JSON has the expected structure.

    Each entry must have an integer "post_id" and a list "cats".
    "new_cats" is optional.

    Args:
        suggestions: Parsed JSON list from a result file.

    Returns:
        Dict with 'valid' (bool) and 'errors' (list of strings).
    """
    errors = []
    if not isinstance(suggestions, list):
        return {'valid': False, 'errors': ['Suggestions must be a JSON array']}

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
        elif any(not isinstance(cat, str) for cat in entry['cats']):
            errors.append(f'Post ID {entry.get("post_id", f"index {i}")}: "cats" must contain only strings')
        if 'new_cats' in entry:
            if not isinstance(entry['new_cats'], list):
                errors.append(f'Post ID {entry.get("post_id", f"index {i}")}: "new_cats" must be list')
            elif any(not isinstance(cat, str) for cat in entry['new_cats']):
                errors.append(f'Post ID {entry.get("post_id", f"index {i}")}: "new_cats" must contain only strings')

    return {'valid': not errors, 'errors': errors}


def validate_backup(backup):
    """
    Validate that a backup JSON has the expected structure.

    Checks for required top-level keys and structure of nested data.

    Args:
        backup: Parsed JSON dict from a backup file.

    Returns:
        Dict with 'valid' (bool) and 'errors' (list of strings).
    """
    errors = []
    if not isinstance(backup, dict):
        return {'valid': False, 'errors': ['Backup must be a JSON object']}

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

    return {'valid': not errors, 'errors': errors}


def validate_result_ids(results_dir, batch_dir):
    """
    Verify that every post_id in the analysis results actually exists in the
    source batches.

    This catches a class of agent bug where the analyze agent outputs array
    indices (0, 1, 2, …) instead of real WordPress post IDs. It also detects
    IDs that appear in results but were never part of any batch, and batch
    posts that are missing from the results entirely.

    Args:
        results_dir: Directory containing result-NNN.json files.
        batch_dir: Directory containing the corresponding batch-NNN.json files.

    Returns:
        A dict with keys:
            valid (bool): True if all checks pass.
            errors (list[str]): Human-readable error descriptions.
            invalid_ids (set[int]): Result post_ids not found in any batch.
            missing_ids (set[int]): Batch post_ids with no result entry.
            suspect_index_files (list[str]): Result files whose IDs look like
                sequential indices rather than real post IDs.
    """
    # Collect every post_id present in the source batches.
    batch_ids = set()
    for filename in sorted(os.listdir(batch_dir)):
        if not (filename.startswith('batch-') and filename.endswith('.json')):
            continue
        with open(os.path.join(batch_dir, filename)) as f:
            for post in json.load(f):
                pid = post.get('post_id')
                if isinstance(pid, int):
                    batch_ids.add(pid)

    # Collect every post_id from the result files and track per-file ranges.
    result_ids = set()
    per_file = {}  # filename -> list of post_ids
    for filename in sorted(os.listdir(results_dir)):
        if not (filename.startswith('result-') and filename.endswith('.json')):
            continue
        with open(os.path.join(results_dir, filename)) as f:
            ids = []
            for entry in json.load(f):
                pid = entry.get('post_id')
                if isinstance(pid, int):
                    ids.append(pid)
                    result_ids.add(pid)
            per_file[filename] = ids

    errors = []
    invalid_ids = result_ids - batch_ids
    missing_ids = batch_ids - result_ids
    suspect_files = []

    if invalid_ids:
        errors.append(
            f'{len(invalid_ids)} result post_id(s) not found in any batch: '
            f'{sorted(invalid_ids)[:20]}{"…" if len(invalid_ids) > 20 else ""}'
        )

    if missing_ids:
        errors.append(
            f'{len(missing_ids)} batch post(s) have no result entry: '
            f'{sorted(missing_ids)[:20]}{"…" if len(missing_ids) > 20 else ""}'
        )

    # Heuristic: if a result file's IDs form a near-contiguous run starting
    # at 0 or 1 and the batch IDs do NOT start near 0, it's almost certainly
    # an index-vs-ID bug.
    min_batch_id = min(batch_ids) if batch_ids else 0
    for filename, ids in per_file.items():
        if len(ids) < 2:
            continue
        sorted_ids = sorted(ids)
        starts_near_zero = sorted_ids[0] <= 1
        looks_sequential = all(
            sorted_ids[i + 1] - sorted_ids[i] <= 2
            for i in range(min(len(sorted_ids) - 1, 20))
        )
        if starts_near_zero and looks_sequential and min_batch_id > 100:
            suspect_files.append(filename)
            errors.append(
                f'{filename}: IDs look like array indices (0–{sorted_ids[-1]}) '
                f'instead of real post IDs (batch range {min_batch_id}–{max(batch_ids)})'
            )

    return {
        'valid': len(errors) == 0,
        'errors': errors,
        'invalid_ids': invalid_ids,
        'missing_ids': missing_ids,
        'suspect_index_files': suspect_files,
    }


def validate_category_slugs(suggestions, valid_slugs):
    """
    Check that every category slug in the suggestions exists in the valid set.

    Args:
        suggestions: List of suggestion dicts (each with a 'cats' list).
        valid_slugs: Set of valid category slug strings.

    Returns:
        A dict with keys:
            valid (bool): True if all slugs are recognized.
            unknown_slugs (Counter): slug -> count of occurrences.
            errors (list[str]): Human-readable error descriptions.
    """
    unknown = Counter()
    for entry in suggestions:
        for cat in entry.get('cats', []):
            if cat not in valid_slugs:
                unknown[cat] += 1

    errors = []
    if unknown:
        errors.append(
            f'{len(unknown)} unknown category slug(s): '
            + ', '.join(f'{slug} ({n}x)' for slug, n in unknown.most_common(10))
        )

    return {
        'valid': len(errors) == 0,
        'unknown_slugs': unknown,
        'errors': errors,
    }


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
