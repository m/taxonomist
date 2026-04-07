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

    def _has_duplicate_slugs(self, slug):
        """Check if more than one category shares this slug."""
        count = sum(
            1 for c in self._ensure_category_cache() if c['slug'] == slug
        )
        return count > 1

    def _update_category_v2(self, term_id, fields):
        """
        Update a category using the wp/v2 endpoint (ID-based).

        Falls back to this when the v1.1 slug-based endpoint can't
        distinguish between categories with duplicate slugs.

        Args:
            term_id: Integer category ID.
            fields: Dict of fields to update.

        Returns:
            Parsed JSON response dict.

        Raises:
            WpcomApiError: On any API error.
        """
        url = (
            f'https://public-api.wordpress.com/wp/v2'
            f'/sites/{self.site_id}/categories/{term_id}'
        )
        body = json.dumps(fields).encode('utf-8')
        req = urllib.request.Request(url, data=body, method='POST')
        req.add_header('Authorization', f'Bearer {self.access_token}')
        req.add_header('User-Agent', 'taxonomist/1.0')
        req.add_header('Content-Type', 'application/json')

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            try:
                body_text = e.read().decode('utf-8', errors='replace')
            except Exception:
                body_text = str(e)
            try:
                err = json.loads(body_text)
                raise WpcomApiError(
                    e.code, err.get('code', 'unknown'),
                    err.get('message', body_text),
                ) from e
            except json.JSONDecodeError:
                raise WpcomApiError(e.code, 'http_error', body_text) from e
        except urllib.error.URLError as e:
            raise WpcomApiError(
                0, 'connection_error',
                f'Failed to connect to {url}: {e.reason}',
            ) from e

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

    def _delete_category_v2(self, term_id):
        """
        Delete a category using the wp/v2 endpoint (ID-based).

        Falls back to this when the v1.1 slug-based endpoint can't
        distinguish between categories with duplicate slugs.

        Args:
            term_id: Integer category ID.

        Raises:
            WpcomApiError: On any API error.
        """
        url = (
            f'https://public-api.wordpress.com/wp/v2'
            f'/sites/{self.site_id}/categories/{term_id}'
        )
        req = urllib.request.Request(url, method='DELETE')
        req.add_header('Authorization', f'Bearer {self.access_token}')
        req.add_header('User-Agent', 'taxonomist/1.0')
        req.add_header('Content-Type', 'application/json')

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            try:
                body_text = e.read().decode('utf-8', errors='replace')
            except Exception:
                body_text = str(e)
            try:
                err = json.loads(body_text)
                raise WpcomApiError(
                    e.code, err.get('code', 'unknown'),
                    err.get('message', body_text),
                ) from e
            except json.JSONDecodeError:
                raise WpcomApiError(e.code, 'http_error', body_text) from e
        except urllib.error.URLError as e:
            raise WpcomApiError(
                0, 'connection_error',
                f'Failed to connect to {url}: {e.reason}',
            ) from e

    def delete_category(self, term_id):
        """
        Delete a category by term ID.

        Always resolves the term_id to its slug from live data before
        deleting. Never guesses slugs. (Prevents issue #1.) When
        duplicate slugs exist, uses the wp/v2 ID-based endpoint.

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

        if self._has_duplicate_slugs(cat['slug']):
            self._delete_category_v2(term_id)
            self._invalidate_category_cache()
            return

        slug = urllib.parse.quote(cat['slug'], safe='')
        self._post(
            f'/sites/{self.site_id}/categories/slug:{slug}/delete',
        )
        self._invalidate_category_cache()

    # --- Extended interface ---

    def update_category(self, term_id, fields):
        """
        Update a category's fields.

        When the category's slug is unique, uses the v1.1 slug-based
        endpoint with parent pinning and duplicate detection (issue #3).
        When duplicate slugs exist, falls back to the wp/v2 ID-based
        endpoint to avoid updating the wrong category.

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

        # Always include parent to prevent silent duplicate creation.
        if 'parent' not in payload:
            payload['parent'] = current.get('parent', 0)

        # Use wp/v2 ID-based endpoint when duplicate slugs exist,
        # since the v1.1 slug-based endpoint can't distinguish them.
        # The v2 path skips the pre/post term-count check because
        # ID-based updates cannot create duplicate categories.
        if self._has_duplicate_slugs(current['slug']):
            result = self._update_category_v2(term_id, payload)
            self._invalidate_category_cache()
            return result

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
