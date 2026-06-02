"""
Tests for Taxonomist helper functions.

Covers batch splitting, result aggregation, data format validation,
and change log parsing. These test the local processing logic that
runs on the user's machine, independent of WordPress.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))
from helpers import (
    aggregate_results,
    batch_manifest_path,
    calculate_batch_size,
    compute_batch_fingerprint,
    find_incomplete_batches,
    parse_change_log,
    resolve_category_export_row,
    split_into_batches,
    validate_backup,
    validate_category_slugs,
    validate_export,
    validate_result_ids,
    validate_suggestions,
    wp_urlencode,
    write_batches,
)

# urllib.parse is used to round-trip wp_urlencode output through PHP-style
# parsing in the regression tests below.
from urllib.parse import parse_qs, parse_qsl, unquote


class TestSplitIntoBatches(unittest.TestCase):
    """Tests for splitting post lists into fixed-size batches."""

    def test_empty_list(self):
        self.assertEqual(split_into_batches([]), [])

    def test_single_batch(self):
        posts = [{'id': i} for i in range(50)]
        batches = split_into_batches(posts, batch_size=200)
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0]), 50)

    def test_exact_multiple(self):
        posts = [{'id': i} for i in range(400)]
        batches = split_into_batches(posts, batch_size=200)
        self.assertEqual(len(batches), 2)
        self.assertEqual(len(batches[0]), 200)
        self.assertEqual(len(batches[1]), 200)

    def test_remainder(self):
        posts = [{'id': i} for i in range(350)]
        batches = split_into_batches(posts, batch_size=200)
        self.assertEqual(len(batches), 2)
        self.assertEqual(len(batches[0]), 200)
        self.assertEqual(len(batches[1]), 150)

    def test_preserves_order(self):
        posts = [{'id': i} for i in range(5)]
        batches = split_into_batches(posts, batch_size=2)
        self.assertEqual(batches[0][0]['id'], 0)
        self.assertEqual(batches[0][1]['id'], 1)
        self.assertEqual(batches[1][0]['id'], 2)
        self.assertEqual(batches[2][0]['id'], 4)

    def test_batch_size_one(self):
        posts = [{'id': i} for i in range(3)]
        batches = split_into_batches(posts, batch_size=1)
        self.assertEqual(len(batches), 3)
        self.assertEqual(len(batches[0]), 1)


    def test_auto_batch_size(self):
        """When batch_size is None, it's calculated from content."""
        posts = [{'id': i, 'content': 'short'} for i in range(100)]
        batches = split_into_batches(posts)
        # Short posts should produce large batches.
        self.assertTrue(len(batches[0]) > 10)
        # All posts should be accounted for.
        total = sum(len(b) for b in batches)
        self.assertEqual(total, 100)


class TestCheckLargestBatch(unittest.TestCase):
    """Tests for checking the largest batch file in a directory."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.test_dir)

    def test_empty_dir(self):
        from helpers import check_largest_batch
        ok, largest, size = check_largest_batch(self.test_dir)
        self.assertTrue(ok)
        self.assertIsNone(largest)
        self.assertEqual(size, 0)

    def test_single_file(self):
        from helpers import check_largest_batch
        path = os.path.join(self.test_dir, 'batch-000.json')
        with open(path, 'w') as f:
            f.write('x' * 100)

        ok, largest, size = check_largest_batch(self.test_dir, max_chars=150)
        self.assertTrue(ok)
        self.assertEqual(largest, 'batch-000.json')
        self.assertEqual(size, 100)

    def test_multiple_files(self):
        from helpers import check_largest_batch
        for i, size in enumerate([50, 150, 100]):
            path = os.path.join(self.test_dir, f'batch-{i:03d}.json')
            with open(path, 'w') as f:
                f.write('x' * size)

        ok, largest, size = check_largest_batch(self.test_dir, max_chars=200)
        self.assertTrue(ok)
        self.assertEqual(largest, 'batch-001.json')
        self.assertEqual(size, 150)

    def test_exceeds_limit(self):
        from helpers import check_largest_batch
        path = os.path.join(self.test_dir, 'batch-000.json')
        with open(path, 'w') as f:
            f.write('x' * 300)

        ok, largest, size = check_largest_batch(self.test_dir, max_chars=200)
        self.assertFalse(ok)
        self.assertEqual(largest, 'batch-000.json')
        self.assertEqual(size, 300)


class TestMaxBatchTokensEnv(unittest.TestCase):
    """Tests for MAX_BATCH_TOKENS environment variable override."""

    def test_env_override(self):
        # We need to reload helpers to pick up the env var change
        # since it's set at the module level.
        import importlib
        import os

        import helpers

        original_val = os.environ.get('TAXONOMIST_MAX_BATCH_TOKENS')
        try:
            os.environ['TAXONOMIST_MAX_BATCH_TOKENS'] = '5000'
            importlib.reload(helpers)
            self.assertEqual(helpers.MAX_BATCH_TOKENS, 5000)
            self.assertEqual(helpers.MAX_BATCH_CHARS, 5000 * 4)
        finally:
            if original_val is None:
                del os.environ['TAXONOMIST_MAX_BATCH_TOKENS']
            else:
                os.environ['TAXONOMIST_MAX_BATCH_TOKENS'] = original_val
            importlib.reload(helpers)


class TestCalculateBatchSize(unittest.TestCase):
    """Tests for adaptive batch size calculation."""

    def test_empty_list(self):
        self.assertEqual(calculate_batch_size([]), 50)

    def test_short_posts_large_batches(self):
        posts = [{'id': i, 'title': 'Hi'} for i in range(100)]
        size = calculate_batch_size(posts)
        self.assertGreaterEqual(size, 100)

    def test_long_posts_small_batches(self):
        posts = [{'id': i, 'content': 'x' * 10000} for i in range(10)]
        size = calculate_batch_size(posts)
        self.assertLessEqual(size, 10)

    def test_minimum_batch_size(self):
        posts = [{'id': i, 'content': 'x' * 100000} for i in range(5)]
        size = calculate_batch_size(posts)
        self.assertGreaterEqual(size, 5)

    def test_maximum_batch_size(self):
        posts = [{'id': i} for i in range(500)]
        size = calculate_batch_size(posts)
        self.assertLessEqual(size, 200)


class TestWriteBatches(unittest.TestCase):
    """Tests for writing batch files to disk."""

    def test_creates_files(self):
        posts = [{'id': i, 'title': f'Post {i}'} for i in range(5)]
        with tempfile.TemporaryDirectory() as tmpdir:
            paths, batch_size = write_batches(posts, tmpdir, batch_size=2)
            self.assertEqual(len(paths), 3)
            self.assertTrue(all(os.path.exists(p) for p in paths))

    def test_file_naming(self):
        posts = [{'id': i} for i in range(5)]
        with tempfile.TemporaryDirectory() as tmpdir:
            paths, _ = write_batches(posts, tmpdir, batch_size=2)
            self.assertTrue(paths[0].endswith('batch-000.json'))
            self.assertTrue(paths[1].endswith('batch-001.json'))
            self.assertTrue(paths[2].endswith('batch-002.json'))

    def test_file_contents_valid_json(self):
        posts = [{'id': 1, 'title': 'Hello'}, {'id': 2, 'title': 'World'}]
        with tempfile.TemporaryDirectory() as tmpdir:
            paths, _ = write_batches(posts, tmpdir, batch_size=2)
            with open(paths[0]) as f:
                loaded = json.load(f)
            self.assertEqual(len(loaded), 2)
            self.assertEqual(loaded[0]['id'], 1)

    def test_returns_batch_size(self):
        posts = [{'id': i} for i in range(10)]
        with tempfile.TemporaryDirectory() as tmpdir:
            _, batch_size = write_batches(posts, tmpdir, batch_size=3)
            self.assertEqual(batch_size, 3)

    def test_creates_directory(self):
        posts = [{'id': 1}]
        with tempfile.TemporaryDirectory() as tmpdir:
            new_dir = os.path.join(tmpdir, 'nested', 'batches')
            write_batches(posts, new_dir, batch_size=10)
            self.assertTrue(os.path.isdir(new_dir))

    def test_removes_stale_batch_files(self):
        posts = [{'id': 1}, {'id': 2}]
        with tempfile.TemporaryDirectory() as tmpdir:
            stale = os.path.join(tmpdir, 'batch-001.json')
            with open(stale, 'w') as f:
                f.write('stale')
            paths, _ = write_batches(posts, tmpdir, batch_size=10)
            self.assertEqual(len(paths), 1)
            self.assertFalse(os.path.exists(stale))


class TestAggregateResults(unittest.TestCase):
    """Tests for combining per-batch result files."""

    def _write_result(self, tmpdir, name, data):
        path = os.path.join(tmpdir, name)
        with open(path, 'w') as f:
            json.dump(data, f)

    def test_combines_batches(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_result(tmpdir, 'result-000.json', [
                {'post_id': 1, 'cats': [10], 'new_cats': []},
                {'post_id': 2, 'cats': [20], 'new_cats': ['Jazz']},
            ])
            self._write_result(tmpdir, 'result-001.json', [
                {'post_id': 3, 'cats': [10, 30], 'new_cats': []},
            ])
            result = aggregate_results(tmpdir)
            suggestions = result['suggestions']
            cat_counts = result['category_counts']
            new_counts = result['new_category_counts']
            self.assertEqual(len(suggestions), 3)
            self.assertEqual(cat_counts[10], 2)
            self.assertEqual(cat_counts[20], 1)
            self.assertEqual(cat_counts[30], 1)
            self.assertEqual(new_counts['Jazz'], 1)

    def test_empty_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = aggregate_results(tmpdir)
            suggestions = result['suggestions']
            cat_counts = result['category_counts']
            self.assertEqual(len(suggestions), 0)
            self.assertEqual(len(cat_counts), 0)

    def test_ignores_non_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_result(tmpdir, 'result-000.json', [
                {'post_id': 1, 'cats': [10], 'new_cats': []},
            ])
            with open(os.path.join(tmpdir, 'notes.txt'), 'w') as f:
                f.write('ignore me')
            suggestions = aggregate_results(tmpdir)['suggestions']
            self.assertEqual(len(suggestions), 1)

    def test_ignores_non_result_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_result(tmpdir, 'result-000.json', [
                {'post_id': 1, 'cats': [10], 'new_cats': []},
            ])
            self._write_result(tmpdir, 'categories.json', [
                {'post_id': 99, 'cats': [99], 'new_cats': []},
            ])
            suggestions = aggregate_results(tmpdir)['suggestions']
            self.assertEqual(len(suggestions), 1)
            self.assertEqual(suggestions[0]['post_id'], 1)

    def test_dedupes_duplicate_post_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_result(tmpdir, 'result-000.json', [
                {'post_id': 1, 'cats': [10], 'new_cats': []},
            ])
            self._write_result(tmpdir, 'result-001.json', [
                {'post_id': 1, 'cats': [30], 'new_cats': ['ML']},
            ])
            result = aggregate_results(tmpdir)
            suggestions = result['suggestions']
            cat_counts = result['category_counts']
            new_counts = result['new_category_counts']
            self.assertEqual(len(suggestions), 1)
            self.assertEqual(suggestions[0]['cats'], [30])
            self.assertEqual(cat_counts[30], 1)
            self.assertNotIn(10, cat_counts)
            self.assertEqual(new_counts['ML'], 1)

    def test_sorted_file_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_result(tmpdir, 'result-001.json', [
                {'post_id': 99, 'cats': [99], 'new_cats': []},
            ])
            self._write_result(tmpdir, 'result-000.json', [
                {'post_id': 1, 'cats': [1], 'new_cats': []},
            ])
            suggestions = aggregate_results(tmpdir)['suggestions']
            # result-000 should come first due to sorted() filename order.
            self.assertEqual(suggestions[0]['post_id'], 1)
            self.assertEqual(suggestions[1]['post_id'], 99)


class TestValidateExport(unittest.TestCase):
    """Tests for export JSON format validation."""

    def test_valid_export(self):
        posts = [
            {
                'post_id': 1,
                'title': 'Test',
                'date': '2024-01-01 00:00:00',
                'content': 'Hello world',
                'categories': ['Tech'],
                'category_ids': [1],
                'category_slugs': ['tech'],
                'url': 'https://example.com/test',
            }
        ]
        result = validate_export(posts)
        self.assertTrue(result['valid'])
        self.assertEqual(result['errors'], [])

    def test_not_a_list(self):
        result = validate_export({'post_id': 1})
        self.assertFalse(result['valid'])
        self.assertEqual(len(result['errors']), 1)
        self.assertIn('JSON array', result['errors'][0])

    def test_missing_field(self):
        posts = [{'post_id': 1, 'title': 'Test'}]
        result = validate_export(posts)
        self.assertFalse(result['valid'])
        self.assertTrue(any('missing "date"' in e for e in result['errors']))
        self.assertTrue(any('missing "content"' in e for e in result['errors']))

    def test_wrong_type(self):
        posts = [
            {
                'post_id': 'not-an-int',
                'title': 'Test',
                'date': '2024-01-01',
                'content': 'Hello',
                'categories': ['Tech'],
                'category_ids': [1],
                'category_slugs': ['tech'],
                'url': 'https://example.com/test',
            }
        ]
        result = validate_export(posts)
        self.assertFalse(result['valid'])
        self.assertTrue(any('"post_id" should be int' in e for e in result['errors']))

    def test_empty_list_is_valid(self):
        result = validate_export([])
        self.assertTrue(result['valid'])
        self.assertEqual(result['errors'], [])

    def test_category_lists_must_contain_strings(self):
        posts = [
            {
                'post_id': 1,
                'title': 'Test',
                'date': '2024-01-01 00:00:00',
                'content': 'Hello',
                'categories': ['Tech', 5],
                'category_ids': [1],
                'category_slugs': ['tech'],
                'url': 'https://example.com/test',
            }
        ]
        result = validate_export(posts)
        self.assertFalse(result['valid'])
        self.assertTrue(
            any('"categories" must contain only strings' in e for e in result['errors'])
        )

    def test_category_ids_must_contain_ints(self):
        posts = [
            {
                'post_id': 1,
                'title': 'Test',
                'date': '2024-01-01 00:00:00',
                'content': 'Hello',
                'categories': ['Tech'],
                'category_ids': ['1'],
                'category_slugs': ['tech'],
                'url': 'https://example.com/test',
            }
        ]
        result = validate_export(posts)
        self.assertFalse(result['valid'])
        self.assertTrue(any('"category_ids" must contain only ints' in e for e in result['errors']))


class TestValidateSuggestions(unittest.TestCase):
    """Tests for suggestion JSON format validation."""

    def test_valid_suggestions(self):
        data = [
            {'post_id': 1, 'cats': [10], 'new_cats': []},
            {'post_id': 2, 'cats': [20, 30]},
        ]
        result = validate_suggestions(data)
        self.assertTrue(result['valid'])
        self.assertEqual(result['errors'], [])

    def test_missing_post_id(self):
        data = [{'cats': [10]}]
        result = validate_suggestions(data)
        self.assertFalse(result['valid'])
        self.assertTrue(any('missing "post_id"' in e for e in result['errors']))

    def test_missing_cats(self):
        data = [{'post_id': 1}]
        result = validate_suggestions(data)
        self.assertFalse(result['valid'])
        self.assertTrue(any('missing "cats"' in e for e in result['errors']))

    def test_cats_wrong_type(self):
        data = [{'post_id': 1, 'cats': 10}]
        result = validate_suggestions(data)
        self.assertFalse(result['valid'])
        self.assertTrue(any('"cats" must be list' in e for e in result['errors']))

    def test_cats_entries_must_be_ints(self):
        data = [{'post_id': 1, 'cats': [10, '7']}]
        result = validate_suggestions(data)
        self.assertFalse(result['valid'])
        self.assertTrue(any('"cats" must contain only ints' in e for e in result['errors']))

    def test_new_cats_entries_must_be_strings(self):
        data = [{'post_id': 1, 'cats': [10], 'new_cats': ['ml', 7]}]
        result = validate_suggestions(data)
        self.assertFalse(result['valid'])
        self.assertTrue(any('"new_cats" must contain only strings' in e for e in result['errors']))


class TestValidateBackup(unittest.TestCase):
    """Tests for backup JSON format validation."""

    def test_valid_backup(self):
        backup = {
            'timestamp': '2024-01-01 00:00:00',
            'site_url': 'https://example.com',
            'total_posts': 100,
            'total_categories': 10,
            'default_category_slug': 'uncategorized',
            'categories': [
                {
                    'term_id': 1,
                    'name': 'Tech',
                    'slug': 'tech',
                    'description': '',
                    'count': 5,
                    'parent': 0,
                }
            ],
            'post_categories': [
                {
                    'post_id': 1,
                    'post_title': 'Test',
                    'category_ids': [1],
                    'category_slugs': ['tech'],
                }
            ],
        }
        result = validate_backup(backup)
        self.assertTrue(result['valid'])
        self.assertEqual(result['errors'], [])

    def test_not_a_dict(self):
        result = validate_backup([])
        self.assertFalse(result['valid'])
        self.assertIn('Backup must be a JSON object', result['errors'])

    def test_missing_top_level_keys(self):
        result = validate_backup({})
        self.assertFalse(result['valid'])
        self.assertTrue(any('timestamp' in e for e in result['errors']))
        self.assertTrue(any('categories' in e for e in result['errors']))
        self.assertTrue(any('default_category_slug' in e for e in result['errors']))

    def test_missing_category_fields(self):
        backup = {
            'timestamp': '', 'site_url': '', 'total_posts': 0,
            'total_categories': 0,
            'categories': [{'name': 'Tech'}],
            'post_categories': [],
        }
        result = validate_backup(backup)
        self.assertFalse(result['valid'])
        self.assertTrue(any('missing "term_id"' in e for e in result['errors']))
        self.assertTrue(any('missing "slug"' in e for e in result['errors']))

    def test_missing_post_mapping_fields(self):
        backup = {
            'timestamp': '', 'site_url': '', 'total_posts': 0,
            'total_categories': 0,
            'categories': [],
            'post_categories': [{'post_id': 1}],
        }
        result = validate_backup(backup)
        self.assertFalse(result['valid'])
        self.assertTrue(any('missing "category_slugs"' in e for e in result['errors']))


class TestResolveCategoryExportRow(unittest.TestCase):
    """Tests for exact category resolution from exported metadata."""

    def setUp(self):
        self.categories = [
            {
                'term_id': 101,
                'name': 'Files & Subscriptions',
                'slug': 'files-subscriptions-ios',
                'description': '',
                'count': 0,
                'parent': 10,
            },
            {
                'term_id': 202,
                'name': 'Files & Subscriptions',
                'slug': 'files-subscriptions-android',
                'description': '',
                'count': 1,
                'parent': 20,
            },
        ]

    def test_resolves_by_term_id(self):
        category = resolve_category_export_row(self.categories, term_id=101)
        self.assertEqual(category['slug'], 'files-subscriptions-ios')

    def test_resolves_by_exact_slug(self):
        category = resolve_category_export_row(
            self.categories,
            slug='files-subscriptions-android',
        )
        self.assertEqual(category['term_id'], 202)

    def test_rejects_name_only_lookup(self):
        with self.assertRaisesRegex(ValueError, 'name alone'):
            resolve_category_export_row(
                self.categories,
                name='Files & Subscriptions',
            )

    def test_rejects_mismatched_term_id_and_slug(self):
        with self.assertRaisesRegex(ValueError, 'different categories'):
            resolve_category_export_row(
                self.categories,
                term_id=101,
                slug='files-subscriptions-android',
            )

    def test_rejects_duplicate_slug(self):
        categories = self.categories + [
            {
                'term_id': 303,
                'name': 'Duplicate',
                'slug': 'files-subscriptions-ios',
                'description': '',
                'count': 0,
                'parent': 0,
            }
        ]
        with self.assertRaisesRegex(ValueError, 'Duplicate slug'):
            resolve_category_export_row(categories, slug='files-subscriptions-ios')


class TestWpUrlencode(unittest.TestCase):
    """Tests for the wp_urlencode helper.

    The encoder must produce output that PHP's parse_str / WordPress can
    decode back into the original nested structure. We assert on both the
    raw encoded form and on a round-trip through urllib's parse_qsl, which
    matches PHP's behaviour for repeated keys.
    """

    def test_flat_string_value(self):
        self.assertEqual(wp_urlencode({'name': 'Hello World'}), 'name=Hello+World')

    def test_nested_dict_with_list_encodes_repeated_keys(self):
        # The exact case from issue #2: nested dict containing a list of
        # strings must produce repeated terms[kb_category][] keys, not a
        # single stringified-list value.
        encoded = wp_urlencode({'terms': {'kb_category': ['General', 'Settings']}})

        # Brackets are URL-encoded; decoding the key reveals the PHP form.
        decoded_pairs = [(unquote(k), v) for k, v in parse_qsl(encoded)]
        self.assertEqual(
            decoded_pairs,
            [
                ('terms[kb_category][]', 'General'),
                ('terms[kb_category][]', 'Settings'),
            ],
        )

    def test_brackets_are_url_encoded(self):
        # Sanity check: the raw output uses %5B / %5D for [ and ].
        # PHP/WordPress accepts both forms, but the encoded form is what
        # urllib emits and what callers should expect to see on the wire.
        encoded = wp_urlencode({'terms': {'kb_category': ['x']}})
        self.assertIn('%5B', encoded)
        self.assertIn('%5D', encoded)
        self.assertNotIn('[', encoded)
        self.assertNotIn(']', encoded)

    def test_top_level_list(self):
        self.assertEqual(
            wp_urlencode({'tags': ['a', 'b', 'c']}),
            'tags%5B%5D=a&tags%5B%5D=b&tags%5B%5D=c',
        )

    def test_nested_list_of_dicts(self):
        # This is the case commit 3bcd2d8 claims to fix: a list whose
        # elements are themselves dicts. Each dict's keys must be appended
        # under [] so PHP receives a list of associative arrays.
        encoded = wp_urlencode({'items': [{'name': 'foo', 'qty': 1}, {'name': 'bar', 'qty': 2}]})
        decoded_pairs = [(unquote(k), v) for k, v in parse_qsl(encoded)]
        self.assertEqual(
            decoded_pairs,
            [
                ('items[][name]', 'foo'),
                ('items[][qty]', '1'),
                ('items[][name]', 'bar'),
                ('items[][qty]', '2'),
            ],
        )

    def test_empty_list_omits_key(self):
        # Documented behaviour: empty lists produce no output for that key.
        # If this changes, callers relying on "no key means no change"
        # need to know.
        self.assertEqual(wp_urlencode({'foo': []}), '')

    def test_empty_dict_omits_key(self):
        self.assertEqual(wp_urlencode({'foo': {}}), '')

    def test_empty_input(self):
        self.assertEqual(wp_urlencode({}), '')

    def test_none_value_is_stringified(self):
        # Documented behaviour: scalars are passed through str(). None
        # becomes the literal string 'None'. Callers should not pass None
        # if they mean "omit this field".
        self.assertEqual(wp_urlencode({'foo': None}), 'foo=None')

    def test_bool_values_are_stringified(self):
        # Booleans become 'True' / 'False' literals. PHP will receive
        # them as strings, not as PHP booleans. Callers wanting 1/0 must
        # convert before calling wp_urlencode.
        self.assertEqual(wp_urlencode({'on': True, 'off': False}), 'on=True&off=False')

    def test_integer_values_are_stringified(self):
        self.assertEqual(wp_urlencode({'id': 42}), 'id=42')

    def test_special_characters_are_escaped(self):
        encoded = wp_urlencode({'q': 'a&b=c d'})
        # Ampersand, equals, and space must all be escaped so PHP sees
        # them as part of the value, not as query separators.
        self.assertEqual(parse_qs(encoded), {'q': ['a&b=c d']})

    def test_rejects_list_input(self):
        # Top-level lists would produce malformed `[]=value` pairs with
        # no key. Fail loudly instead.
        with self.assertRaisesRegex(TypeError, r'expects a dict, got list'):
            wp_urlencode(['a', 'b'])

    def test_rejects_tuple_input(self):
        with self.assertRaisesRegex(TypeError, r'expects a dict, got tuple'):
            wp_urlencode(('a', 'b'))

    def test_rejects_string_input(self):
        # A string would be iterated character-by-character by the
        # flatten() helper and produce nonsense. Reject it.
        with self.assertRaisesRegex(TypeError, r'expects a dict, got str'):
            wp_urlencode('key=value')

    def test_rejects_none_input(self):
        with self.assertRaisesRegex(TypeError, r'expects a dict, got NoneType'):
            wp_urlencode(None)


class TestWpUrlencodeIssue2Regression(unittest.TestCase):
    """Regression tests for issue #2.

    The original bug: passing a list value through urllib.parse.urlencode
    serialized the list as its Python repr (e.g. "['General', 'Settings']"),
    which WordPress then interpreted as a literal category name and
    created a junk category. wp_urlencode must produce the repeated-key
    form so WordPress assigns the intended terms.
    """

    def test_does_not_stringify_list_as_python_repr(self):
        # The exact failure mode from the issue: the encoded output must
        # NOT contain the Python list repr.
        encoded = wp_urlencode({'terms': {'kb_category': ['General', 'Settings']}})
        self.assertNotIn("%5B%27", encoded)  # %5B%27 = [' (start of repr)
        self.assertNotIn("%27%5D", encoded)  # %27%5D = '] (end of repr)
        self.assertNotIn("'General'", encoded)
        self.assertNotIn("'Settings'", encoded)

    def test_produces_repeated_key_form_from_issue(self):
        # The "expected behaviour" form documented in issue #2:
        #   terms[kb_category][]=General&terms[kb_category][]=Settings
        encoded = wp_urlencode({'terms': {'kb_category': ['General', 'Settings']}})
        decoded = unquote(encoded)
        self.assertEqual(
            decoded,
            'terms[kb_category][]=General&terms[kb_category][]=Settings',
        )

    def test_php_style_round_trip_preserves_list(self):
        # parse_qs groups repeated keys into lists, matching how PHP's
        # parse_str populates terms[kb_category] as an array. If this
        # round-trip ever degrades to a single-value string, the issue
        # has regressed.
        encoded = wp_urlencode({'terms': {'kb_category': ['General', 'Settings']}})
        parsed = parse_qs(encoded)
        # Decode the bracket-encoded key for assertion clarity.
        decoded_keys = {unquote(k): v for k, v in parsed.items()}
        self.assertEqual(decoded_keys, {'terms[kb_category][]': ['General', 'Settings']})

    def test_naive_urlencode_demonstrates_the_bug(self):
        # Sanity check that the bug is real: a naive urllib.parse.urlencode
        # call on the same input produces the broken stringified form.
        # This guards against someone "simplifying" wp_urlencode back to
        # a thin urlencode wrapper.
        from urllib.parse import urlencode
        broken = urlencode({'terms[kb_category][]': ['General', 'Settings']})
        self.assertIn("%27", broken)  # contains a quote = stringified repr


class TestParseChangeLog(unittest.TestCase):
    """Tests for parsing TSV change log files."""

    def test_parses_log(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.tsv', delete=False, newline='') as f:
            f.write(
                "timestamp\taction\tpost_id\tpost_title\told_categories\t"
                "new_categories\tcats_added\tcats_removed\n"
            )
            f.write(
                "2024-01-01 00:00:00\tSET_CATS\t123\tTest Post\tAsides\t"
                "Tech|AI\tTech|AI\tAsides\n"
            )
            f.write(
                "2024-01-01 00:00:01\tSET_CATS\t456\tOther Post\tAsides\t"
                "Music\tMusic\tAsides\n"
            )
            path = f.name

        try:
            changes = parse_change_log(path)
            self.assertEqual(len(changes), 2)
            self.assertEqual(changes[0]['post_id'], '123')
            self.assertEqual(changes[0]['action'], 'SET_CATS')
            self.assertEqual(changes[0]['cats_added'], 'Tech|AI')
            self.assertEqual(changes[1]['post_title'], 'Other Post')
        finally:
            os.unlink(path)

    def test_parses_quoted_tabs_and_newlines(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.tsv', delete=False, newline='') as f:
            f.write("timestamp\taction\tpost_id\tpost_title\told_categories\tnew_categories\tcats_added\tcats_removed\n")
            import csv
            writer = csv.writer(f, delimiter='\t', quoting=csv.QUOTE_MINIMAL)
            writer.writerow([
                '2024-01-01 00:00:00', 'SET_CATS', '123', 'Title with\ttab',
                'Old', 'New\nWrapped', 'Tech|AI', 'Asides'
            ])
            path = f.name

        try:
            changes = parse_change_log(path)
            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0]['post_title'], 'Title with\ttab')
            self.assertEqual(changes[0]['new_categories'], 'New\nWrapped')
        finally:
            os.unlink(path)

    def test_empty_log(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.tsv', delete=False) as f:
            f.write("timestamp\taction\tpost_id\tpost_title\told_categories\tnew_categories\tcats_added\tcats_removed\n")
            path = f.name

        try:
            changes = parse_change_log(path)
            self.assertEqual(len(changes), 0)
        finally:
            os.unlink(path)


class TestComputeBatchFingerprint(unittest.TestCase):
    """Tests for batch fingerprint computation."""

    def test_same_posts_same_fingerprint(self):
        posts = [{'post_id': 1}, {'post_id': 2}, {'post_id': 3}]
        self.assertEqual(
            compute_batch_fingerprint(posts),
            compute_batch_fingerprint(posts),
        )

    def test_different_order_same_fingerprint(self):
        posts_a = [{'post_id': 3}, {'post_id': 1}, {'post_id': 2}]
        posts_b = [{'post_id': 1}, {'post_id': 2}, {'post_id': 3}]
        self.assertEqual(
            compute_batch_fingerprint(posts_a),
            compute_batch_fingerprint(posts_b),
        )

    def test_different_posts_different_fingerprint(self):
        posts_a = [{'post_id': 1}, {'post_id': 2}]
        posts_b = [{'post_id': 1}, {'post_id': 3}]
        self.assertNotEqual(
            compute_batch_fingerprint(posts_a),
            compute_batch_fingerprint(posts_b),
        )

    def test_empty_list_consistent(self):
        self.assertEqual(
            compute_batch_fingerprint([]),
            compute_batch_fingerprint([]),
        )

    def test_returns_hex_string(self):
        fp = compute_batch_fingerprint([{'post_id': 1}])
        self.assertIsInstance(fp, str)
        self.assertEqual(len(fp), 64)  # SHA-256 hex digest


class TestWriteBatchesResume(unittest.TestCase):
    """Tests for write_batches resume functionality."""

    def _make_posts(self, n):
        return [{'post_id': i, 'title': f'Post {i}'} for i in range(n)]

    def test_default_writes_manifest(self):
        posts = self._make_posts(5)
        with tempfile.TemporaryDirectory() as tmpdir:
            write_batches(posts, tmpdir, batch_size=2)
            manifest_file = batch_manifest_path(tmpdir)
            self.assertTrue(os.path.exists(manifest_file))
            with open(manifest_file) as f:
                manifest = json.load(f)
            self.assertIn('fingerprint', manifest)
            self.assertEqual(manifest['batch_size'], 2)
            self.assertEqual(manifest['num_batches'], 3)

    def test_resume_reuses_unchanged_batches(self):
        posts = self._make_posts(4)
        with tempfile.TemporaryDirectory() as tmpdir:
            # First write.
            paths1, size1 = write_batches(posts, tmpdir, batch_size=2)
            # Record modification times.
            mtimes = {p: os.path.getmtime(p) for p in paths1}

            # Tiny delay to ensure mtime would differ if rewritten.
            import time
            time.sleep(0.05)

            # Resume with same posts — should reuse.
            paths2, size2 = write_batches(posts, tmpdir, batch_size=2, resume=True)
            self.assertEqual(paths1, paths2)
            self.assertEqual(size1, size2)
            # Files should NOT have been rewritten.
            for p in paths2:
                self.assertEqual(os.path.getmtime(p), mtimes[p])

    def test_resume_rewrites_when_posts_change(self):
        posts_v1 = self._make_posts(4)
        posts_v2 = self._make_posts(6)
        with tempfile.TemporaryDirectory() as tmpdir:
            write_batches(posts_v1, tmpdir, batch_size=2)
            paths, _ = write_batches(posts_v2, tmpdir, batch_size=2, resume=True)
            # Should have rewritten with 3 batches (6 posts / 2).
            self.assertEqual(len(paths), 3)

    def test_resume_without_manifest_writes_fresh(self):
        posts = self._make_posts(4)
        with tempfile.TemporaryDirectory() as tmpdir:
            paths, _ = write_batches(posts, tmpdir, batch_size=2, resume=True)
            self.assertEqual(len(paths), 2)
            self.assertTrue(os.path.exists(batch_manifest_path(tmpdir)))

    def test_no_resume_clears_old_batches(self):
        """Default (resume=False) still clears stale files."""
        posts = self._make_posts(2)
        with tempfile.TemporaryDirectory() as tmpdir:
            # Write 3 batches first.
            write_batches(self._make_posts(6), tmpdir, batch_size=2)
            # Rewrite with fewer posts, no resume.
            paths, _ = write_batches(posts, tmpdir, batch_size=2)
            self.assertEqual(len(paths), 1)
            # Old batch files should be gone (exclude manifest).
            remaining = [f for f in os.listdir(tmpdir)
                         if f.startswith('batch-') and f.endswith('.json')
                         and f != 'batch-manifest.json']
            self.assertEqual(len(remaining), 1)


class TestFindIncompleteBatches(unittest.TestCase):
    """Tests for finding batches without valid result files."""

    def _write_file(self, directory, name, data):
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, 'w') as f:
            json.dump(data, f)

    def test_all_complete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            batch_dir = os.path.join(tmpdir, 'batches')
            results_dir = os.path.join(tmpdir, 'results')
            self._write_file(batch_dir, 'batch-000.json', [{'post_id': 1}])
            self._write_file(batch_dir, 'batch-001.json', [{'post_id': 2}])
            self._write_file(results_dir, 'result-000.json', [
                {'post_id': 1, 'cats': [10], 'new_cats': []},
            ])
            self._write_file(results_dir, 'result-001.json', [
                {'post_id': 2, 'cats': [20], 'new_cats': []},
            ])
            self.assertEqual(find_incomplete_batches(batch_dir, results_dir), [])

    def test_none_complete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            batch_dir = os.path.join(tmpdir, 'batches')
            results_dir = os.path.join(tmpdir, 'results')
            self._write_file(batch_dir, 'batch-000.json', [{'post_id': 1}])
            self._write_file(batch_dir, 'batch-001.json', [{'post_id': 2}])
            os.makedirs(results_dir, exist_ok=True)
            incomplete = find_incomplete_batches(batch_dir, results_dir)
            self.assertEqual(incomplete, ['batch-000.json', 'batch-001.json'])

    def test_gap_in_middle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            batch_dir = os.path.join(tmpdir, 'batches')
            results_dir = os.path.join(tmpdir, 'results')
            for i in range(3):
                self._write_file(batch_dir, f'batch-{i:03d}.json', [{'post_id': i}])
            self._write_file(results_dir, 'result-000.json', [
                {'post_id': 0, 'cats': [10], 'new_cats': []},
            ])
            self._write_file(results_dir, 'result-002.json', [
                {'post_id': 2, 'cats': [30], 'new_cats': []},
            ])
            incomplete = find_incomplete_batches(batch_dir, results_dir)
            self.assertEqual(incomplete, ['batch-001.json'])

    def test_invalid_result_treated_as_incomplete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            batch_dir = os.path.join(tmpdir, 'batches')
            results_dir = os.path.join(tmpdir, 'results')
            self._write_file(batch_dir, 'batch-000.json', [{'post_id': 1}])
            # Invalid result: missing 'cats' field.
            self._write_file(results_dir, 'result-000.json', [
                {'post_id': 1},
            ])
            incomplete = find_incomplete_batches(batch_dir, results_dir)
            self.assertEqual(incomplete, ['batch-000.json'])

    def test_corrupt_json_treated_as_incomplete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            batch_dir = os.path.join(tmpdir, 'batches')
            results_dir = os.path.join(tmpdir, 'results')
            self._write_file(batch_dir, 'batch-000.json', [{'post_id': 1}])
            os.makedirs(results_dir, exist_ok=True)
            with open(os.path.join(results_dir, 'result-000.json'), 'w') as f:
                f.write('not valid json{{{')
            incomplete = find_incomplete_batches(batch_dir, results_dir)
            self.assertEqual(incomplete, ['batch-000.json'])

    def test_no_results_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            batch_dir = os.path.join(tmpdir, 'batches')
            results_dir = os.path.join(tmpdir, 'results')  # Does not exist.
            self._write_file(batch_dir, 'batch-000.json', [{'post_id': 1}])
            incomplete = find_incomplete_batches(batch_dir, results_dir)
            self.assertEqual(incomplete, ['batch-000.json'])

    def test_empty_directories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            batch_dir = os.path.join(tmpdir, 'batches')
            results_dir = os.path.join(tmpdir, 'results')
            os.makedirs(batch_dir)
            os.makedirs(results_dir)
            self.assertEqual(find_incomplete_batches(batch_dir, results_dir), [])

    def test_partial_coverage_treated_as_incomplete(self):
        """A result with fewer posts than the batch should be flagged."""
        with tempfile.TemporaryDirectory() as tmpdir:
            batch_dir = os.path.join(tmpdir, 'batches')
            results_dir = os.path.join(tmpdir, 'results')
            self._write_file(batch_dir, 'batch-000.json', [
                {'post_id': 10}, {'post_id': 20}, {'post_id': 30},
            ])
            # Only 1 of 3 posts covered — agent crashed mid-batch.
            self._write_file(results_dir, 'result-000.json', [
                {'post_id': 10, 'cats': ['tech'], 'new_cats': []},
            ])
            incomplete = find_incomplete_batches(batch_dir, results_dir)
            self.assertEqual(incomplete, ['batch-000.json'])

    def test_extra_result_ids_still_valid(self):
        """Results may contain extra IDs (e.g., from a retry) — that's fine."""
        with tempfile.TemporaryDirectory() as tmpdir:
            batch_dir = os.path.join(tmpdir, 'batches')
            results_dir = os.path.join(tmpdir, 'results')
            self._write_file(batch_dir, 'batch-000.json', [
                {'post_id': 10},
            ])
            self._write_file(results_dir, 'result-000.json', [
                {'post_id': 10, 'cats': [10], 'new_cats': []},
                {'post_id': 99, 'cats': [20], 'new_cats': []},
            ])
            self.assertEqual(find_incomplete_batches(batch_dir, results_dir), [])

class TestValidateResultIds(unittest.TestCase):
    """Tests for post ID validation between batches and results."""

    def _write_json(self, tmpdir, subdir, name, data):
        path = os.path.join(tmpdir, subdir)
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, name), 'w') as f:
            json.dump(data, f)

    def test_valid_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_json(tmpdir, 'batches', 'batch-000.json', [
                {'post_id': 100, 'title': 'A'},
                {'post_id': 200, 'title': 'B'},
            ])
            self._write_json(tmpdir, 'results', 'result-000.json', [
                {'post_id': 100, 'cats': ['tech']},
                {'post_id': 200, 'cats': ['food']},
            ])
            check = validate_result_ids(
                os.path.join(tmpdir, 'results'),
                os.path.join(tmpdir, 'batches'),
            )
            self.assertTrue(check['valid'])
            self.assertEqual(len(check['errors']), 0)
            self.assertEqual(len(check['invalid_ids']), 0)
            self.assertEqual(len(check['missing_ids']), 0)

    def test_detects_invalid_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_json(tmpdir, 'batches', 'batch-000.json', [
                {'post_id': 100, 'title': 'A'},
            ])
            self._write_json(tmpdir, 'results', 'result-000.json', [
                {'post_id': 100, 'cats': ['tech']},
                {'post_id': 999, 'cats': ['food']},
            ])
            check = validate_result_ids(
                os.path.join(tmpdir, 'results'),
                os.path.join(tmpdir, 'batches'),
            )
            self.assertFalse(check['valid'])
            self.assertIn(999, check['invalid_ids'])

    def test_detects_missing_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_json(tmpdir, 'batches', 'batch-000.json', [
                {'post_id': 100, 'title': 'A'},
                {'post_id': 200, 'title': 'B'},
            ])
            self._write_json(tmpdir, 'results', 'result-000.json', [
                {'post_id': 100, 'cats': ['tech']},
            ])
            check = validate_result_ids(
                os.path.join(tmpdir, 'results'),
                os.path.join(tmpdir, 'batches'),
            )
            self.assertFalse(check['valid'])
            self.assertIn(200, check['missing_ids'])

    def test_detects_index_vs_id_bug(self):
        """Catches when an agent outputs 0,1,2,... instead of real post IDs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_json(tmpdir, 'batches', 'batch-000.json', [
                {'post_id': 2632, 'title': 'A'},
                {'post_id': 2631, 'title': 'B'},
                {'post_id': 2630, 'title': 'C'},
            ])
            self._write_json(tmpdir, 'results', 'result-000.json', [
                {'post_id': 0, 'cats': ['tech']},
                {'post_id': 1, 'cats': ['food']},
                {'post_id': 2, 'cats': ['art']},
            ])
            check = validate_result_ids(
                os.path.join(tmpdir, 'results'),
                os.path.join(tmpdir, 'batches'),
            )
            self.assertFalse(check['valid'])
            self.assertIn('result-000.json', check['suspect_index_files'])

    def test_no_false_positive_on_legitimately_low_ids(self):
        """Don't flag low IDs when the batch itself has low IDs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_json(tmpdir, 'batches', 'batch-000.json', [
                {'post_id': 1, 'title': 'A'},
                {'post_id': 2, 'title': 'B'},
                {'post_id': 3, 'title': 'C'},
            ])
            self._write_json(tmpdir, 'results', 'result-000.json', [
                {'post_id': 1, 'cats': ['tech']},
                {'post_id': 2, 'cats': ['food']},
                {'post_id': 3, 'cats': ['art']},
            ])
            check = validate_result_ids(
                os.path.join(tmpdir, 'results'),
                os.path.join(tmpdir, 'batches'),
            )
            self.assertTrue(check['valid'])
            self.assertEqual(len(check['suspect_index_files']), 0)


class TestValidateCategorySlugs(unittest.TestCase):
    """Tests for category slug validation in suggestions."""

    def test_all_valid(self):
        suggestions = [
            {'post_id': 1, 'cats': ['tech', 'food']},
            {'post_id': 2, 'cats': ['art']},
        ]
        check = validate_category_slugs(suggestions, {'tech', 'food', 'art'})
        self.assertTrue(check['valid'])
        self.assertEqual(len(check['unknown_slugs']), 0)

    def test_detects_unknown_slugs(self):
        suggestions = [
            {'post_id': 1, 'cats': ['tech', 'bogus']},
            {'post_id': 2, 'cats': ['bogus', 'also-fake']},
        ]
        check = validate_category_slugs(suggestions, {'tech', 'food'})
        self.assertFalse(check['valid'])
        self.assertEqual(check['unknown_slugs']['bogus'], 2)
        self.assertEqual(check['unknown_slugs']['also-fake'], 1)

    def test_empty_suggestions(self):
        check = validate_category_slugs([], {'tech'})
        self.assertTrue(check['valid'])

    def test_empty_cats(self):
        suggestions = [{'post_id': 1, 'cats': []}]
        check = validate_category_slugs(suggestions, {'tech'})
        self.assertTrue(check['valid'])


if __name__ == '__main__':
    unittest.main()
