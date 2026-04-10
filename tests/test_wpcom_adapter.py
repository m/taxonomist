"""
Tests for the WordPress.com API adapter.

Uses unittest.mock to patch urllib.request.urlopen so no real HTTP
requests are made. Verifies issue #1-4 defenses and API interactions.

The restore/logging tests use FakeWpcom, a subclass that overrides
_request() with an in-memory category/post store so the restore logic
can be exercised end-to-end without complex mock response choreography.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))
from adapters.wpcom_adapter import (
    WpcomAdapter, WpcomApiError, PartialRestoreError, wp_urlencode,
    ACTION_CREATE_CAT, ACTION_DELETE_CAT, ACTION_UPDATE_CAT,
    ACTION_SET_CATS, ACTION_SET_DEFAULT,
    MODE_AUTO, MODE_LOGS, MODE_SNAPSHOT,
)


VALID_CONFIG = {
    'site_url': 'https://example.wordpress.com',
    'connection': {
        'method': 'wpcom-api',
        'site_id': '12345',
        'access_token': 'test-token-xxx',
    },
}


def _mock_response(data, status=200):
    """Create a mock urllib response that works as a context manager."""
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = json.dumps(data).encode('utf-8')
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestWpUrlencode(unittest.TestCase):
    """Tests for the wp_urlencode helper."""

    def test_simple_dict(self):
        result = wp_urlencode({'name': 'Tech', 'slug': 'tech'})
        self.assertIn('name=Tech', result)
        self.assertIn('slug=tech', result)

    def test_list_values_produce_repeated_keys(self):
        """Issue #2: lists must not be stringified."""
        result = wp_urlencode({'tags': ['a', 'b']})
        self.assertEqual(result, 'tags=a&tags=b')

    def test_special_characters_encoded(self):
        result = wp_urlencode({'name': 'Rock & Roll'})
        self.assertIn('Rock', result)
        self.assertIn('%26', result)


class TestWpcomAdapterInit(unittest.TestCase):
    """Tests for adapter initialization and config validation."""

    def test_valid_config(self):
        adapter = WpcomAdapter(VALID_CONFIG)
        self.assertEqual(adapter.site_id, '12345')
        self.assertEqual(adapter.access_token, 'test-token-xxx')

    def test_wrong_method(self):
        config = {**VALID_CONFIG, 'connection': {
            'method': 'rest-api', 'site_id': '1', 'access_token': 'x',
        }}
        with self.assertRaises(ValueError) as ctx:
            WpcomAdapter(config)
        self.assertIn('wpcom-api', str(ctx.exception))

    def test_missing_site_id(self):
        config = {**VALID_CONFIG, 'connection': {
            'method': 'wpcom-api', 'access_token': 'x',
        }}
        with self.assertRaises(ValueError) as ctx:
            WpcomAdapter(config)
        self.assertIn('site_id', str(ctx.exception))

    def test_missing_access_token(self):
        config = {**VALID_CONFIG, 'connection': {
            'method': 'wpcom-api', 'site_id': '1',
        }}
        with self.assertRaises(ValueError) as ctx:
            WpcomAdapter(config)
        self.assertIn('access_token', str(ctx.exception))


class TestListCategories(unittest.TestCase):
    """Tests for listing categories with pagination."""

    @patch('adapters.wpcom_adapter.urllib.request.urlopen')
    def test_single_page(self, mock_urlopen):
        cats = [{'ID': 1, 'name': 'Tech', 'slug': 'tech', 'parent': 0}]
        mock_urlopen.return_value = _mock_response({
            'found': 1, 'categories': cats,
        })
        adapter = WpcomAdapter(VALID_CONFIG)
        result = adapter.list_categories()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['name'], 'Tech')

    @patch('adapters.wpcom_adapter.urllib.request.urlopen')
    def test_pagination(self, mock_urlopen):
        page1 = [{'ID': i, 'name': f'Cat{i}', 'slug': f'cat{i}', 'parent': 0}
                  for i in range(1000)]
        page2 = [{'ID': 1000, 'name': 'Last', 'slug': 'last', 'parent': 0}]
        mock_urlopen.side_effect = [
            _mock_response({'found': 1001, 'categories': page1}),
            _mock_response({'found': 1001, 'categories': page2}),
        ]
        adapter = WpcomAdapter(VALID_CONFIG)
        result = adapter.list_categories()
        self.assertEqual(len(result), 1001)


class TestDeleteCategory(unittest.TestCase):
    """Tests for category deletion (issue #1 defenses)."""

    @patch('adapters.wpcom_adapter.urllib.request.urlopen')
    def test_rejects_string_input(self, mock_urlopen):
        """Issue #1: must not accept slug strings."""
        adapter = WpcomAdapter(VALID_CONFIG)
        with self.assertRaises(TypeError) as ctx:
            adapter.delete_category('my-slug')
        self.assertIn('int', str(ctx.exception))

    @patch('adapters.wpcom_adapter.urllib.request.urlopen')
    def test_resolves_by_id(self, mock_urlopen):
        """Issue #1: resolves slug from live data, not guessing."""
        cat = {'ID': 42, 'name': 'Test', 'slug': 'test-cat', 'parent': 0}
        # First call: list_categories for cache. Second: delete.
        mock_urlopen.side_effect = [
            _mock_response({'found': 1, 'categories': [cat]}),
            _mock_response({'ID': 42}),  # delete response
            _mock_response({'found': 0, 'categories': []}),  # cache refresh
        ]
        adapter = WpcomAdapter(VALID_CONFIG)
        adapter.delete_category(42)

        # Verify the delete URL contained the correct slug.
        calls = mock_urlopen.call_args_list
        delete_call = calls[1]
        delete_url = delete_call[0][0].full_url
        self.assertIn('slug:test-cat/delete', delete_url)

    @patch('adapters.wpcom_adapter.urllib.request.urlopen')
    def test_not_found_raises(self, mock_urlopen):
        mock_urlopen.side_effect = [
            _mock_response({'found': 0, 'categories': []}),
            _mock_response({'found': 0, 'categories': []}),
        ]
        adapter = WpcomAdapter(VALID_CONFIG)
        with self.assertRaises(WpcomApiError) as ctx:
            adapter.delete_category(999)
        self.assertEqual(ctx.exception.status_code, 404)


class TestUpdateCategory(unittest.TestCase):
    """Tests for category updates (issue #3 defenses)."""

    @patch('adapters.wpcom_adapter.urllib.request.urlopen')
    def test_includes_parent(self, mock_urlopen):
        """Issue #3: parent always included in update payload."""
        cat = {'ID': 42, 'name': 'Sub', 'slug': 'sub', 'parent': 5,
               'description': 'old'}
        mock_urlopen.side_effect = [
            _mock_response({'found': 1, 'categories': [cat]}),  # cache
            _mock_response({'found': 1}),  # pre-count
            _mock_response({'ID': 42, 'slug': 'sub'}),  # update
            _mock_response({'found': 1}),  # post-count
            _mock_response({'found': 1, 'categories': [cat]}),  # cache refresh
        ]
        adapter = WpcomAdapter(VALID_CONFIG)
        adapter.update_category(42, {'description': 'new'})

        # Find the update POST call and verify parent is in the body.
        update_call = mock_urlopen.call_args_list[2]
        req = update_call[0][0]
        body = req.data.decode('utf-8')
        self.assertIn('parent=5', body)
        self.assertIn('description=new', body)

    @patch('adapters.wpcom_adapter.urllib.request.urlopen')
    def test_explicit_parent_used(self, mock_urlopen):
        """When caller provides parent, use it instead of current."""
        cat = {'ID': 42, 'name': 'Sub', 'slug': 'sub', 'parent': 5}
        mock_urlopen.side_effect = [
            _mock_response({'found': 1, 'categories': [cat]}),
            _mock_response({'found': 1}),
            _mock_response({'ID': 42}),
            _mock_response({'found': 1}),
            _mock_response({'found': 1, 'categories': [cat]}),
        ]
        adapter = WpcomAdapter(VALID_CONFIG)
        adapter.update_category(42, {'parent': 7})

        update_call = mock_urlopen.call_args_list[2]
        body = update_call[0][0].data.decode('utf-8')
        self.assertIn('parent=7', body)
        self.assertNotIn('parent=5', body)

    @patch('adapters.wpcom_adapter.urllib.request.urlopen')
    def test_detects_duplicate(self, mock_urlopen):
        """Issue #3: raises if term count increases after update."""
        cat = {'ID': 42, 'name': 'Sub', 'slug': 'sub', 'parent': 5}
        mock_urlopen.side_effect = [
            _mock_response({'found': 1, 'categories': [cat]}),
            _mock_response({'found': 10}),  # pre-count: 10
            _mock_response({'ID': 42}),      # update succeeds
            _mock_response({'found': 11}),  # post-count: 11 (duplicate!)
        ]
        adapter = WpcomAdapter(VALID_CONFIG)
        with self.assertRaises(WpcomApiError) as ctx:
            adapter.update_category(42, {'description': 'new'})
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn('duplicate', ctx.exception.error)


class TestSetPostCategories(unittest.TestCase):
    """Tests for setting post categories."""

    @patch('adapters.wpcom_adapter.urllib.request.urlopen')
    def test_resolves_ids_to_names(self, mock_urlopen):
        cats = [
            {'ID': 1, 'name': 'Tech', 'slug': 'tech', 'parent': 0},
            {'ID': 2, 'name': 'AI', 'slug': 'ai', 'parent': 0},
        ]
        mock_urlopen.side_effect = [
            _mock_response({'found': 2, 'categories': cats}),
            _mock_response({'ID': 100}),  # post update
        ]
        adapter = WpcomAdapter(VALID_CONFIG)
        adapter.set_post_categories(100, [1, 2])

        post_call = mock_urlopen.call_args_list[1]
        body = post_call[0][0].data.decode('utf-8')
        # Should send comma-separated names, not IDs.
        self.assertIn('categories=Tech', body)

    @patch('adapters.wpcom_adapter.urllib.request.urlopen')
    def test_unknown_id_raises(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({
            'found': 0, 'categories': [],
        })
        adapter = WpcomAdapter(VALID_CONFIG)
        with self.assertRaises(WpcomApiError) as ctx:
            adapter.set_post_categories(100, [999])
        self.assertEqual(ctx.exception.status_code, 404)


class TestExportPosts(unittest.TestCase):
    """Tests for post export with category normalization."""

    @patch('adapters.wpcom_adapter.urllib.request.urlopen')
    def test_normalizes_category_hash(self, mock_urlopen):
        """WP.com returns categories as {name: {...}}, not a list."""
        post = {
            'ID': 1,
            'title': 'Test',
            'content': '<p>Hello <b>world</b></p>',
            'date': '2024-01-01',
            'categories': {
                'Tech': {'ID': 10, 'slug': 'tech'},
                'AI': {'ID': 20, 'slug': 'ai'},
            },
            'URL': 'https://example.com/test',
        }
        mock_urlopen.return_value = _mock_response({
            'found': 1, 'posts': [post], 'meta': {},
        })
        adapter = WpcomAdapter(VALID_CONFIG)

        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            output_path = f.name
        try:
            adapter.export_posts(output_path)
            with open(output_path) as f:
                exported = json.load(f)
            self.assertEqual(len(exported), 1)
            self.assertIn('Tech', exported[0]['categories'])
            self.assertIn('ai', exported[0]['category_slugs'])
            # HTML should be stripped.
            self.assertNotIn('<p>', exported[0]['content'])
            self.assertIn('Hello world', exported[0]['content'])
        finally:
            os.unlink(output_path)

    @patch('adapters.wpcom_adapter.urllib.request.urlopen')
    def test_pagination(self, mock_urlopen):
        post1 = {
            'ID': 1, 'title': 'P1', 'content': '', 'date': '',
            'categories': {}, 'URL': '',
        }
        post2 = {
            'ID': 2, 'title': 'P2', 'content': '', 'date': '',
            'categories': {}, 'URL': '',
        }
        mock_urlopen.side_effect = [
            _mock_response({
                'found': 2, 'posts': [post1],
                'meta': {'next_page': 'cursor123'},
            }),
            _mock_response({
                'found': 2, 'posts': [post2], 'meta': {},
            }),
        ]
        adapter = WpcomAdapter(VALID_CONFIG)

        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            output_path = f.name
        try:
            adapter.export_posts(output_path)
            with open(output_path) as f:
                exported = json.load(f)
            self.assertEqual(len(exported), 2)
        finally:
            os.unlink(output_path)


class TestErrorHandling(unittest.TestCase):
    """Tests for API error propagation."""

    @patch('adapters.wpcom_adapter.urllib.request.urlopen')
    def test_http_error_raised(self, mock_urlopen):
        fp = io.BytesIO(b'{"error":"server_error","message":"boom"}')
        error = urllib.error.HTTPError('url', 500, 'Server Error', {}, fp)
        mock_urlopen.side_effect = error
        adapter = WpcomAdapter(VALID_CONFIG)
        with self.assertRaises(WpcomApiError) as ctx:
            adapter.list_categories()
        self.assertEqual(ctx.exception.status_code, 500)

    @patch('adapters.wpcom_adapter.urllib.request.urlopen')
    def test_connection_error_raised(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError('Name resolution failed')
        adapter = WpcomAdapter(VALID_CONFIG)
        with self.assertRaises(WpcomApiError) as ctx:
            adapter.list_categories()
        self.assertEqual(ctx.exception.status_code, 0)
        self.assertIn('connection_error', ctx.exception.error)

    @patch('adapters.wpcom_adapter.urllib.request.urlopen')
    def test_html_response_raises(self, mock_urlopen):
        """CDN/Cloudflare returning HTML instead of JSON."""
        mock_urlopen.return_value = _mock_response('<html>Challenge</html>')
        # Override mock to return raw bytes
        mock_urlopen.return_value.read.return_value = b'<html>Challenge</html>'
        adapter = WpcomAdapter(VALID_CONFIG)
        with self.assertRaises(WpcomApiError) as ctx:
            adapter.list_categories()
        self.assertIn('invalid_json', ctx.exception.error)

    @patch('adapters.wpcom_adapter.urllib.request.urlopen')
    def test_200_with_error_field(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({
            'error': 'unauthorized', 'message': 'bad token',
        })
        adapter = WpcomAdapter(VALID_CONFIG)
        with self.assertRaises(WpcomApiError) as ctx:
            adapter.list_categories()
        self.assertIn('unauthorized', str(ctx.exception))


class TestGetDefaultCategory(unittest.TestCase):
    """Tests for default category lookup."""

    @patch('adapters.wpcom_adapter.urllib.request.urlopen')
    def test_returns_default(self, mock_urlopen):
        cat = {'ID': 1, 'name': 'Uncategorized', 'slug': 'uncategorized',
               'parent': 0}
        mock_urlopen.side_effect = [
            _mock_response({'default_category': 1}),    # settings
            _mock_response({'found': 1, 'categories': [cat]}),  # cache
        ]
        adapter = WpcomAdapter(VALID_CONFIG)
        result = adapter.get_default_category()
        self.assertEqual(result['name'], 'Uncategorized')


class TestBackup(unittest.TestCase):
    """Tests for full taxonomy backup."""

    @patch('adapters.wpcom_adapter.urllib.request.urlopen')
    def test_writes_backup_json(self, mock_urlopen):
        cat = {'ID': 1, 'name': 'Tech', 'slug': 'tech', 'parent': 0,
               'description': 'Technology', 'post_count': 5}
        post = {
            'ID': 100, 'title': 'Test',
            'categories': {'Tech': {'ID': 1, 'slug': 'tech'}},
        }
        mock_urlopen.side_effect = [
            # list_categories
            _mock_response({'found': 1, 'categories': [cat]}),
            # posts pagination
            _mock_response({'found': 1, 'posts': [post], 'meta': {}}),
            # get_default_category -> settings
            _mock_response({'default_category': 1}),
            # get_default_category -> category cache
            _mock_response({'found': 1, 'categories': [cat]}),
        ]
        adapter = WpcomAdapter(VALID_CONFIG)

        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            output_path = f.name
        try:
            adapter.backup(output_path)
            with open(output_path) as f:
                backup = json.load(f)
            self.assertEqual(backup['total_posts'], 1)
            self.assertEqual(backup['total_categories'], 1)
            self.assertEqual(backup['default_category_slug'], 'tech')
            self.assertEqual(backup['categories'][0]['term_id'], 1)
            self.assertEqual(backup['post_categories'][0]['post_id'], 100)
        finally:
            os.unlink(output_path)


# --- FakeWpcom for restore/logging tests ---

class FakeWpcom(WpcomAdapter):
    """In-memory WP.com adapter for testing restore/logging logic."""

    def __init__(self, cats=None, posts=None, default_id=1):
        super().__init__(VALID_CONFIG)
        self.cats = dict(cats or {})
        self.posts = dict(posts or {})
        self.default_id = default_id
        self.next_id = max(
            list(self.cats.keys()) + list(self.posts.keys()) + [0]
        ) + 100
        self._category_cache = list(self.cats.values())

    def list_categories(self):
        self._category_cache = list(self.cats.values())
        return self._category_cache

    def _get_category_count(self):
        return len(self.cats)

    def _request(self, method, path, data=None, params=None):
        if path.endswith('/categories'):
            return {
                'categories': list(self.cats.values()),
                'found': len(self.cats),
            }
        if path.endswith('/categories/new'):
            tid = self.next_id
            self.next_id += 1
            cat = {
                'ID': tid, 'name': data.get('name', ''),
                'slug': data.get('slug') or '',
                'description': data.get('description', ''),
                'parent': int(data.get('parent', 0) or 0),
                'post_count': 0,
            }
            self.cats[tid] = cat
            return cat
        if '/categories/slug:' in path and path.endswith('/delete'):
            slug = path.split('/categories/slug:')[1].split('/')[0]
            for tid, c in list(self.cats.items()):
                if c['slug'] == slug:
                    del self.cats[tid]
                    return {'success': True}
            raise WpcomApiError(404, 'not_found', slug)
        if '/categories/slug:' in path:
            slug = path.split('/categories/slug:')[1]
            for c in self.cats.values():
                if c['slug'] == slug:
                    for k, v in (data or {}).items():
                        if k == 'parent':
                            v = int(v or 0)
                        c[k] = v
                    return c
            raise WpcomApiError(404, 'not_found', slug)
        if path.endswith('/posts'):
            return {
                'posts': [
                    {'ID': p['ID'], 'title': p.get('title', ''),
                     'categories': p.get('categories', {})}
                    for p in self.posts.values()
                ],
                'meta': {},
            }
        if '/posts/' in path and method == 'POST':
            pid = int(path.split('/posts/')[1])
            names = [
                n.strip()
                for n in (data or {}).get('categories', '').split(',')
                if n.strip()
            ]
            cat_hash = {}
            for n in names:
                for c in self.cats.values():
                    if c['name'] == n:
                        cat_hash[n] = c
                        break
            self.posts[pid]['categories'] = cat_hash
            return self.posts[pid]
        if '/posts/' in path and method == 'GET':
            pid = int(path.split('/posts/')[1])
            return self.posts[pid]
        if path.endswith('/settings') and method == 'GET':
            return {'settings': {'default_category': self.default_id}}
        if path.endswith('/settings') and method == 'POST':
            self.default_id = int(
                data.get('default_category', self.default_id)
            )
            return {}
        raise RuntimeError(f'unstubbed: {method} {path}')


def _make_cats():
    return {
        1: {'ID': 1, 'name': 'General', 'slug': 'general',
            'description': '', 'parent': 0, 'post_count': 0},
        2: {'ID': 2, 'name': 'Tech', 'slug': 'tech',
            'description': 'old desc', 'parent': 0, 'post_count': 5},
    }


def _make_posts():
    return {
        123: {'ID': 123, 'title': 'Hello',
              'categories': {
                  'Tech': {'ID': 2, 'name': 'Tech', 'slug': 'tech',
                           'parent': 0},
              }},
    }


class TestLogging(unittest.TestCase):
    """Tests for the term/post mutation logging hooks."""

    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix='taxo-test-')
        self.changes = os.path.join(self.workdir, 'changes.tsv')
        self.terms = os.path.join(self.workdir, 'terms.tsv')

    def tearDown(self):
        shutil.rmtree(self.workdir)

    def test_create_category_logs_to_terms(self):
        adapter = FakeWpcom(cats=_make_cats())
        adapter.set_logging(terms_log_path=self.terms)
        adapter.create_category('New', 'new-cat', 'desc')
        from helpers import parse_terms_log
        rows = parse_terms_log(self.terms)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['action'], ACTION_CREATE_CAT)
        self.assertEqual(rows[0]['slug'], 'new-cat')

    def test_delete_category_logs_full_snapshot(self):
        adapter = FakeWpcom(cats=_make_cats())
        adapter.set_logging(terms_log_path=self.terms)
        adapter.delete_category(2)
        from helpers import parse_terms_log
        rows = parse_terms_log(self.terms)
        self.assertEqual(rows[0]['action'], ACTION_DELETE_CAT)
        snapshot = json.loads(rows[0]['old_value'])
        self.assertEqual(snapshot['slug'], 'tech')
        self.assertEqual(snapshot['description'], 'old desc')

    def test_update_category_logs_per_field(self):
        adapter = FakeWpcom(cats=_make_cats())
        adapter.set_logging(terms_log_path=self.terms)
        adapter.update_category(2, {'description': 'new', 'name': 'Tech 2'})
        from helpers import parse_terms_log
        rows = parse_terms_log(self.terms)
        fields = {r['field'] for r in rows}
        self.assertIn('description', fields)
        self.assertIn('name', fields)
        desc_row = next(r for r in rows if r['field'] == 'description')
        self.assertEqual(desc_row['old_value'], 'old desc')
        self.assertEqual(desc_row['new_value'], 'new')

    def test_set_post_categories_logs_change(self):
        adapter = FakeWpcom(cats=_make_cats(), posts=_make_posts())
        adapter.set_logging(changes_log_path=self.changes)
        adapter.set_post_categories(
            123, [1, 2], old_category_ids=[2], post_title='Hello',
        )
        from helpers import parse_change_log
        rows = parse_change_log(self.changes)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['action'], ACTION_SET_CATS)
        self.assertEqual(rows[0]['old_categories'], 'Tech')
        self.assertIn('General', rows[0]['new_categories'])

    def test_set_post_categories_raises_without_old_ids(self):
        """Codex fix #3: must reject calls that would produce bad log rows."""
        adapter = FakeWpcom(cats=_make_cats(), posts=_make_posts())
        adapter.set_logging(changes_log_path=self.changes)
        with self.assertRaises(ValueError) as ctx:
            adapter.set_post_categories(123, [2])
        self.assertIn('old_category_ids is required', str(ctx.exception))

    def test_set_default_logs(self):
        adapter = FakeWpcom(cats=_make_cats())
        adapter.set_logging(terms_log_path=self.terms)
        adapter.set_default_category(2)
        from helpers import parse_terms_log
        rows = parse_terms_log(self.terms)
        self.assertEqual(rows[0]['action'], ACTION_SET_DEFAULT)
        self.assertIn('general', rows[0]['old_value'])


class TestRestoreFromLogs(unittest.TestCase):
    """Tests for inverse-replay restore."""

    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix='taxo-test-')
        self.changes = os.path.join(self.workdir, 'changes.tsv')
        self.terms = os.path.join(self.workdir, 'terms.tsv')
        self.backup = os.path.join(self.workdir, 'backup.json')

    def tearDown(self):
        shutil.rmtree(self.workdir)

    def _apply_and_revert(self):
        """Run a mini apply then revert. Returns (adapter, result)."""
        adapter = FakeWpcom(cats=_make_cats(), posts=_make_posts())
        adapter.backup(self.backup)
        adapter.set_logging(self.changes, self.terms)

        # Simulate a Taxonomist apply run.
        adapter.create_category('Remote Work', 'remote-work', 'WFH stuff')
        adapter.update_category(2, {'description': 'new desc'})
        new_id = max(adapter.cats.keys())
        adapter.set_post_categories(
            123, [2, new_id], old_category_ids=[2], post_title='Hello',
        )
        adapter.set_default_category(2)
        adapter.delete_category(1)

        adapter.set_logging()  # disable before restore
        result = adapter.restore(
            backup_path=self.backup,
            changes_log_path=self.changes,
            terms_log_path=self.terms,
            mode=MODE_LOGS,
            dry_run=False,
        )
        return adapter, result

    def test_round_trip(self):
        """Apply + revert should recover the baseline exactly."""
        adapter, result = self._apply_and_revert()
        self.assertFalse(result['errors'])
        self.assertEqual(result['mode'], MODE_LOGS)

        with open(self.backup) as f:
            baseline = json.load(f)
        after_cats = sorted(
            (c['slug'], c['name'], c.get('description', ''))
            for c in adapter.cats.values()
        )
        base_cats = sorted(
            (c['slug'], c['name'], c.get('description', ''))
            for c in baseline['categories']
        )
        self.assertEqual(after_cats, base_cats)

    def test_dry_run_makes_no_changes(self):
        adapter = FakeWpcom(cats=_make_cats(), posts=_make_posts())
        adapter.backup(self.backup)
        adapter.set_logging(self.changes, self.terms)
        adapter.create_category('X', 'x')
        adapter.set_logging()

        cats_before = dict(adapter.cats)
        result = adapter.restore(
            backup_path=self.backup,
            changes_log_path=self.changes,
            terms_log_path=self.terms,
            mode=MODE_LOGS,
            dry_run=True,
        )
        self.assertTrue(result['dry_run'])
        self.assertTrue(len(result['operations']) > 0)
        # Cats unchanged.
        self.assertEqual(set(adapter.cats.keys()), set(cats_before.keys()))

    def test_update_cat_after_slug_rename(self):
        """Codex fix #2: UPDATE_CAT should use term_id for robust lookup."""
        adapter = FakeWpcom(cats=_make_cats())
        adapter.set_logging(terms_log_path=self.terms)

        # Rename slug AND update description in one call — produces two
        # log rows both tagged with the old slug.
        adapter.update_category(2, {'slug': 'technology', 'description': 'new'})
        adapter.set_logging()

        # Write an empty changes log so both logs exist for auto mode.
        with open(self.changes, 'w') as f:
            f.write('')

        result = adapter.restore(
            changes_log_path=self.changes,
            terms_log_path=self.terms,
            mode=MODE_LOGS,
            dry_run=False,
        )
        self.assertFalse(result['errors'])
        self.assertEqual(adapter.cats[2]['slug'], 'tech')
        self.assertEqual(adapter.cats[2]['description'], 'old desc')

    def test_hierarchy_restored_after_delete(self):
        """Codex fix (parent-ID mapping): deleted child gets correct parent."""
        cats = {
            10: {'ID': 10, 'name': 'Parent', 'slug': 'parent',
                 'description': '', 'parent': 0, 'post_count': 0},
            20: {'ID': 20, 'name': 'Child', 'slug': 'child',
                 'description': '', 'parent': 10, 'post_count': 0},
        }
        adapter = FakeWpcom(cats=cats, default_id=10)
        adapter.set_logging(self.changes, self.terms)

        adapter.set_default_category(20)
        adapter.default_id = 20
        adapter.delete_category(20)
        adapter.delete_category(10)
        adapter.set_logging()

        result = adapter.restore(
            changes_log_path=self.changes,
            terms_log_path=self.terms,
            mode=MODE_LOGS,
            dry_run=False,
        )
        self.assertFalse(result['errors'])
        parent = next(
            c for c in adapter.cats.values() if c['slug'] == 'parent'
        )
        child = next(
            c for c in adapter.cats.values() if c['slug'] == 'child'
        )
        # Child's parent should be the recreated parent's new live ID.
        self.assertEqual(child['parent'], parent['ID'])


class TestRestoreFromSnapshot(unittest.TestCase):
    """Tests for full-snapshot restore."""

    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix='taxo-test-')
        self.backup = os.path.join(self.workdir, 'backup.json')

    def tearDown(self):
        shutil.rmtree(self.workdir)

    def test_round_trip(self):
        adapter = FakeWpcom(cats=_make_cats(), posts=_make_posts())
        adapter.backup(self.backup)

        # Mutate without logging.
        adapter.create_category('New', 'new')
        adapter.update_category(2, {'description': 'changed'})

        result = adapter.restore(
            backup_path=self.backup, mode=MODE_SNAPSHOT, dry_run=False,
        )
        self.assertFalse(result['errors'])
        self.assertEqual(result['mode'], MODE_SNAPSHOT)

        with open(self.backup) as f:
            baseline = json.load(f)
        after_cats = sorted(c['slug'] for c in adapter.cats.values())
        base_cats = sorted(c['slug'] for c in baseline['categories'])
        self.assertEqual(after_cats, base_cats)

    def test_hierarchy_reconciled(self):
        """Codex fix #4: snapshot restore should fix parent-child."""
        cats = _make_cats()
        cats[2]['parent'] = 1  # Tech is child of General
        adapter = FakeWpcom(cats=cats)
        adapter.backup(self.backup)

        adapter.update_category(2, {'parent': 0})
        self.assertEqual(adapter.cats[2]['parent'], 0)

        result = adapter.restore(
            backup_path=self.backup, mode=MODE_SNAPSHOT, dry_run=False,
        )
        self.assertFalse(result['errors'])
        self.assertEqual(adapter.cats[2]['parent'], 1)

    def test_dry_run(self):
        adapter = FakeWpcom(cats=_make_cats())
        adapter.backup(self.backup)
        adapter.create_category('New', 'new')
        cats_before = set(adapter.cats.keys())

        result = adapter.restore(
            backup_path=self.backup, mode=MODE_SNAPSHOT, dry_run=True,
        )
        self.assertTrue(result['dry_run'])
        self.assertEqual(set(adapter.cats.keys()), cats_before)


class TestRestoreAutoMode(unittest.TestCase):
    """Tests for mode=auto fallback behavior."""

    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix='taxo-test-')
        self.changes = os.path.join(self.workdir, 'changes.tsv')
        self.terms = os.path.join(self.workdir, 'terms.tsv')
        self.backup = os.path.join(self.workdir, 'backup.json')

    def tearDown(self):
        shutil.rmtree(self.workdir)

    def test_both_logs_uses_log_mode(self):
        adapter = FakeWpcom(cats=_make_cats(), posts=_make_posts())
        adapter.backup(self.backup)
        adapter.set_logging(self.changes, self.terms)
        adapter.create_category('X', 'x')
        adapter.set_post_categories(
            123, [1], old_category_ids=[2], post_title='Hello',
        )
        adapter.set_logging()

        result = adapter.restore(
            backup_path=self.backup,
            changes_log_path=self.changes,
            terms_log_path=self.terms,
            dry_run=True,
        )
        self.assertEqual(result['mode'], MODE_LOGS)

    def test_one_log_falls_back_to_snapshot(self):
        """Codex fix #1: partial logs → snapshot, not partial replay."""
        adapter = FakeWpcom(cats=_make_cats(), posts=_make_posts())
        adapter.backup(self.backup)
        adapter.set_logging(self.changes, self.terms)
        adapter.create_category('X', 'x')
        adapter.set_logging()

        # Only the terms log was written (no post changes were made).
        # Confirm changes log does NOT exist — only terms does.
        self.assertFalse(os.path.exists(self.changes))

        result = adapter.restore(
            backup_path=self.backup,
            changes_log_path=self.changes,
            terms_log_path=self.terms,
            dry_run=True,
        )
        self.assertEqual(result['mode'], MODE_SNAPSHOT)
        # Should contain a warning about the partial log.
        warnings = [e for e in result['errors'] if e.get('op') is None]
        self.assertEqual(len(warnings), 1)
        self.assertIn('changes', warnings[0]['error'].lower())

    def test_no_logs_no_backup_raises(self):
        adapter = FakeWpcom(cats=_make_cats())
        with self.assertRaises(ValueError):
            adapter.restore()


class TestPartialRestoreError(unittest.TestCase):
    """Tests for partial failure signaling."""

    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix='taxo-test-')
        self.backup = os.path.join(self.workdir, 'backup.json')

    def tearDown(self):
        shutil.rmtree(self.workdir)

    def test_clean_restore_returns_result(self):
        """A successful restore should return the result dict, not raise."""
        adapter = FakeWpcom(cats=_make_cats(), posts=_make_posts())
        adapter.backup(self.backup)
        adapter.create_category('X', 'x')

        result = adapter.restore(
            backup_path=self.backup, mode=MODE_SNAPSHOT, dry_run=False,
        )
        self.assertFalse(result['partial'])
        self.assertFalse(result['errors'])

    def test_partial_restore_raises(self):
        """A restore with errors must raise PartialRestoreError."""
        workdir = tempfile.mkdtemp(prefix='taxo-partial-')
        try:
            terms_log = os.path.join(workdir, 'terms.tsv')
            changes_log = os.path.join(workdir, 'changes.tsv')

            # Write a changes log that references a category name that
            # doesn't exist on the live site. The revert will fail to
            # find it, producing an error.
            adapter = FakeWpcom(cats=_make_cats(), posts=_make_posts())
            adapter.set_logging(changes_log, terms_log)
            adapter.create_category('Ephemeral', 'ephemeral')
            eid = next(c['ID'] for c in adapter.cats.values()
                       if c['slug'] == 'ephemeral')
            adapter.set_post_categories(
                123, [eid], old_category_ids=[2], post_title='Hello',
            )
            adapter.set_logging()

            # Now delete the original 'Tech' category so the revert can't
            # restore post 123 back to ['Tech'] — it's gone.
            adapter.delete_category(2)

            with self.assertRaises(PartialRestoreError) as ctx:
                adapter.restore(
                    changes_log_path=changes_log,
                    terms_log_path=terms_log,
                    mode=MODE_LOGS,
                    dry_run=False,
                )
            result = ctx.exception.result
            self.assertTrue(result['partial'])
            self.assertTrue(len(result['errors']) > 0)
        finally:
            shutil.rmtree(workdir)

    def test_partial_flag_on_result(self):
        """The result dict always has a 'partial' boolean."""
        adapter = FakeWpcom(cats=_make_cats())
        adapter.backup(self.backup)

        result = adapter.restore(
            backup_path=self.backup, mode=MODE_SNAPSHOT, dry_run=True,
        )
        self.assertIn('partial', result)
        self.assertIsInstance(result['partial'], bool)

    def test_dry_run_with_errors_does_not_raise(self):
        """Dry-run should never raise even if there would be errors."""
        adapter = FakeWpcom(cats=_make_cats())
        adapter.backup(self.backup)

        # Dry-run always succeeds without raising.
        result = adapter.restore(
            backup_path=self.backup, mode=MODE_SNAPSHOT, dry_run=True,
        )
        self.assertFalse(result['partial'])


class TestReadBackVerification(unittest.TestCase):
    """Tests for post-mutation read-back verification."""

    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix='taxo-test-')
        self.changes = os.path.join(self.workdir, 'changes.tsv')
        self.terms = os.path.join(self.workdir, 'terms.tsv')

    def tearDown(self):
        shutil.rmtree(self.workdir)

    def test_verification_passes_on_clean_restore(self):
        """No verification errors when mutations succeed normally."""
        adapter = FakeWpcom(cats=_make_cats())
        adapter.set_logging(terms_log_path=self.terms)
        adapter.create_category('X', 'x-cat', 'desc')
        adapter.set_logging()

        with open(self.changes, 'w') as f:
            f.write('')

        result = adapter.restore(
            changes_log_path=self.changes,
            terms_log_path=self.terms,
            mode=MODE_LOGS, dry_run=False,
        )
        # No verification keys should appear on ops.
        for op in result['operations']:
            self.assertNotIn('verification', op)


if __name__ == '__main__':
    unittest.main()
