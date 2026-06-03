"""
Tests for the WP-CLI adapter.

Focused on argument-injection hardening: config values and content that
begin with '-' must not be interpretable as ssh/wp command-line options.
subprocess.run is patched so no real commands execute.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))
from adapters.wp_cli_adapter import WpCliAdapter


def _local_config(**overrides):
    conn = {'method': 'wp-cli-local', 'wp_path': '/var/www'}
    conn.update(overrides)
    return {'connection': conn}


def _ssh_config(**overrides):
    conn = {
        'method': 'wp-cli-ssh',
        'wp_path': '/var/www',
        'ssh_user': 'root',
        'ssh_host': 'example.com',
    }
    conn.update(overrides)
    return {'connection': conn}


def _ok_run():
    """A subprocess.run mock returning a successful empty result."""
    result = MagicMock()
    result.returncode = 0
    result.stdout = '[]'
    result.stderr = ''
    return result


class TestSshTargetInjection(unittest.TestCase):
    @patch('adapters.wp_cli_adapter.subprocess.run')
    def test_rejects_option_like_ssh_user(self, mock_run):
        """A ssh_user beginning with '-' would be parsed by ssh as an
        option (e.g. -oProxyCommand=...), enabling local code execution."""
        adapter = WpCliAdapter(_ssh_config(ssh_user='-oProxyCommand=touch /tmp/x'))
        with self.assertRaises(ValueError):
            adapter.list_categories()
        mock_run.assert_not_called()

    @patch('adapters.wp_cli_adapter.subprocess.run')
    def test_rejects_option_like_ssh_host(self, mock_run):
        adapter = WpCliAdapter(_ssh_config(ssh_host='-oProxyCommand=touch /tmp/x'))
        with self.assertRaises(ValueError):
            adapter.list_categories()
        mock_run.assert_not_called()

    @patch('adapters.wp_cli_adapter.subprocess.run')
    def test_ssh_invocation_uses_double_dash(self, mock_run):
        mock_run.return_value = _ok_run()
        adapter = WpCliAdapter(_ssh_config())
        adapter.list_categories()
        argv = mock_run.call_args[0][0]
        self.assertEqual(argv[0], 'ssh')
        # '--' must appear before the target so a future target value can't
        # be parsed as an option.
        self.assertIn('--', argv)
        self.assertLess(argv.index('--'), argv.index('root@example.com'))


class TestCreateCategoryInjection(unittest.TestCase):
    @patch('adapters.wp_cli_adapter.subprocess.run')
    def test_rejects_option_like_name(self, mock_run):
        """A category name beginning with '-' (e.g. --require=evil.php)
        would be parsed by wp as a global flag → arbitrary PHP execution."""
        adapter = WpCliAdapter(_local_config())
        with self.assertRaises(ValueError):
            adapter.create_category('--require=/tmp/evil.php', 'evil')
        mock_run.assert_not_called()

    @patch('adapters.wp_cli_adapter.subprocess.run')
    def test_rejects_option_like_slug(self, mock_run):
        adapter = WpCliAdapter(_local_config())
        with self.assertRaises(ValueError):
            adapter.create_category('Tech', '--path=/etc')
        mock_run.assert_not_called()

    @patch('adapters.wp_cli_adapter.subprocess.run')
    def test_accepts_normal_name(self, mock_run):
        mock_run.return_value = _ok_run()
        adapter = WpCliAdapter(_local_config())
        adapter.create_category('Tech', 'tech', 'Technology')
        argv = mock_run.call_args[0][0]
        self.assertIn('Tech', argv)
        self.assertIn('--slug=tech', argv)


class TestSetPostCategories(unittest.TestCase):
    @patch('adapters.wp_cli_adapter.subprocess.run')
    def test_uses_by_id_and_separate_args(self, mock_run):
        """Term IDs must be passed as separate positional args with
        --by=id. Comma-joining them ('5,7') makes wp treat the value as a
        single slug, not find it, and silently CREATE a junk category named
        after the value (verified live: setting [390] created a category
        named '390')."""
        mock_run.return_value = _ok_run()
        adapter = WpCliAdapter(_local_config())
        adapter.set_post_categories(123, [5, 7])
        argv = mock_run.call_args[0][0]
        self.assertIn('--by=id', argv)
        self.assertIn('5', argv)
        self.assertIn('7', argv)
        # The buggy version passed a single comma-joined '5,7' token.
        self.assertNotIn('5,7', argv)
        # Term values must come after the 'category' taxonomy positional.
        self.assertEqual(argv[-3:], ['5', '7', '--by=id'])


if __name__ == '__main__':
    unittest.main()
