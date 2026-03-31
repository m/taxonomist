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
    parse_change_log,
    split_into_batches,
    validate_backup,
    validate_export,
    validate_suggestions,
    write_batches,
)


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


class TestWriteBatches(unittest.TestCase):
    """Tests for writing batch files to disk."""

    def test_creates_files(self):
        posts = [{'id': i, 'title': f'Post {i}'} for i in range(5)]
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = write_batches(posts, tmpdir, batch_size=2)
            self.assertEqual(len(paths), 3)
            self.assertTrue(all(os.path.exists(p) for p in paths))

    def test_file_naming(self):
        posts = [{'id': i} for i in range(5)]
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = write_batches(posts, tmpdir, batch_size=2)
            self.assertTrue(paths[0].endswith('batch-000.json'))
            self.assertTrue(paths[1].endswith('batch-001.json'))
            self.assertTrue(paths[2].endswith('batch-002.json'))

    def test_file_contents_valid_json(self):
        posts = [{'id': 1, 'title': 'Hello'}, {'id': 2, 'title': 'World'}]
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = write_batches(posts, tmpdir, batch_size=2)
            with open(paths[0]) as f:
                loaded = json.load(f)
            self.assertEqual(len(loaded), 2)
            self.assertEqual(loaded[0]['id'], 1)

    def test_creates_directory(self):
        posts = [{'id': 1}]
        with tempfile.TemporaryDirectory() as tmpdir:
            new_dir = os.path.join(tmpdir, 'nested', 'batches')
            write_batches(posts, new_dir, batch_size=10)
            self.assertTrue(os.path.isdir(new_dir))


class TestAggregateResults(unittest.TestCase):
    """Tests for combining per-batch result files."""

    def _write_result(self, tmpdir, name, data):
        path = os.path.join(tmpdir, name)
        with open(path, 'w') as f:
            json.dump(data, f)

    def test_combines_batches(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_result(tmpdir, 'result-000.json', [
                {'id': 1, 'cats': ['Tech'], 'new_cats': []},
                {'id': 2, 'cats': ['Music'], 'new_cats': ['Jazz']},
            ])
            self._write_result(tmpdir, 'result-001.json', [
                {'id': 3, 'cats': ['Tech', 'AI'], 'new_cats': []},
            ])
            suggestions, cat_counts, new_counts = aggregate_results(tmpdir)
            self.assertEqual(len(suggestions), 3)
            self.assertEqual(cat_counts['Tech'], 2)
            self.assertEqual(cat_counts['Music'], 1)
            self.assertEqual(cat_counts['AI'], 1)
            self.assertEqual(new_counts['Jazz'], 1)

    def test_empty_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            suggestions, cat_counts, new_counts = aggregate_results(tmpdir)
            self.assertEqual(len(suggestions), 0)
            self.assertEqual(len(cat_counts), 0)

    def test_ignores_non_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_result(tmpdir, 'result-000.json', [
                {'id': 1, 'cats': ['Tech'], 'new_cats': []},
            ])
            with open(os.path.join(tmpdir, 'notes.txt'), 'w') as f:
                f.write('ignore me')
            suggestions, _, _ = aggregate_results(tmpdir)
            self.assertEqual(len(suggestions), 1)

    def test_sorted_file_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_result(tmpdir, 'result-001.json', [
                {'id': 99, 'cats': ['B'], 'new_cats': []},
            ])
            self._write_result(tmpdir, 'result-000.json', [
                {'id': 1, 'cats': ['A'], 'new_cats': []},
            ])
            suggestions, _, _ = aggregate_results(tmpdir)
            # result-000 should come first due to sorted() filename order.
            self.assertEqual(suggestions[0]['id'], 1)
            self.assertEqual(suggestions[1]['id'], 99)


class TestValidateExport(unittest.TestCase):
    """Tests for export JSON format validation."""

    def test_valid_export(self):
        posts = [
            {
                'id': 1,
                'title': 'Test',
                'date': '2024-01-01 00:00:00',
                'content': 'Hello world',
                'categories': ['Tech'],
            }
        ]
        self.assertEqual(validate_export(posts), [])

    def test_not_a_list(self):
        errors = validate_export({'id': 1})
        self.assertEqual(len(errors), 1)
        self.assertIn('JSON array', errors[0])

    def test_missing_field(self):
        posts = [{'id': 1, 'title': 'Test'}]
        errors = validate_export(posts)
        self.assertTrue(any('missing "date"' in e for e in errors))
        self.assertTrue(any('missing "content"' in e for e in errors))

    def test_wrong_type(self):
        posts = [
            {
                'id': 'not-an-int',
                'title': 'Test',
                'date': '2024-01-01',
                'content': 'Hello',
                'categories': ['Tech'],
            }
        ]
        errors = validate_export(posts)
        self.assertTrue(any('"id" should be int' in e for e in errors))

    def test_empty_list_is_valid(self):
        self.assertEqual(validate_export([]), [])


class TestValidateSuggestions(unittest.TestCase):
    """Tests for suggestion JSON format validation."""

    def test_valid_suggestions(self):
        data = [
            {'id': 1, 'cats': ['Tech'], 'new_cats': []},
            {'id': 2, 'cats': ['Music', 'Jazz']},
        ]
        self.assertEqual(validate_suggestions(data), [])

    def test_missing_id(self):
        data = [{'cats': ['Tech']}]
        errors = validate_suggestions(data)
        self.assertTrue(any('missing "id"' in e for e in errors))

    def test_missing_cats(self):
        data = [{'id': 1}]
        errors = validate_suggestions(data)
        self.assertTrue(any('missing "cats"' in e for e in errors))

    def test_cats_wrong_type(self):
        data = [{'id': 1, 'cats': 'Tech'}]
        errors = validate_suggestions(data)
        self.assertTrue(any('"cats" must be list' in e for e in errors))


class TestValidateBackup(unittest.TestCase):
    """Tests for backup JSON format validation."""

    def test_valid_backup(self):
        backup = {
            'timestamp': '2024-01-01 00:00:00',
            'site_url': 'https://example.com',
            'total_posts': 100,
            'total_categories': 10,
            'categories': [
                {'term_id': 1, 'name': 'Tech', 'slug': 'tech', 'description': '', 'count': 5, 'parent': 0}
            ],
            'post_categories': [
                {'post_id': 1, 'post_title': 'Test', 'category_ids': [1], 'category_slugs': ['tech']}
            ],
        }
        self.assertEqual(validate_backup(backup), [])

    def test_not_a_dict(self):
        errors = validate_backup([])
        self.assertIn('Backup must be a JSON object', errors)

    def test_missing_top_level_keys(self):
        errors = validate_backup({})
        self.assertTrue(any('timestamp' in e for e in errors))
        self.assertTrue(any('categories' in e for e in errors))

    def test_missing_category_fields(self):
        backup = {
            'timestamp': '', 'site_url': '', 'total_posts': 0,
            'total_categories': 0,
            'categories': [{'name': 'Tech'}],
            'post_categories': [],
        }
        errors = validate_backup(backup)
        self.assertTrue(any('missing "term_id"' in e for e in errors))
        self.assertTrue(any('missing "slug"' in e for e in errors))

    def test_missing_post_mapping_fields(self):
        backup = {
            'timestamp': '', 'site_url': '', 'total_posts': 0,
            'total_categories': 0,
            'categories': [],
            'post_categories': [{'post_id': 1}],
        }
        errors = validate_backup(backup)
        self.assertTrue(any('missing "category_slugs"' in e for e in errors))


class TestParseChangeLog(unittest.TestCase):
    """Tests for parsing TSV change log files."""

    def test_parses_log(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.tsv', delete=False) as f:
            f.write("timestamp\taction\tpost_id\tpost_title\told_categories\tnew_categories\tcats_added\tcats_removed\n")
            f.write("2024-01-01 00:00:00\tSET_CATS\t123\tTest Post\tAsides\tTech|AI\tTech|AI\tAsides\n")
            f.write("2024-01-01 00:00:01\tSET_CATS\t456\tOther Post\tAsides\tMusic\tMusic\tAsides\n")
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

    def test_empty_log(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.tsv', delete=False) as f:
            f.write("timestamp\taction\tpost_id\tpost_title\told_categories\tnew_categories\tcats_added\tcats_removed\n")
            path = f.name

        try:
            changes = parse_change_log(path)
            self.assertEqual(len(changes), 0)
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
