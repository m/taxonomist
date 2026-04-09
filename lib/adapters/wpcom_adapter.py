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

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request


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

    def _get_category_by_id(self, term_id):
        """
        Look up a category by term_id.

        Uses cache first, refreshes once on miss to handle
        recently-created categories.

        Args:
            term_id: Integer category ID.

        Returns:
            Category dict, or None if not found.
        """
        for cat in self._ensure_category_cache():
            if cat['ID'] == term_id:
                return cat
        # Cache miss -- refresh and retry.
        self._invalidate_category_cache()
        for cat in self._ensure_category_cache():
            if cat['ID'] == term_id:
                return cat
        return None

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

    def set_post_categories(self, post_id, category_ids):
        """
        Set categories for a post by term IDs.

        Resolves IDs to names because the WP.com API accepts
        comma-separated category names, not IDs.

        Args:
            post_id: Integer post ID.
            category_ids: List of integer category IDs.

        Raises:
            WpcomApiError: If any category ID is not found.
        """
        names = []
        for cid in category_ids:
            cat = self._get_category_by_id(cid)
            if cat is None:
                raise WpcomApiError(
                    404, 'not_found', f'Category ID {cid} not found',
                )
            names.append(cat['name'])

        self._post(
            f'/sites/{self.site_id}/posts/{post_id}',
            data={'categories': ','.join(names)},
        )

    def create_category(self, name, slug, description=''):
        """
        Create a new category.

        Args:
            name: Display name.
            slug: URL slug.
            description: Category description.

        Returns:
            API response dict with the new category.
        """
        data = {'name': name}
        if slug:
            data['slug'] = slug
        if description:
            data['description'] = description

        result = self._post(f'/sites/{self.site_id}/categories/new', data=data)
        self._invalidate_category_cache()
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
        slug = urllib.parse.quote(cat['slug'], safe='')
        self._post(
            f'/sites/{self.site_id}/categories/slug:{slug}/delete',
        )
        self._invalidate_category_cache()

    # --- Extended interface ---

    def update_category(self, term_id, fields):
        """
        Update a category's fields.

        Always includes the current parent in the payload to prevent
        the WP.com API from silently creating a duplicate term at
        root level. Verifies term count before and after to detect
        duplicates. (Prevents issue #3.)

        Empty-string values are translated to a single NULL byte
        before posting because WP.com's v1.1 category endpoint
        silently ignores form-urlencoded empty strings — see the
        "empty-string clears" note below. After the update, the
        response is compared field-by-field against the caller's
        intent and raises on mismatch so silent no-ops become loud
        failures.

        Args:
            term_id: Integer category ID.
            fields: Dict of fields to update (description, name, etc.).

        Returns:
            API response dict.

        Raises:
            WpcomApiError: If category not found, a duplicate is
                detected, or the API silently fails to apply one of
                the requested field updates.
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

        # Always include parent to prevent silent duplicate creation.
        if 'parent' not in payload:
            payload['parent'] = current.get('parent', 0)

        # Empty-string clears: WP.com's v1.1 category update endpoint
        # treats a form-urlencoded empty string as "do not update this
        # field" rather than "clear it". Passing `description=''`
        # silently preserves the old value and the POST still reports
        # success. Empirically verified against a live site across
        # every payload shape I could think of (form/JSON, v1.1/v1.2);
        # the only form-urlencoded shape that actually clears a text
        # field is a single NULL byte. WP's input sanitization turns
        # `\x00` into an empty string on the server side, producing
        # the clear the caller asked for. Only substitute for string
        # values — integer fields like `parent=0` must be left alone.
        for key, value in list(payload.items()):
            if isinstance(value, str) and value == '':
                payload[key] = '\x00'

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

        # Verify-after-write. Compare the POST response against the
        # caller's original intent field-by-field. If any value
        # doesn't land (either because the NULL-byte substitution
        # stopped working, or because WP.com introduces a new silent
        # failure mode), raise instead of returning a success-shaped
        # dict that the caller will trust.
        for key, intended in fields.items():
            actual = result.get(key)
            if actual is None:
                actual = ''
            if str(actual) != str(intended):
                raise WpcomApiError(
                    500, 'update_no_op',
                    f'update_category(term_id={term_id}): field '
                    f'{key!r} was sent as {intended!r} but the API '
                    f'returned {actual!r} after the update',
                )

        self._invalidate_category_cache()
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
