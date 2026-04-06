"""
Taxonomist helper functions for local data processing.

These run on the user's machine (not on WordPress) and handle:
- Splitting exported posts into batches for parallel AI analysis
- Aggregating analysis results from multiple agent batches
- Validating data formats (export JSON, suggestion JSON, backup JSON, log TSV)
- Generating summary statistics from analysis results
- Rendering hierarchy tree diffs for plan review
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
        elif any(not isinstance(cat, str) for cat in entry['cats']):
            errors.append(f'Post ID {entry.get("post_id", f"index {i}")}: "cats" must contain only strings')
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


def _build_tree(categories, actions=None):
    """
    Build a tree structure from a flat list of categories.

    Indexes categories by slug and term_id, builds a parent-child map,
    and injects synthetic nodes for 'create' actions.

    Args:
        categories: List of category dicts (term_id, name, slug, count, parent).
        actions: Optional dict mapping slug to action descriptors.

    Returns:
        Tuple of (roots, children_map, node_map) where:
        - roots: sorted list of root-level slugs
        - children_map: {slug: [child_slugs]} sorted by name
        - node_map: {slug: category dict}
    """
    actions = actions or {}
    node_map = {}
    id_to_slug = {}

    for cat in categories:
        slug = cat['slug']
        node_map[slug] = cat
        # Coerce to int: WordPress APIs may return string IDs.
        id_to_slug[int(cat['term_id'])] = slug

    children_map = {}
    roots = []
    seen = set()

    for cat in categories:
        slug = cat['slug']
        seen.add(slug)
        parent_id = int(cat.get('parent', 0))
        if parent_id == 0:
            roots.append(slug)
        else:
            parent_slug = id_to_slug.get(parent_id)
            if parent_slug:
                children_map.setdefault(parent_slug, []).append(slug)
            else:
                roots.append(slug)
                node_map[slug] = {**cat, '_warning': 'parent missing'}

    # Inject synthetic nodes for 'create' actions.
    for slug, action in actions.items():
        if action.get('action') != 'create':
            continue
        if slug in seen:
            continue
        node_map[slug] = {
            'name': action.get('name', slug.replace('-', ' ').title()),
            'slug': slug,
            'count': action.get('count', 0),
            'parent': 0,
            '_synthetic': True,
        }
        parent_slug = action.get('parent_slug')
        if parent_slug and parent_slug in node_map:
            children_map.setdefault(parent_slug, []).append(slug)
        elif parent_slug:
            roots.append(slug)
            node_map[slug]['_warning'] = 'parent not found'
        else:
            roots.append(slug)

    # Detect circular parent references.
    for slug in list(node_map):
        visited = set()
        current = slug
        while current and current in node_map:
            if current in visited:
                # Break the cycle: move to root.
                if current not in roots:
                    roots.append(current)
                for parent_slug, child_list in children_map.items():
                    if current in child_list:
                        child_list.remove(current)
                        break
                node_map[current] = {**node_map[current], '_warning': 'circular'}
                break
            visited.add(current)
            parent_id = int(node_map[current].get('parent', 0))
            current = id_to_slug.get(parent_id) if parent_id else None

    # Sort children by name, roots by name.
    for slug in children_map:
        children_map[slug].sort(key=lambda s: node_map[s]['name'].lower())
    roots.sort(key=lambda s: node_map[s]['name'].lower())

    return roots, children_map, node_map


def _detect_orphans(children_map, actions):
    """
    Find categories whose parent is being retired or merged.

    A category is orphaned if its parent has action 'retire' or 'merge'
    but the category itself is being kept (or has no action).

    Args:
        children_map: {slug: [child_slugs]} from _build_tree.
        actions: Dict mapping slug to action descriptors.

    Returns:
        Tuple of (orphaned_slugs, orphan_counts) where:
        - orphaned_slugs: set of slugs that would become orphaned
        - orphan_counts: {parent_slug: number of orphaned children}
    """
    actions = actions or {}
    orphaned = set()
    orphan_counts = {}

    for slug, action in actions.items():
        if action.get('action') not in ('retire', 'merge'):
            continue
        children = children_map.get(slug, [])
        count = 0
        for child in children:
            child_action = actions.get(child, {}).get('action', 'keep')
            if child_action not in ('retire', 'merge'):
                orphaned.add(child)
                count += 1
        if count > 0:
            orphan_counts[slug] = count

    return orphaned, orphan_counts


def _render_node(node, action_info, is_orphaned):
    """
    Render a single category node's display text with annotations.

    Args:
        node: Category dict with at least 'name' and 'count'.
        action_info: Action dict or None.
        is_orphaned: Whether this node would become orphaned.

    Returns:
        String like 'Akismet (2)  ✕ retire → Plugins'
    """
    name = node['name']
    count = node.get('count', 0)
    warning = node.get('_warning')

    label = f'{name} ({count})'

    parts = [label]

    if action_info:
        action = action_info.get('action', 'keep')
        if action == 'retire':
            target = action_info.get('target')
            if target:
                parts.append(f'\u2715 retire \u2192 {target}')
            else:
                parts.append('\u2715 retire')
        elif action == 'merge':
            target = action_info.get('target', '?')
            parts.append(f'\u2192 merge into {target}')
        elif action == 'create':
            parts.append('\u2605 new')

    if warning:
        parts.append(f'\u26a0 {warning}')
    elif is_orphaned:
        parts.append('\u26a0 orphaned')

    if action_info and action_info.get('action') in ('retire', 'merge'):
        orphan_count = action_info.get('_orphan_count', 0)
        if orphan_count:
            s = 'child' if orphan_count == 1 else 'children'
            parts.append(f'\u26a0 {orphan_count} {s} orphaned')

    return '  '.join(parts)


def _format_tree_lines(slug, children_map, node_map, actions, orphaned, prefix, is_last):
    """
    Recursively render tree lines with box-drawing connectors.

    Args:
        slug: Current category slug to render.
        children_map: {slug: [child_slugs]}.
        node_map: {slug: category dict}.
        actions: Dict mapping slug to action descriptors.
        orphaned: Set of orphaned slugs.
        prefix: String prefix for indentation.
        is_last: Whether this is the last sibling.

    Returns:
        List of output strings.
    """
    connector = '\u2514\u2500\u2500 ' if is_last else '\u251c\u2500\u2500 '
    node = node_map[slug]
    action_info = (actions or {}).get(slug)
    line = prefix + connector + _render_node(node, action_info, slug in orphaned)
    lines = [line]

    child_prefix = prefix + ('    ' if is_last else '\u2502   ')
    children = children_map.get(slug, [])
    for i, child_slug in enumerate(children):
        child_is_last = (i == len(children) - 1)
        lines.extend(_format_tree_lines(
            child_slug, children_map, node_map, actions,
            orphaned, child_prefix, child_is_last,
        ))

    return lines


def render_category_tree(categories, actions=None):
    """
    Render an annotated ASCII tree of the category hierarchy.

    Shows parent-child relationships with box-drawing characters and
    annotates proposed changes (retire, merge, create). Detects and
    warns about categories that would become orphaned.

    Args:
        categories: List of category dicts from the export/backup JSON.
            Each dict has: term_id, name, slug, count, parent.
        actions: Optional dict mapping category slug to an action descriptor:
            {
                "action": "keep" | "retire" | "merge" | "create",
                "target": "merge-target-slug",
                "parent_slug": "parent-for-new-category",
                "name": "Display Name (for create)",
                "count": 0,
                "detail": "optional context"
            }

    Returns:
        Multi-line string showing the hierarchy with annotations.
    """
    if not categories and not (actions and any(
        a.get('action') == 'create' for a in actions.values()
    )):
        return '(no categories)'

    roots, children_map, node_map = _build_tree(categories, actions)
    orphaned, orphan_counts = _detect_orphans(children_map, actions)

    # Inject orphan counts into action_info for rendering.
    if actions:
        actions = {slug: {**info} for slug, info in actions.items()}
        for slug, count in orphan_counts.items():
            if slug in actions:
                actions[slug]['_orphan_count'] = count

    if not roots:
        return '(no categories)'

    # Single root: render directly. Multiple roots: wrap in virtual root.
    if len(roots) == 1:
        root = roots[0]
        node = node_map[root]
        action_info = (actions or {}).get(root)
        header = _render_node(node, action_info, root in orphaned)
        lines = [header]
        children = children_map.get(root, [])
        for i, child in enumerate(children):
            lines.extend(_format_tree_lines(
                child, children_map, node_map, actions,
                orphaned, '', i == len(children) - 1,
            ))
    else:
        lines = ['Categories']
        for i, root in enumerate(roots):
            lines.extend(_format_tree_lines(
                root, children_map, node_map, actions,
                orphaned, '', i == len(roots) - 1,
            ))

    return '\n'.join(lines)
