"""
WordPress.com REST API adapter for Taxonomist.

Provides a tested, safe interface to the WordPress.com / Jetpack API,
preventing the data integrity bugs documented in issues #1-4:
- #1: Always uses term_id for operations, never guesses slugs
- #2: Proper array encoding via wp_urlencode (doseq=True)
- #3: Always includes parent in update payloads, verifies no duplicates
- #4: Uses wp_urlencode consistently for all encoding

Matches the WpCliAdapter interface so the orchestrating agent can swap
between adapters transparently.
"""

import csv
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone


def wp_urlencode(params):
    """
    URL-encode params with proper list handling.

    Uses doseq=True so list values produce repeated keys instead of
    being stringified. Prevents issue #2 where Python's default
    urlencode turned ['Tech', 'Science'] into a literal string.

    Args:
        params: Dict or list of (key, value) tuples.

    Returns:
        URL-encoded string.
    """
    return urllib.parse.urlencode(params, doseq=True)


class WpcomApiError(Exception):
    """Raised when the WordPress.com API returns an error."""

    def __init__(self, status_code, error, message=''):
        self.status_code = status_code
        self.error = error
        super().__init__(f'WP.com API {status_code}: {error} - {message}')


class PartialRestoreError(Exception):
    """Raised when a restore completes but some operations failed.

    Callers who only check for exceptions will see this instead of
    silently receiving a success-shaped dict from a half-done revert.
    The full result dict (with operations, errors, and partial=True)
    is attached for inspection.
    """

    def __init__(self, result):
        self.result = result
        n = len(result.get('errors', []))
        super().__init__(
            f'Restore completed with {n} error(s) — site may be in a '
            'partially reverted state. Inspect result.errors for details.'
        )


# Log row action types. Defined as constants so a typo in either the
# writer or the reader becomes a NameError instead of a silent no-op.
ACTION_SET_CATS = 'SET_CATS'
ACTION_CREATE_CAT = 'CREATE_CAT'
ACTION_DELETE_CAT = 'DELETE_CAT'
ACTION_UPDATE_CAT = 'UPDATE_CAT'
ACTION_SET_DEFAULT = 'SET_DEFAULT'

# Sources for the combined replay stream in restore_from_logs.
SOURCE_TERM = 'term'
SOURCE_CHANGE = 'change'

# restore() mode selection.
MODE_AUTO = 'auto'
MODE_LOGS = 'logs'
MODE_SNAPSHOT = 'snapshot'


def _term_snapshot(cat):
    """Distill a category dict to the minimum needed to rehydrate it."""
    return {
        'ID': cat.get('ID'),
        'name': cat.get('name'),
        'slug': cat.get('slug'),
        'description': cat.get('description', ''),
        'parent': cat.get('parent', 0),
    }


class WpcomAdapter:
    """
    Adapter for the WordPress.com / Jetpack REST API.

    Implements the same interface as WpCliAdapter so the orchestrating
    agent can use either adapter transparently.
    """

    BASE_URL = 'https://public-api.wordpress.com/rest/v1.1'

    def __init__(self, config):
        """
        Initialize from a config dict (parsed from config.json).

        Args:
            config: Dict with 'site_url' and 'connection' containing
                    'method', 'site_id', and 'access_token'.

        Raises:
            ValueError: If required config fields are missing.
        """
        conn = config.get('connection', {})
        if conn.get('method') != 'wpcom-api':
            raise ValueError(
                f"Expected connection method 'wpcom-api', "
                f"got '{conn.get('method')}'"
            )
        if not conn.get('site_id'):
            raise ValueError('Missing required field: connection.site_id')
        if not conn.get('access_token'):
            raise ValueError('Missing required field: connection.access_token')
        self.site_id = str(conn['site_id'])
        self.access_token = conn['access_token']
        self.site_url = config.get('site_url', '')
        self._category_cache = None
        # When set via set_logging(), every mutating call appends to these
        # TSV files so a later restore() can replay them in reverse.
        self.changes_log_path = None
        self.terms_log_path = None
        # Track which log paths have already had a header row written this
        # process so _append_tsv doesn't stat() the file on every row.
        self._log_headers_written = set()

    # --- HTTP layer ---

    def _request(self, method, path, data=None, params=None):
        """
        Make an authenticated request to the WordPress.com API.

        Args:
            method: HTTP method (GET, POST).
            path: API path (e.g., '/sites/{id}/categories').
            data: Dict of form data for POST requests.
            params: Dict of query parameters.

        Returns:
            Parsed JSON response as a dict.

        Raises:
            WpcomApiError: On any API error (including 200 with error field).
        """
        url = f'{self.BASE_URL}{path}'
        if params:
            url += '?' + wp_urlencode(params)

        body = None
        if data is not None:
            body = wp_urlencode(data).encode('utf-8')

        req = urllib.request.Request(url, data=body, method=method)
        req.add_header('Authorization', f'Bearer {self.access_token}')
        req.add_header('User-Agent', 'taxonomist/1.0')
        if body:
            req.add_header('Content-Type', 'application/x-www-form-urlencoded')

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_body = resp.read().decode('utf-8')
                try:
                    result = json.loads(resp_body)
                except json.JSONDecodeError:
                    raise WpcomApiError(
                        resp.status, 'invalid_json',
                        f'Expected JSON, got: {resp_body[:200]}',
                    )
                if isinstance(result, dict) and 'error' in result:
                    raise WpcomApiError(
                        resp.status, result['error'],
                        result.get('message', ''),
                    )
                return result
        except urllib.error.HTTPError as e:
            try:
                body_text = e.read().decode('utf-8', errors='replace')
            except Exception:
                body_text = str(e)
            try:
                err = json.loads(body_text)
                raise WpcomApiError(
                    e.code, err.get('error', 'unknown'),
                    err.get('message', body_text),
                ) from e
            except json.JSONDecodeError:
                raise WpcomApiError(e.code, 'http_error', body_text) from e
        except urllib.error.URLError as e:
            raise WpcomApiError(
                0, 'connection_error',
                f'Failed to connect to {url}: {e.reason}',
            ) from e

    def _get(self, path, params=None):
        return self._request('GET', path, params=params)

    def _post(self, path, data=None):
        return self._request('POST', path, data=data)

    # --- Category cache ---

    def _ensure_category_cache(self):
        if self._category_cache is None:
            self._category_cache = self.list_categories()
        return self._category_cache

    def _invalidate_category_cache(self):
        self._category_cache = None

    def _find_category(self, predicate):
        """
        Find a category matching `predicate`. Cache-first, one refresh on miss.

        Used by all the public lookup-by-X helpers below so they share
        the same cache-then-refresh-on-miss behavior.
        """
        for cat in self._ensure_category_cache():
            if predicate(cat):
                return cat
        self._invalidate_category_cache()
        for cat in self._ensure_category_cache():
            if predicate(cat):
                return cat
        return None

    def _get_category_by_id(self, term_id):
        return self._find_category(lambda c: c.get('ID') == term_id)

    def _lookup_category_by_slug(self, slug):
        if not slug:
            return None
        return self._find_category(lambda c: c.get('slug') == slug)

    def _verify_category_state(self, slug, expected_fields):
        """
        Read back a category and verify it matches expected state.

        Returns an error string if verification fails, None if OK.
        Used after mutations in the restore path to catch silent drift.
        """
        self._invalidate_category_cache()
        cat = self._lookup_category_by_slug(slug)
        if cat is None:
            return f'verification: category slug "{slug}" not found after write'
        mismatches = []
        for field, expected in expected_fields.items():
            actual = cat.get(field)
            if str(actual) != str(expected):
                mismatches.append(f'{field}: expected {expected!r}, got {actual!r}')
        if mismatches:
            return f'verification on "{slug}": {"; ".join(mismatches)}'
        return None

    def _verify_category_absent(self, slug):
        """Verify a category no longer exists after deletion."""
        self._invalidate_category_cache()
        cat = self._lookup_category_by_slug(slug)
        if cat is not None:
            return f'verification: category "{slug}" still exists after delete'
        return None

    def _lookup_category_by_name(self, name):
        if not name:
            return None
        return self._find_category(lambda c: c.get('name') == name)

    def _get_category_count(self):
        """Get the current total number of categories on the site."""
        resp = self._get(
            f'/sites/{self.site_id}/categories',
            params={'number': 0},
        )
        return resp.get('found', 0)

    # --- WpCliAdapter-compatible interface ---

    def list_categories(self):
        """
        List all categories with metadata.

        Paginates through all categories (1000 per page).

        Returns:
            List of category dicts with at least: ID, name, slug,
            description, parent, post_count.
        """
        all_categories = []
        offset = 0
        page_size = 1000
        while True:
            resp = self._get(
                f'/sites/{self.site_id}/categories',
                params={'number': page_size, 'offset': offset},
            )
            categories = resp.get('categories', [])
            all_categories.extend(categories)
            found = resp.get('found', 0)
            if offset + page_size >= found or not categories:
                break
            offset += page_size
        return all_categories

    def export_posts(self, output_path):
        """
        Export all published posts with categories to a JSON file.

        Uses page_handle cursor pagination. Normalizes category hashes
        to lists.

        Args:
            output_path: Path to write the JSON file.
        """
        all_posts = []
        page_handle = None

        while True:
            params = {
                'number': 100,
                'status': 'publish',
                'fields': 'ID,title,content,date,categories',
            }
            if page_handle:
                params['page_handle'] = page_handle
            resp = self._get(f'/sites/{self.site_id}/posts', params=params)
            posts = resp.get('posts', [])

            for post in posts:
                # Normalize categories from {name: {ID: ...}} hash to list.
                cat_hash = post.get('categories') or {}
                cat_names = list(cat_hash.keys())
                cat_slugs = [v.get('slug', '') for v in cat_hash.values()]

                content = post.get('content', '')
                # Strip HTML tags for plain-text analysis.
                content = re.sub(r'<[^>]+>', '', content)
                content = re.sub(r'\s+', ' ', content).strip()

                all_posts.append({
                    'post_id': post['ID'],
                    'title': post.get('title', ''),
                    'date': post.get('date', ''),
                    'content': content,
                    'categories': cat_names,
                    'category_slugs': cat_slugs,
                    'url': post.get('URL', ''),
                })

            meta = resp.get('meta', {})
            page_handle = meta.get('next_page')
            if not page_handle or not posts:
                break

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(all_posts, f, ensure_ascii=False)

    def set_post_categories(self, post_id, category_ids,
                            old_category_ids=None, post_title=''):
        """
        Set categories for a post by term IDs.

        Resolves IDs to names because the WP.com API accepts
        comma-separated category names, not IDs.

        Args:
            post_id: Integer post ID.
            category_ids: List of integer category IDs to set.
            old_category_ids: List of category IDs the post had before
                this call. Required when logging is enabled — the
                orchestrator already has this state from the export, so
                we never spend an extra GET to recover it. When logging
                is off this argument is ignored.
            post_title: Optional title for the log row (display only).

        Raises:
            WpcomApiError: If any category ID is not found.
            ValueError: If logging is enabled but old_category_ids is
                not supplied (would produce an unrevertable log row).
        """
        names = [self._resolve_id_to_name(cid) for cid in category_ids]

        old_names = []
        if self.changes_log_path and not old_category_ids:
            raise ValueError(
                f'set_post_categories(post_id={post_id}): '
                'old_category_ids is required when logging is enabled. '
                'Pass the pre-change category IDs from the export so '
                'the change log can record a reversible operation.'
            )
        if self.changes_log_path and old_category_ids:
            old_names = [
                cat['name']
                for cid in old_category_ids
                for cat in (self._get_category_by_id(cid),)
                if cat is not None
            ]

        self._post(
            f'/sites/{self.site_id}/posts/{post_id}',
            data={'categories': ','.join(names)},
        )

        if self.changes_log_path:
            self._log_post_change(
                action=ACTION_SET_CATS,
                post_id=post_id,
                post_title=post_title,
                old_categories=old_names,
                new_categories=names,
                cats_added=[n for n in names if n not in old_names],
                cats_removed=[n for n in old_names if n not in names],
            )

    def _resolve_id_to_name(self, term_id):
        cat = self._get_category_by_id(term_id)
        if cat is None:
            raise WpcomApiError(
                404, 'not_found', f'Category ID {term_id} not found',
            )
        return cat['name']

    def create_category(self, name, slug, description='', parent=0):
        """
        Create a new category.

        Args:
            name: Display name.
            slug: URL slug.
            description: Category description.
            parent: Parent category ID (0 for top-level).

        Returns:
            API response dict with the new category.
        """
        data = {'name': name}
        if slug:
            data['slug'] = slug
        if description:
            data['description'] = description
        if parent:
            data['parent'] = parent

        result = self._post(f'/sites/{self.site_id}/categories/new', data=data)
        self._invalidate_category_cache()
        self._log_term_op(
            ACTION_CREATE_CAT,
            term_id=result.get('ID', 0),
            slug=result.get('slug', slug or ''),
            field='*',
            old_value='',
            new_value=json.dumps(_term_snapshot(result), ensure_ascii=False),
        )
        return result

    def delete_category(self, term_id):
        """
        Delete a category by term ID.

        Always resolves the term_id to its slug from live data before
        deleting. Never guesses slugs. (Prevents issue #1.)

        Args:
            term_id: Integer category ID.

        Raises:
            TypeError: If term_id is not an int.
            WpcomApiError: If category not found.
        """
        if not isinstance(term_id, int):
            raise TypeError(
                f'term_id must be int, got {type(term_id).__name__}'
            )
        cat = self._get_category_by_id(term_id)
        if cat is None:
            raise WpcomApiError(
                404, 'not_found', f'Category {term_id} does not exist',
            )
        # Capture the snapshot BEFORE the destructive call so revert can
        # rehydrate the category exactly.
        snapshot = json.dumps(_term_snapshot(cat), ensure_ascii=False)
        slug = urllib.parse.quote(cat['slug'], safe='')
        self._post(
            f'/sites/{self.site_id}/categories/slug:{slug}/delete',
        )
        self._invalidate_category_cache()
        self._log_term_op(
            ACTION_DELETE_CAT,
            term_id=cat.get('ID', term_id),
            slug=cat.get('slug', ''),
            field='*',
            old_value=snapshot,
            new_value='',
        )

    # --- Extended interface ---

    def update_category(self, term_id, fields):
        """
        Update a category's fields.

        Always includes the current parent in the payload to prevent
        the WP.com API from silently creating a duplicate term at
        root level. Verifies term count before and after to detect
        duplicates. (Prevents issue #3.)

        Args:
            term_id: Integer category ID.
            fields: Dict of fields to update (description, name, etc.).

        Returns:
            API response dict.

        Raises:
            WpcomApiError: If category not found or duplicate detected.
        """
        if not isinstance(term_id, int):
            raise TypeError(
                f'term_id must be int, got {type(term_id).__name__}'
            )
        current = self._get_category_by_id(term_id)
        if current is None:
            raise WpcomApiError(
                404, 'not_found', f'Category {term_id} not found',
            )

        # Copy to avoid mutating the caller's dict.
        payload = dict(fields)

        # Snapshot old field values BEFORE the destructive call so the
        # log row records what was actually replaced. Holding `current`
        # alone is not enough — the cache invalidation after the POST
        # leaves us with a stale reference whose contents could be
        # overwritten by other code paths.
        old_field_values = {f: current.get(f, '') for f in fields.keys()}
        log_slug = current.get('slug', '')

        # Always include parent to prevent silent duplicate creation.
        if 'parent' not in payload:
            payload['parent'] = current.get('parent', 0)

        pre_count = self._get_category_count()

        slug = urllib.parse.quote(current['slug'], safe='')
        result = self._post(
            f'/sites/{self.site_id}/categories/slug:{slug}',
            data=payload,
        )

        post_count = self._get_category_count()
        if post_count > pre_count:
            raise WpcomApiError(
                409, 'duplicate_detected',
                f'Term count increased from {pre_count} to {post_count} '
                f'during update of term {term_id}. A duplicate may have '
                f'been created. Manual inspection required.',
            )

        self._invalidate_category_cache()

        # Log one row per field actually being changed.
        for field, new_val in fields.items():
            old_val = old_field_values.get(field, '')
            if old_val == new_val:
                continue
            self._log_term_op(
                ACTION_UPDATE_CAT,
                term_id=term_id,
                slug=log_slug,
                field=field,
                old_value='' if old_val is None else str(old_val),
                new_value='' if new_val is None else str(new_val),
            )

        return result

    def get_default_category(self):
        """
        Get the site's default category.

        Returns:
            Category dict for the default category.

        Raises:
            WpcomApiError: If settings or category not found.
        """
        resp = self._get(f'/sites/{self.site_id}/settings')
        settings = resp if 'default_category' in resp else resp.get('settings', {})
        default_id = settings.get('default_category', 1)

        cat = self._get_category_by_id(default_id)
        if cat is None:
            raise WpcomApiError(
                404, 'not_found',
                f'Default category {default_id} not found',
            )
        return cat

    def set_default_category(self, term_id):
        """
        Change the site's default category.

        WordPress assigns the default category to any post that would
        otherwise be uncategorized. Changing it is a prerequisite to
        deleting the current default. Logged so revert can restore it.

        Args:
            term_id: Integer term ID of the new default category.

        Raises:
            WpcomApiError: If the term doesn't exist.
        """
        if not isinstance(term_id, int):
            raise TypeError(
                f'term_id must be int, got {type(term_id).__name__}'
            )
        new_cat = self._get_category_by_id(term_id)
        if new_cat is None:
            raise WpcomApiError(
                404, 'not_found', f'Category {term_id} not found',
            )

        # Capture current default for the inverse-replay log.
        try:
            old_cat = self.get_default_category()
            old_id = old_cat.get('ID', 0)
            old_slug = old_cat.get('slug', '')
        except WpcomApiError as e:
            if e.status_code == 404:
                old_id, old_slug = 0, ''
            else:
                raise

        self._post(
            f'/sites/{self.site_id}/settings',
            data={'default_category': term_id},
        )
        self._log_term_op(
            ACTION_SET_DEFAULT,
            term_id=term_id,
            slug=new_cat.get('slug', ''),
            field='default_category',
            old_value=f'{old_id}:{old_slug}',
            new_value=f'{term_id}:{new_cat.get("slug", "")}',
        )

    def backup(self, output_path):
        """
        Create a full taxonomy state backup.

        Args:
            output_path: Path to write the backup JSON file.
        """
        categories = self.list_categories()

        # Get post-category mappings.
        post_categories = []
        page_handle = None
        while True:
            params = {
                'number': 100,
                'status': 'publish',
                'fields': 'ID,title,categories',
            }
            if page_handle:
                params['page_handle'] = page_handle
            resp = self._get(f'/sites/{self.site_id}/posts', params=params)
            for post in resp.get('posts', []):
                cat_hash = post.get('categories') or {}
                post_categories.append({
                    'post_id': post['ID'],
                    'post_title': post.get('title', ''),
                    'category_ids': [v['ID'] for v in cat_hash.values()],
                    'category_slugs': [v.get('slug', '') for v in cat_hash.values()],
                })
            meta = resp.get('meta', {})
            page_handle = meta.get('next_page')
            if not page_handle or not resp.get('posts'):
                break

        # Resolve default category slug. Only suppress 404 (category
        # genuinely missing); all other errors should propagate.
        try:
            default_cat = self.get_default_category()
            default_slug = default_cat.get('slug', '')
        except WpcomApiError as e:
            if e.status_code == 404:
                default_slug = ''
            else:
                raise

        backup_data = {
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime()),
            'site_url': self.site_url,
            'total_posts': len(post_categories),
            'total_categories': len(categories),
            'default_category_slug': default_slug,
            'categories': [{
                'term_id': c['ID'],
                'name': c['name'],
                'slug': c['slug'],
                'description': c.get('description', ''),
                'count': c.get('post_count', 0),
                'parent': c.get('parent', 0),
            } for c in categories],
            'post_categories': post_categories,
        }

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(backup_data, f, indent=2, ensure_ascii=False)

    # --- Logging hooks ---
    #
    # Term and post mutations append to TSV logs whose schemas match what
    # parse_change_log() and parse_terms_log() in lib/helpers.py expect.
    # Logging is opt-in: an adapter that hasn't been told a log path stays
    # silent, so reads/exports never write logs.

    POST_LOG_HEADER = (
        'timestamp', 'action', 'post_id', 'post_title',
        'old_categories', 'new_categories', 'cats_added', 'cats_removed',
    )
    TERM_LOG_HEADER = (
        'timestamp', 'action', 'term_id', 'slug',
        'field', 'old_value', 'new_value',
    )
    RESTORE_LOG_HEADER = (
        'timestamp', 'kind', 'detail', 'status', 'error',
    )

    def set_logging(self, changes_log_path=None, terms_log_path=None):
        """
        Enable mutation logging to TSV files.

        Once set, every create/update/delete/set_post_categories/
        set_default_category call appends a row to the appropriate log.
        Pass None to either argument to leave that log disabled.

        Args:
            changes_log_path: Path for the post-change TSV log.
            terms_log_path: Path for the term-operation TSV log.
        """
        self.changes_log_path = changes_log_path
        self.terms_log_path = terms_log_path

    @contextmanager
    def _logging_suspended(self):
        """Suspend logging within a `with` block — used during restore."""
        saved_changes = self.changes_log_path
        saved_terms = self.terms_log_path
        self.changes_log_path = None
        self.terms_log_path = None
        try:
            yield
        finally:
            self.changes_log_path = saved_changes
            self.terms_log_path = saved_terms

    def _append_tsv(self, path, header, row):
        """
        Append a single row to a TSV log, writing the header on first use.

        Header-written state is cached in self._log_headers_written so we
        avoid two stat() calls per row at scale (apply runs touch
        thousands of rows). The cache is per-process; if the same path is
        appended to from a previous process, the header check still falls
        through to a single os.path.getsize() probe on the first call.
        """
        if path not in self._log_headers_written:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            try:
                already_has_header = os.path.getsize(path) > 0
            except FileNotFoundError:
                already_has_header = False
            with open(path, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f, delimiter='\t')
                if not already_has_header:
                    writer.writerow(header)
                writer.writerow(row)
            self._log_headers_written.add(path)
            return

        with open(path, 'a', newline='', encoding='utf-8') as f:
            csv.writer(f, delimiter='\t').writerow(row)

    @staticmethod
    def _log_timestamp():
        # Microsecond resolution so the restore agent can recover the
        # exact apply order even when many operations land in the same
        # second. Format sorts lexically.
        return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')

    def _log_term_op(self, action, term_id, slug, field, old_value, new_value):
        if not self.terms_log_path:
            return
        ts = self._log_timestamp()
        self._append_tsv(
            self.terms_log_path,
            self.TERM_LOG_HEADER,
            (ts, action, str(term_id), slug, field, old_value, new_value),
        )

    def _log_post_change(self, action, post_id, post_title,
                         old_categories, new_categories,
                         cats_added, cats_removed):
        if not self.changes_log_path:
            return
        ts = self._log_timestamp()
        self._append_tsv(
            self.changes_log_path,
            self.POST_LOG_HEADER,
            (
                ts, action, str(post_id), post_title,
                '|'.join(old_categories),
                '|'.join(new_categories),
                '|'.join(cats_added),
                '|'.join(cats_removed),
            ),
        )

    def _log_restore_op(self, path, op, status='ok', error=''):
        """Stream a single restore operation to the audit TSV."""
        ts = self._log_timestamp()
        kind = op.get('kind', '?')
        # Build a short detail string from whatever the op dict has.
        detail_parts = []
        for key in ('slug', 'post_id', 'restore_slug', 'field', 'restore_to'):
            val = op.get(key)
            if val is not None:
                detail_parts.append(f'{key}={val}')
        detail = '; '.join(detail_parts) or ''
        self._append_tsv(
            path,
            self.RESTORE_LOG_HEADER,
            (ts, kind, detail, status, str(error) if error else ''),
        )

    # --- Restore ---

    def restore(self, backup_path=None, changes_log_path=None,
                terms_log_path=None, mode=MODE_AUTO, dry_run=False,
                restore_log_path=None):
        """
        Revert a Taxonomist run.

        Args:
            backup_path: Path to backup-{timestamp}.json. Required for
                snapshot mode and used as the auto-mode fallback when
                no logs are present.
            changes_log_path: Path to the post change TSV (optional).
            terms_log_path: Path to the term-op TSV (optional).
            mode: MODE_AUTO (use logs if any present, else snapshot),
                MODE_LOGS (force log replay; error if no logs), or
                MODE_SNAPSHOT (force full backup replay).
            dry_run: If True, return planned operations without writing.
            restore_log_path: Path for the restore audit TSV. Each
                executed operation is streamed here as it completes so
                a crash leaves a durable record of what actually ran.
                Ignored during dry-run. Pass this instead of writing the
                log from agent code — the adapter enforces it so an
                agent that forgets the step can't produce a revert with
                no audit trail.

        Returns:
            Dict with keys: mode, operations, errors, partial, dry_run.
        """
        term_rows = _try_parse_log(terms_log_path, _parse_terms_tsv)
        change_rows = _try_parse_log(changes_log_path, _parse_changes_tsv)
        have_both_logs = bool(term_rows) and bool(change_rows)
        have_any_log = bool(term_rows) or bool(change_rows)

        rlp = restore_log_path if not dry_run else None

        if mode == MODE_LOGS:
            if not have_any_log:
                raise ValueError(
                    'restore mode=logs requires at least one of '
                    'changes_log_path or terms_log_path to exist'
                )
            result = self.restore_from_logs(
                change_rows, term_rows, dry_run=dry_run,
                restore_log_path=rlp,
            )
        elif mode == MODE_AUTO and have_both_logs:
            # Require BOTH logs for an inverse replay. A partial replay
            # (e.g., post assignments only) would leave orphaned created/
            # deleted categories behind — worse than a full snapshot undo.
            result = self.restore_from_logs(
                change_rows, term_rows, dry_run=dry_run,
                restore_log_path=rlp,
            )
        else:
            # Snapshot fallback.
            if not backup_path:
                raise ValueError(
                    'restore: no logs and no backup_path provided — '
                    'nothing to do'
                    if mode == MODE_AUTO
                    else 'restore mode=snapshot requires backup_path'
                )
            with open(backup_path, encoding='utf-8') as f:
                result = self.restore_from_snapshot(
                    json.load(f), dry_run=dry_run,
                    restore_log_path=rlp,
                )
            if mode == MODE_AUTO and have_any_log and not have_both_logs:
                present = 'terms' if term_rows else 'changes'
                missing = 'changes' if term_rows else 'terms'
                result['errors'].append({
                    'op': None,
                    'error': (
                        f'Only the {present} log was found ({missing} '
                        'log is missing). Fell back to full snapshot '
                        'restore to avoid a partial revert.'
                    ),
                })

        # Surface partial failures loudly. A caller who only checks for
        # exceptions must not believe a half-done revert succeeded.
        result['partial'] = bool(result['errors'])
        if result['partial'] and not dry_run:
            raise PartialRestoreError(result)
        return result

    def restore_from_logs(self, change_rows, term_rows, dry_run=False,
                          restore_log_path=None):
        """
        Inverse-replay change and term log rows in reverse-time order.

        Accepts already-parsed row lists (use parse_change_log /
        parse_terms_log from helpers.py to get them) so callers can
        massage them first if needed. See restore() for the contract.
        """
        # Tag each row with (timestamp, source-priority, position) so
        # ties within a second fall back to natural file order. Term ops
        # have priority 0 because they happen first in apply.md's order
        # (create → update → posts → delete); post changes have 1.
        # Sort ascending then reverse — that way ties also invert,
        # unlike sort(reverse=True) which preserves input order on ties.
        combined = [
            (r.get('timestamp', ''), 0, i, SOURCE_TERM, r)
            for i, r in enumerate(term_rows or [])
        ] + [
            (r.get('timestamp', ''), 1, i, SOURCE_CHANGE, r)
            for i, r in enumerate(change_rows or [])
        ]
        combined.sort(key=lambda x: (x[0], x[1], x[2]))
        combined.reverse()

        operations = []
        errors = []

        # Build a term_id→slug map from the log so we can resolve backup
        # parent IDs to live categories (e.g., when a deleted child
        # category's parent was also deleted and recreated with a new ID).
        id_to_slug = {}
        for r in (term_rows or []):
            tid = r.get('term_id')
            slug = r.get('slug')
            if tid and slug:
                id_to_slug[int(tid)] = slug

        with self._logging_suspended():
            for _ts, _pri, _pos, source, row in combined:
                try:
                    op = self._invert_op(source, row, dry_run, id_to_slug)
                    if op is not None:
                        operations.append(op)
                        if restore_log_path and not dry_run:
                            self._log_restore_op(
                                restore_log_path, op, status='ok',
                            )
                except (WpcomApiError, ValueError, KeyError, TypeError) as e:
                    err = {
                        'source': source,
                        'row': dict(row),
                        'error': str(e),
                    }
                    errors.append(err)
                    if restore_log_path and not dry_run:
                        self._log_restore_op(
                            restore_log_path,
                            {'kind': row.get('action', '?')},
                            status='error', error=str(e),
                        )

        return {
            'mode': MODE_LOGS,
            'operations': operations,
            'errors': errors,
            'partial': bool(errors),
            'dry_run': dry_run,
        }

    def _invert_op(self, source, row, dry_run, id_to_slug=None):
        """Invert a single logged operation. Returns a description dict."""
        action = row.get('action', '')

        if source == SOURCE_CHANGE and action == ACTION_SET_CATS:
            post_id = int(row.get('post_id') or 0)
            old_cat_names = [
                n for n in (row.get('old_categories') or '').split('|') if n
            ]
            op = {
                'kind': 'set_post_categories',
                'post_id': post_id,
                'restore_to': old_cat_names,
                'post_title': row.get('post_title', ''),
            }
            if dry_run:
                return op
            ids = []
            for name in old_cat_names:
                cat = self._lookup_category_by_name(name)
                if cat is None:
                    raise WpcomApiError(
                        404, 'not_found',
                        f'Cannot restore post {post_id}: category '
                        f'"{name}" no longer exists',
                    )
                ids.append(cat['ID'])
            self.set_post_categories(post_id, ids)
            return op

        if source == SOURCE_TERM and action == ACTION_CREATE_CAT:
            # Inverse of CREATE is DELETE. Look up by slug (term_id may
            # be stale if a previous inverse step recreated the term).
            slug = row.get('slug', '')
            cat = self._lookup_category_by_slug(slug)
            op = {
                'kind': 'delete_category',
                'slug': slug,
                'term_id': cat['ID'] if cat else None,
            }
            if cat is None:
                if not dry_run:
                    op['note'] = 'category already absent'
                return op
            if dry_run:
                return op
            self.delete_category(int(cat['ID']))
            err = self._verify_category_absent(slug)
            if err:
                op['verification'] = err
            return op

        if source == SOURCE_TERM and action == ACTION_DELETE_CAT:
            # Inverse of DELETE is CREATE, rehydrated from old_value JSON.
            try:
                snapshot = json.loads(row.get('old_value') or '{}')
            except json.JSONDecodeError as e:
                raise WpcomApiError(
                    400, 'invalid_log',
                    f'Could not parse DELETE_CAT snapshot: {e}',
                )
            op = {
                'kind': 'create_category',
                'name': snapshot.get('name'),
                'slug': snapshot.get('slug'),
                'description': snapshot.get('description', ''),
                'parent': snapshot.get('parent', 0),
            }
            if dry_run:
                return op
            # Idempotency: don't duplicate if revert is re-run.
            if self._lookup_category_by_slug(snapshot.get('slug', '')) is not None:
                op['note'] = 'category already present'
                return op
            result = self.create_category(
                snapshot.get('name', ''),
                snapshot.get('slug', ''),
                snapshot.get('description', ''),
            )
            err = self._verify_category_state(
                snapshot.get('slug', ''),
                {'name': snapshot.get('name', '')},
            )
            if err:
                op['verification'] = err
            # Restore parent-child relationship. The snapshot stores the
            # backup's term_id as parent, which may not match the live ID
            # (e.g., if the parent was also deleted and recreated). Resolve
            # through id_to_slug → live slug lookup, same approach as
            # restore_from_snapshot's hierarchy step.
            backup_parent_id = snapshot.get('parent', 0)
            if backup_parent_id and result.get('ID'):
                live_parent = None
                if id_to_slug:
                    parent_slug = id_to_slug.get(backup_parent_id)
                    if parent_slug:
                        live_parent = self._lookup_category_by_slug(parent_slug)
                # Fallback: the parent may still exist with its original ID.
                if live_parent is None:
                    live_parent = self._get_category_by_id(backup_parent_id)
                if live_parent is not None:
                    try:
                        self.update_category(
                            int(result['ID']),
                            {'parent': int(live_parent['ID'])},
                        )
                    except WpcomApiError:
                        op['note'] = (
                            f'parent {backup_parent_id} found but could '
                            'not be set'
                        )
                else:
                    op['note'] = (
                        f'parent {backup_parent_id} not found on live site'
                    )
            return op

        if source == SOURCE_TERM and action == ACTION_UPDATE_CAT:
            slug = row.get('slug', '')
            field = row.get('field', '')
            old_value = row.get('old_value', '')
            op = {
                'kind': 'update_category',
                'slug': slug,
                'field': field,
                'restore_to': old_value,
            }
            if dry_run:
                return op
            # term_id is stable across renames, so try it first. Fall
            # back to slug lookup (pre-change slug, then new_value for
            # slug-rename rows).
            term_id = int(row.get('term_id') or 0)
            cat = self._get_category_by_id(term_id) if term_id else None
            if cat is None:
                cat = self._lookup_category_by_slug(slug)
            if cat is None and field == 'slug':
                cat = self._lookup_category_by_slug(row.get('new_value', ''))
            if cat is None:
                raise WpcomApiError(
                    404, 'not_found',
                    f'Cannot revert UPDATE_CAT on slug "{slug}": '
                    'category not found on live site',
                )
            self.update_category(int(cat['ID']), {field: old_value})
            # For slug changes, verify against the restored slug; for
            # other fields, the slug column in the row is still valid.
            verify_slug = old_value if field == 'slug' else slug
            err = self._verify_category_state(verify_slug, {field: old_value})
            if err:
                op['verification'] = err
            return op

        if source == SOURCE_TERM and action == ACTION_SET_DEFAULT:
            # old_value is "id:slug"; prefer the slug since IDs may shift.
            old_value = row.get('old_value', '')
            old_slug = old_value.split(':', 1)[-1] if ':' in old_value else ''
            op = {
                'kind': 'set_default_category',
                'restore_slug': old_slug,
            }
            if dry_run:
                return op
            cat = self._lookup_category_by_slug(old_slug)
            if cat is None:
                raise WpcomApiError(
                    404, 'not_found',
                    f'Cannot restore default category to "{old_slug}": '
                    'category not found',
                )
            self.set_default_category(int(cat['ID']))
            return op

        return {'kind': 'unknown', 'source': source, 'action': action}

    def restore_from_snapshot(self, backup_data, dry_run=False,
                              restore_log_path=None):
        """
        Replay a full backup snapshot. Heavy-handed but works without logs.

        Mirrors the steps of lib/restore.php (categories without parents
        → reconcile descriptions → post assignments → default category →
        delete extras), but does NOT reproduce its known bugs: on slug
        collision we ABORT with a clear error rather than silently
        overwriting (lib/restore.php:90), and we never use temporary
        names that can leak on crash (lib/restore.php:109).

        Args:
            backup_data: Parsed backup JSON dict (from backup() output).
            dry_run: If True, plan operations without making any writes.

        Returns:
            Result dict matching restore_from_logs().
        """
        operations = []
        errors = []

        def execute(op, fn, *args, verify=None):
            """Run an op, verify the result, stream to restore log."""
            operations.append(op)
            if dry_run:
                return
            try:
                fn(*args)
            except WpcomApiError as e:
                errors.append({'op': op, 'error': str(e)})
                if restore_log_path:
                    self._log_restore_op(
                        restore_log_path, op,
                        status='error', error=str(e),
                    )
                return
            verify_err = verify() if verify else None
            if verify_err:
                errors.append({'op': op, 'error': verify_err})
            if restore_log_path:
                self._log_restore_op(
                    restore_log_path, op,
                    status='verify_failed' if verify_err else 'ok',
                    error=verify_err or '',
                )

        with self._logging_suspended():
            backup_cats = backup_data.get('categories', [])
            backup_posts = backup_data.get('post_categories', [])
            default_slug = backup_data.get('default_category_slug', '')

            live_cats = self.list_categories()
            live_by_slug = {c['slug']: c for c in live_cats}

            # Step 1: recreate any missing backup categories and rename
            # any whose name has drifted. Track whether we mutated so we
            # only refresh the cache when something actually changed.
            # Categories are created without parent first; hierarchy is
            # reconciled in step 2 after all categories exist (mirroring
            # the two-pass approach in lib/restore.php).
            cats_mutated = False
            for cat in backup_cats:
                slug = cat.get('slug', '')
                if not slug:
                    continue
                live = live_by_slug.get(slug)
                if live is None:
                    execute(
                        {'kind': 'create_category', 'slug': slug,
                         'name': cat.get('name', ''),
                         'description': cat.get('description', '')},
                        self.create_category,
                        cat.get('name', ''), slug, cat.get('description', ''),
                        verify=lambda s=slug: self._verify_category_state(
                            s, {'slug': s},
                        ),
                    )
                    cats_mutated = True
                elif live.get('name') != cat.get('name'):
                    execute(
                        {'kind': 'update_category', 'slug': slug,
                         'field': 'name', 'restore_to': cat.get('name', '')},
                        self.update_category,
                        int(live['ID']), {'name': cat.get('name', '')},
                        verify=lambda s=slug, n=cat.get('name', ''): (
                            self._verify_category_state(s, {'name': n})
                        ),
                    )
                    cats_mutated = True

            if cats_mutated and not dry_run:
                self._invalidate_category_cache()
                live_cats = self.list_categories()
                live_by_slug = {c['slug']: c for c in live_cats}

            # Step 2a: reconcile parent-child hierarchy. Backup parent
            # values are the backup's term_ids, not live IDs, so we
            # resolve via backup_id→slug→live_id.
            backup_id_to_slug = {
                c['term_id']: c['slug'] for c in backup_cats
                if c.get('term_id') and c.get('slug')
            }
            for cat in backup_cats:
                slug = cat.get('slug', '')
                live = live_by_slug.get(slug)
                if not live:
                    continue
                backup_parent_id = cat.get('parent', 0)
                if backup_parent_id:
                    parent_slug = backup_id_to_slug.get(backup_parent_id, '')
                    parent_live = live_by_slug.get(parent_slug)
                    desired_parent = int(parent_live['ID']) if parent_live else 0
                else:
                    desired_parent = 0
                if live.get('parent', 0) != desired_parent:
                    execute(
                        {'kind': 'update_category', 'slug': slug,
                         'field': 'parent', 'restore_to': desired_parent},
                        self.update_category,
                        int(live['ID']), {'parent': desired_parent},
                        verify=lambda s=slug, p=desired_parent: (
                            self._verify_category_state(s, {'parent': p})
                        ),
                    )

            # Step 2b: reconcile descriptions.
            for cat in backup_cats:
                slug = cat.get('slug', '')
                live = live_by_slug.get(slug)
                if not live:
                    continue
                desired_desc = cat.get('description', '')
                if (live.get('description') or '') != desired_desc:
                    execute(
                        {'kind': 'update_category', 'slug': slug,
                         'field': 'description', 'restore_to': desired_desc},
                        self.update_category,
                        int(live['ID']), {'description': desired_desc},
                        verify=lambda s=slug, d=desired_desc: (
                            self._verify_category_state(s, {'description': d})
                        ),
                    )

            # Step 3: restore post category assignments. Pre-collect any
            # slugs we don't see live and refresh the cache once before
            # the loop, instead of thrashing per-iteration.
            needed_slugs = {
                s
                for pc in backup_posts
                for s in (pc.get('category_slugs') or [])
            }
            if not dry_run and needed_slugs - live_by_slug.keys():
                self._invalidate_category_cache()
                live_cats = self.list_categories()
                live_by_slug = {c['slug']: c for c in live_cats}

            for pc in backup_posts:
                post_id = pc.get('post_id')
                slugs = pc.get('category_slugs', []) or []
                op = {
                    'kind': 'set_post_categories',
                    'post_id': post_id,
                    'restore_to': slugs,
                }
                operations.append(op)
                if dry_run:
                    continue
                try:
                    ids = []
                    for s in slugs:
                        live = live_by_slug.get(s)
                        if live is None:
                            raise WpcomApiError(
                                404, 'not_found',
                                f'Category slug "{s}" missing during restore',
                            )
                        ids.append(int(live['ID']))
                    if ids:
                        self.set_post_categories(int(post_id), ids)
                    if restore_log_path:
                        self._log_restore_op(
                            restore_log_path, op, status='ok',
                        )
                except WpcomApiError as e:
                    errors.append({'op': op, 'error': str(e)})
                    if restore_log_path:
                        self._log_restore_op(
                            restore_log_path, op,
                            status='error', error=str(e),
                        )

            # Step 4: restore default category.
            if default_slug:
                live = live_by_slug.get(default_slug)
                if live is not None:
                    execute(
                        {'kind': 'set_default_category',
                         'restore_slug': default_slug},
                        self.set_default_category,
                        int(live['ID']),
                    )

            # Step 5: delete categories that exist live but not in the
            # backup (created after the snapshot). Skip the default to
            # avoid orphaning posts.
            backup_slugs = {c.get('slug', '') for c in backup_cats}
            try:
                current_default = self.get_default_category()
            except WpcomApiError as e:
                if e.status_code == 404:
                    current_default = None
                else:
                    raise
            for live in live_cats:
                slug = live.get('slug', '')
                if not slug or slug in backup_slugs:
                    continue
                if current_default and live.get('ID') == current_default.get('ID'):
                    errors.append({
                        'op': {'kind': 'delete_category', 'slug': slug},
                        'error': 'refusing to delete current default category',
                    })
                    continue
                execute(
                    {'kind': 'delete_category', 'slug': slug},
                    self.delete_category,
                    int(live['ID']),
                    verify=lambda s=slug: self._verify_category_absent(s),
                )

        return {
            'mode': MODE_SNAPSHOT,
            'operations': operations,
            'errors': errors,
            'partial': bool(errors),
            'dry_run': dry_run,
        }


def _parse_changes_tsv(path):
    from helpers import parse_change_log
    return parse_change_log(path)


def _parse_terms_tsv(path):
    from helpers import parse_terms_log
    return parse_terms_log(path)


def _try_parse_log(path, parser):
    """
    Read a TSV log without an existence pre-check (TOCTOU-safe).

    Returns [] for any None path, missing file, or empty file.
    """
    if not path:
        return []
    try:
        return parser(path)
    except FileNotFoundError:
        return []
