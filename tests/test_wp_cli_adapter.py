"""
Tests for the WP-CLI adapter.

Focused on argument-injection hardening: config values and content that
begin with '-' must not be interpretable as ssh/wp command-line options.
subprocess.run is patched so no real commands execute.
"""

import os
import sys
import tempfile
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


class TestRunsFromWpPath(unittest.TestCase):
    """The adapter must run wp from the WP directory rather than passing
    --path, because --path doesn't follow a symlinked wp-load.php (some
    managed hosts keep core in a separate directory and symlink wp-load.php
    into the docroot)."""

    @patch('adapters.wp_cli_adapter.subprocess.run')
    def test_local_uses_cwd_not_path_flag(self, mock_run):
        mock_run.return_value = _ok_run()
        adapter = WpCliAdapter(_local_config(wp_path='/srv/htdocs'))
        adapter.list_categories()
        argv = mock_run.call_args[0][0]
        kwargs = mock_run.call_args[1]
        self.assertEqual(kwargs.get('cwd'), '/srv/htdocs')
        self.assertFalse(
            any(str(a).startswith('--path=') for a in argv),
            f'should not pass --path: {argv}',
        )

    @patch('adapters.wp_cli_adapter.subprocess.run')
    def test_ssh_cds_into_wp_path(self, mock_run):
        mock_run.return_value = _ok_run()
        adapter = WpCliAdapter(_ssh_config(wp_path='/srv/htdocs'))
        adapter.list_categories()
        remote_cmd = mock_run.call_args[0][0][-1]
        self.assertIn('cd /srv/htdocs', remote_cmd)
        self.assertNotIn('--path=', remote_cmd)


class TestExportUploadsScript(unittest.TestCase):
    """export_posts must not assume lib/export-posts.php already exists on
    the target. Locally it uses an absolute path; over SSH it uploads the
    script first and eval-files the uploaded copy."""

    @patch('adapters.wp_cli_adapter.subprocess.run')
    def test_local_export_uses_absolute_script_path(self, mock_run):
        mock_run.return_value = _ok_run()
        adapter = WpCliAdapter(_local_config())
        adapter.export_posts('/tmp/out.json')
        argv = mock_run.call_args[0][0]
        script = argv[argv.index('eval-file') + 1]
        self.assertTrue(os.path.isabs(script), f'not absolute: {script}')
        self.assertTrue(script.endswith('export-posts.php'))

    @patch('adapters.wp_cli_adapter.subprocess.run')
    def test_ssh_export_uploads_script_then_eval_files_it(self, mock_run):
        mock_run.return_value = _ok_run()
        adapter = WpCliAdapter(_ssh_config())
        adapter.export_posts('/tmp/out.json')
        calls = [c.args[0] for c in mock_run.call_args_list]
        # An scp call that uploads the local export-posts.php to a remote path.
        uploads = [
            c for c in calls
            if c[0] == 'scp'
            and any(str(a).endswith('export-posts.php') for a in c)
            and any(str(a).startswith('root@example.com:') for a in c)
        ]
        self.assertTrue(uploads, f'no script upload found: {calls}')
        # The eval-file command must target the uploaded remote copy, not the
        # repo-relative lib/export-posts.php.
        evalfile_cmds = [
            c[-1] for c in calls if c[0] == 'ssh' and 'eval-file' in c[-1]
        ]
        self.assertTrue(evalfile_cmds, f'no eval-file ssh call: {calls}')
        self.assertNotIn('lib/export-posts.php', evalfile_cmds[0])


def _failing_call(argv, needle):
    """A subprocess.run side effect failing calls that contain `needle`."""
    result = _ok_run()
    if any(needle in str(arg) for arg in argv):
        result.returncode = 1
        result.stderr = 'boom'
    return result


def _env_path(remote_cmd, key):
    """Pull a remote path out of the `KEY=value` prefix of an ssh command."""
    return remote_cmd.split(f'{key}=')[1].split()[0]


class TestRunScript(unittest.TestCase):
    """backup() and apply_tag_changes() run bundled scripts the same way
    export_posts() does, and must not lose the output when something fails."""

    @patch('adapters.wp_cli_adapter.subprocess.run')
    def test_local_paths_are_made_absolute(self, mock_run):
        """Commands run from wp_path, so a relative path would land there
        instead of in the repo."""
        mock_run.return_value = _ok_run()
        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                WpCliAdapter(_local_config()).backup('data/backup.json')
                self.assertTrue(os.path.isdir('data'))
            finally:
                os.chdir(original_cwd)
        env = mock_run.call_args[1]['env']
        self.assertTrue(os.path.isabs(env['TAXONOMIST_OUTPUT']))

    @patch('adapters.wp_cli_adapter.subprocess.run')
    def test_ssh_backup_uploads_script_and_copies_output_back(self, mock_run):
        mock_run.return_value = _ok_run()
        with tempfile.TemporaryDirectory() as tmp:
            local = os.path.join(tmp, 'backup.json')
            WpCliAdapter(_ssh_config()).backup(local)
        calls = [c.args[0] for c in mock_run.call_args_list]
        self.assertEqual(calls[0][0], 'scp')
        self.assertTrue(calls[0][2].endswith('backup.php'))
        eval_cmd = next(c[-1] for c in calls if 'eval-file' in c[-1])
        remote_output = _env_path(eval_cmd, 'TAXONOMIST_OUTPUT')
        self.assertIn(
            ['scp', '--', f'root@example.com:{remote_output}', local], calls,
        )
        self.assertIn(remote_output, calls[-1][-1])
        self.assertIn('rm -f', calls[-1][-1])

    @patch('adapters.wp_cli_adapter.subprocess.run')
    def test_ssh_apply_uploads_suggestions_and_sets_tag_mode(self, mock_run):
        mock_run.return_value = _ok_run()
        with tempfile.TemporaryDirectory() as tmp:
            WpCliAdapter(_ssh_config()).apply_tag_changes(
                os.path.join(tmp, 'tag-suggestions.json'),
                os.path.join(tmp, 'tag-changes.tsv'),
                mode='apply',
            )
        calls = [c.args[0] for c in mock_run.call_args_list]
        eval_cmd = next(c[-1] for c in calls if 'eval-file' in c[-1])
        self.assertIn('TAXONOMIST_TAXONOMY=post_tag', eval_cmd)
        self.assertIn('TAXONOMIST_MODE=apply', eval_cmd)
        remote_input = _env_path(eval_cmd, 'TAXONOMIST_SUGGESTIONS')
        uploads = [c for c in calls if c[0] == 'scp' and remote_input in c[-1]]
        self.assertTrue(uploads, f'suggestions not uploaded: {calls}')

    @patch('adapters.wp_cli_adapter.subprocess.run')
    def test_rejects_unknown_mode(self, mock_run):
        with self.assertRaises(ValueError):
            WpCliAdapter(_ssh_config()).apply_tag_changes(
                'in.json', 'log.tsv', mode='aply',
            )
        mock_run.assert_not_called()

    @patch('adapters.wp_cli_adapter.subprocess.run')
    def test_refuses_to_overwrite_an_existing_log(self, mock_run):
        """A retry or a later preview must not wipe an earlier run's log."""
        with tempfile.NamedTemporaryFile(suffix='.tsv') as existing:
            with self.assertRaises(ValueError):
                WpCliAdapter(_ssh_config()).apply_tag_changes(
                    'in.json', existing.name, mode='apply',
                )
        mock_run.assert_not_called()

    @patch('adapters.wp_cli_adapter.subprocess.run')
    def test_ssh_log_is_copied_back_when_the_script_fails(self, mock_run):
        """A partial apply still changed posts, so its log must come back."""
        mock_run.side_effect = lambda argv, **_: _failing_call(argv, 'eval-file')
        with tempfile.TemporaryDirectory() as tmp:
            local_log = os.path.join(tmp, 'tag-changes.tsv')
            with self.assertRaisesRegex(Exception, 'boom'):
                WpCliAdapter(_ssh_config()).apply_tag_changes(
                    os.path.join(tmp, 'in.json'), local_log, mode='apply',
                )
        calls = [c.args[0] for c in mock_run.call_args_list]
        self.assertTrue(
            [c for c in calls if c[0] == 'scp' and c[-1] == local_log],
            f'log not copied back: {calls}',
        )

    @patch('adapters.wp_cli_adapter.subprocess.run')
    def test_ssh_output_stays_on_server_when_copy_back_fails(self, mock_run):
        """The remote file may be the only copy, so never delete it unless
        it reached the local machine, and say where it is."""
        def fail_copy_back(argv, **_):
            result = _ok_run()
            if argv[0] == 'scp' and str(argv[-1]).endswith('.tsv'):
                result.returncode = 1
            return result

        mock_run.side_effect = fail_copy_back
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(Exception) as caught:
                WpCliAdapter(_ssh_config()).apply_tag_changes(
                    os.path.join(tmp, 'in.json'),
                    os.path.join(tmp, 'tag-changes.tsv'),
                    mode='apply',
                )
        calls = [c.args[0] for c in mock_run.call_args_list]
        eval_cmd = next(c[-1] for c in calls if 'eval-file' in c[-1])
        remote_log = _env_path(eval_cmd, 'TAXONOMIST_LOG')
        self.assertIn(remote_log, str(caught.exception))
        self.assertNotIn(remote_log, calls[-1][-1])
        self.assertIn('rm -f', calls[-1][-1])


class TestTagMethods(unittest.TestCase):
    """Tag methods mirror the category ones against `post_tag`, with the
    same --by=id and argument-injection hardening."""

    @patch('adapters.wp_cli_adapter.subprocess.run')
    def test_list_tags_queries_post_tag(self, mock_run):
        mock_run.return_value = _ok_run()
        mock_run.return_value.stdout = (
            '[{"term_id": 31, "name": "Gutenberg", "slug": "gutenberg",'
            ' "description": "", "count": 3}]'
        )
        adapter = WpCliAdapter(_local_config())
        tags = adapter.list_tags()
        argv = mock_run.call_args[0][0]
        self.assertEqual(argv[1:4], ['term', 'list', 'post_tag'])
        self.assertIn('--fields=term_id,name,slug,description,count', argv)
        self.assertEqual(tags[0]['term_id'], 31)

    @patch('adapters.wp_cli_adapter.subprocess.run')
    def test_set_post_tags_uses_by_id_and_separate_args(self, mock_run):
        """Same failure mode as categories: a comma-joined or slug-matched
        value makes wp create a junk tag named after the ID."""
        mock_run.return_value = _ok_run()
        adapter = WpCliAdapter(_local_config())
        adapter.set_post_tags(123, [31, 57])
        argv = mock_run.call_args[0][0]
        self.assertEqual(
            argv[1:], ['post', 'term', 'set', '123', 'post_tag', '31', '57', '--by=id'],
        )

    @patch('adapters.wp_cli_adapter.subprocess.run')
    def test_set_post_tags_empty_list_removes_all(self, mock_run):
        """`wp post term set` needs at least one term, so clearing a post's
        tags has to go through `post term remove --all`."""
        mock_run.return_value = _ok_run()
        adapter = WpCliAdapter(_local_config())
        adapter.set_post_tags(123, [])
        argv = mock_run.call_args[0][0]
        self.assertEqual(
            argv[1:], ['post', 'term', 'remove', '123', 'post_tag', '--all'],
        )

    @patch('adapters.wp_cli_adapter.subprocess.run')
    def test_set_post_tags_refuses_clear_when_disallowed(self, mock_run):
        adapter = WpCliAdapter(_local_config())
        with self.assertRaises(ValueError):
            adapter.set_post_tags(123, [], allow_clear=False)
        mock_run.assert_not_called()

    @patch('adapters.wp_cli_adapter.subprocess.run')
    def test_create_tag_rejects_option_like_name(self, mock_run):
        adapter = WpCliAdapter(_local_config())
        with self.assertRaises(ValueError):
            adapter.create_tag('--require=/tmp/evil.php', 'evil')
        with self.assertRaises(ValueError):
            adapter.create_tag('Tech', '--path=/etc')
        mock_run.assert_not_called()

    @patch('adapters.wp_cli_adapter.subprocess.run')
    def test_create_tag_targets_post_tag(self, mock_run):
        mock_run.return_value = _ok_run()
        adapter = WpCliAdapter(_local_config())
        adapter.create_tag('Gutenberg', 'gutenberg', 'Block editor')
        argv = mock_run.call_args[0][0]
        self.assertEqual(argv[1:5], ['term', 'create', 'post_tag', 'Gutenberg'])
        self.assertIn('--slug=gutenberg', argv)
        self.assertIn('--description=Block editor', argv)

    @patch('adapters.wp_cli_adapter.subprocess.run')
    def test_update_tag_sets_fields_by_id(self, mock_run):
        mock_run.return_value = _ok_run()
        WpCliAdapter(_local_config()).update_tag(31, {'description': '-a tag'})
        argv = mock_run.call_args[0][0]
        self.assertEqual(
            argv[1:],
            ['term', 'update', 'post_tag', '31', '--by=id', '--description=-a tag'],
        )

    @patch('adapters.wp_cli_adapter.subprocess.run')
    def test_update_tag_rejects_unknown_fields(self, mock_run):
        """Each key becomes a wp flag, so a key like `require` must not
        reach the command line."""
        adapter = WpCliAdapter(_local_config())
        for fields in ({'require': 'evil.php'}, {}):
            with self.assertRaises(ValueError):
                adapter.update_tag(31, fields)
        mock_run.assert_not_called()

    @patch('adapters.wp_cli_adapter.subprocess.run')
    def test_delete_tag_by_int_id(self, mock_run):
        mock_run.return_value = _ok_run()
        adapter = WpCliAdapter(_local_config())
        adapter.delete_tag(31)
        argv = mock_run.call_args[0][0]
        self.assertEqual(argv[1:], ['term', 'delete', 'post_tag', '31'])

    @patch('adapters.wp_cli_adapter.subprocess.run')
    def test_delete_tag_rejects_non_int(self, mock_run):
        """A slug or digit-string here would mean the caller guessed the
        term instead of resolving it from the export."""
        adapter = WpCliAdapter(_local_config())
        with self.assertRaises(TypeError):
            adapter.delete_tag('31')
        mock_run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
