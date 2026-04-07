"""
Tests for the WordPress.com API adapter.

Uses unittest.mock to patch urllib.request.urlopen so no real HTTP
requests are made. Verifies issue #1-4 defenses and API interactions.
"""

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))
from adapters.wpcom_adapter import WpcomAdapter, WpcomApiError, wp_urlencode


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


    @patch('adapters.wpcom_adapter.urllib.request.urlopen')
    def test_uses_v2_for_duplicate_slugs_delete(self, mock_urlopen):
        """Falls back to wp/v2 DELETE when slug is not unique."""
        cats = [
            {'ID': 42, 'name': 'Reviews', 'slug': 'reviews', 'parent': 0},
            {'ID': 99, 'name': 'Reviews', 'slug': 'reviews', 'parent': 5},
        ]
        mock_urlopen.side_effect = [
            _mock_response({'found': 2, 'categories': cats}),  # cache
            _mock_response({'deleted': True}),  # wp/v2 delete
        ]
        adapter = WpcomAdapter(VALID_CONFIG)
        adapter.delete_category(42)
        self.assertEqual(mock_urlopen.call_count, 2)

        v2_call = mock_urlopen.call_args_list[1]
        req = v2_call[0][0]
        self.assertIn('/wp/v2/', req.full_url)
        self.assertIn('/categories/42', req.full_url)
        self.assertEqual(req.get_method(), 'DELETE')


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

    @patch('adapters.wpcom_adapter.urllib.request.urlopen')
    def test_uses_v2_for_duplicate_slugs(self, mock_urlopen):
        """Falls back to wp/v2 when slug is not unique."""
        cats = [
            {'ID': 42, 'name': 'Reviews', 'slug': 'reviews', 'parent': 0},
            {'ID': 99, 'name': 'Reviews', 'slug': 'reviews', 'parent': 5},
        ]
        mock_urlopen.side_effect = [
            _mock_response({'found': 2, 'categories': cats}),  # cache
            _mock_response({'id': 42, 'slug': 'reviews'}),  # wp/v2 update
        ]
        adapter = WpcomAdapter(VALID_CONFIG)
        adapter.update_category(42, {'description': 'new'})
        self.assertEqual(mock_urlopen.call_count, 2)

        # The second call should be to wp/v2, not v1.1.
        v2_call = mock_urlopen.call_args_list[1]
        req = v2_call[0][0]
        self.assertIn('/wp/v2/', req.full_url)
        self.assertIn('/categories/42', req.full_url)
        # wp/v2 uses JSON, not form-encoded.
        self.assertEqual(req.get_header('Content-type'), 'application/json')

    @patch('adapters.wpcom_adapter.urllib.request.urlopen')
    def test_uses_v1_for_unique_slugs(self, mock_urlopen):
        """Uses v1.1 slug-based endpoint when no duplicate slugs."""
        cat = {'ID': 42, 'name': 'Tech', 'slug': 'tech', 'parent': 0}
        mock_urlopen.side_effect = [
            _mock_response({'found': 1, 'categories': [cat]}),  # cache
            _mock_response({'found': 1}),  # pre-count
            _mock_response({'ID': 42, 'slug': 'tech'}),  # v1.1 update
            _mock_response({'found': 1}),  # post-count
        ]
        adapter = WpcomAdapter(VALID_CONFIG)
        adapter.update_category(42, {'description': 'new'})
        self.assertEqual(mock_urlopen.call_count, 4)

        update_call = mock_urlopen.call_args_list[2]
        req = update_call[0][0]
        self.assertIn('/v1.1/', req.full_url)
        self.assertIn('slug:tech', req.full_url)

    def test_has_duplicate_slugs(self):
        """Detects when multiple categories share a slug."""
        adapter = WpcomAdapter(VALID_CONFIG)
        adapter._category_cache = [
            {'ID': 1, 'slug': 'reviews', 'parent': 0},
            {'ID': 2, 'slug': 'reviews', 'parent': 5},
            {'ID': 3, 'slug': 'tech', 'parent': 0},
        ]
        self.assertTrue(adapter._has_duplicate_slugs('reviews'))
        self.assertFalse(adapter._has_duplicate_slugs('tech'))
        self.assertFalse(adapter._has_duplicate_slugs('nonexistent'))


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


if __name__ == '__main__':
    unittest.main()
