import json
import os
import posixpath
import shlex
import subprocess
import tempfile

# Directory holding the bundled PHP scripts (this file lives in lib/adapters/).
_LIB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _reject_option_like(value, label):
    """Refuse a value that could be parsed as a command-line option.

    A config or content value beginning with '-' can be interpreted by
    ssh, scp, or wp as an option rather than data. For example a
    ssh_user of '-oProxyCommand=touch /tmp/x' makes ssh run a local
    command before connecting, and a category name of '--require=evil.php'
    makes wp load arbitrary PHP. Fail closed rather than risk argument
    injection. '--' separators are added at the call sites as well, but
    OpenSSH still parses '-'-leading hostnames after '--', so this
    validation is the load-bearing guard.
    """
    if isinstance(value, str) and value.startswith('-'):
        raise ValueError(
            f'{label} may not start with "-" (it would be parsed as a '
            f'command-line option): {value!r}'
        )


class WpCliAdapter:
    """
    Adapter for interacting with WordPress via WP-CLI.
    Supports both local and remote (SSH) connections.
    """

    def __init__(self, config):
        self.config = config
        self.connection = config.get('connection', {})
        self.method = self.connection.get('method')
        self.wp_path = self.connection.get('wp_path', '.')
        self.wp_cli_flags = self.connection.get('wp_cli_flags', '')

    def _wp_base_command(self):
        """Build the base WP-CLI command as a list of safe argv tokens.

        No --path: the command is run from the WP directory instead (see
        _run_command). wp-cli's --path doesn't follow a symlinked
        wp-load.php (some managed hosts keep core in a separate directory
        and symlink wp-load.php into the docroot), but running from the
        directory resolves the install fine.
        """
        cmd = ['wp']
        if self.wp_cli_flags:
            cmd.extend(shlex.split(self.wp_cli_flags))
        return cmd

    def _ssh_target(self):
        ssh_user = self.connection.get('ssh_user')
        ssh_host = self.connection.get('ssh_host')
        if not ssh_user or not ssh_host:
            raise ValueError('wp-cli-ssh requires ssh_user and ssh_host')
        _reject_option_like(ssh_user, 'ssh_user')
        _reject_option_like(ssh_host, 'ssh_host')
        return f'{ssh_user}@{ssh_host}'

    def _run_remote_shell(self, command):
        """Run a safely quoted command string on the remote host over SSH."""
        target = self._ssh_target()
        result = subprocess.run(
            ['ssh', '--', target, command],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise Exception(f'SSH error: {result.stderr}')
        return result.stdout

    def _run_command(self, args, env=None):
        """Build and run a WP-CLI command without invoking a local shell.

        Runs from the configured WP directory (cd over SSH, cwd locally)
        so wp-cli auto-detects the install even when wp-load.php is a
        symlink.
        """
        env = {key: str(value) for key, value in (env or {}).items()}
        cmd = [*self._wp_base_command(), *args]

        if self.method == 'wp-cli-ssh':
            env_prefix = ' '.join(
                f'{key}={shlex.quote(value)}' for key, value in env.items()
            )
            quoted_cmd = ' '.join(shlex.quote(part) for part in cmd)
            cd_prefix = f'cd {shlex.quote(self.wp_path)} &&'
            remote_cmd = ' '.join(
                p for p in (cd_prefix, env_prefix, quoted_cmd) if p
            )
            return self._run_remote_shell(remote_cmd)

        local_env = os.environ.copy()
        local_env.update(env)
        result = subprocess.run(
            cmd, capture_output=True, text=True, env=local_env,
            cwd=self.wp_path,
        )
        if result.returncode != 0:
            raise Exception(f'WP-CLI error: {result.stderr}')
        return result.stdout

    def _upload(self, local_path, remote_path):
        """Copy a local file to the remote host over scp."""
        result = subprocess.run(
            ['scp', '--', local_path,
             f'{self._ssh_target()}:{remote_path}'],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise Exception(f'SCP upload error: {result.stderr}')

    def _download(self, remote_path, local_path):
        """Copy a remote file back over scp. Returns whether it worked."""
        result = subprocess.run(
            ['scp', '--', f'{self._ssh_target()}:{remote_path}', local_path],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def list_categories(self):
        """List all categories as JSON."""
        output = self._run_command([
            'term', 'list', 'category', '--format=json',
            '--fields=term_id,name,slug,description,count,parent',
        ])
        return json.loads(output)

    def _run_script(self, script, inputs=None, outputs=None, env=None):
        """Run a bundled PHP script with `wp eval-file`.

        The scripts ship with this repo, not on the target, so they are
        referenced by absolute path locally and uploaded before use over
        SSH (the remote home is not the repo).

        inputs and outputs map environment variables to local file paths.
        Paths are made absolute because the command runs from the WP
        directory. Over SSH, inputs are uploaded first and outputs are
        copied back afterwards. An output can be the only record of what
        the script changed, so it is copied back even when the script
        fails, and stays on the server if that copy fails.

        Returns the script's stdout.
        """
        inputs = {k: os.path.abspath(v) for k, v in (inputs or {}).items()}
        outputs = {k: os.path.abspath(v) for k, v in (outputs or {}).items()}
        for path in outputs.values():
            os.makedirs(os.path.dirname(path), exist_ok=True)
        php_script = os.path.join(_LIB_DIR, script)

        if self.method != 'wp-cli-ssh':
            return self._run_command(
                ['eval-file', php_script],
                env={**(env or {}), **inputs, **outputs},
            )

        self._ssh_target()
        token = next(tempfile._get_candidate_names())

        def remote_path(name):
            return posixpath.join('/tmp', f'taxonomist-{token}-{name}')

        remote_script = remote_path(script)
        remote_inputs = {k: remote_path(k.lower()) for k in inputs}
        remote_outputs = {k: remote_path(k.lower()) for k in outputs}

        stdout = None
        script_error = None
        try:
            self._upload(php_script, remote_script)
            for key, path in inputs.items():
                self._upload(path, remote_inputs[key])
            stdout = self._run_command(
                ['eval-file', remote_script],
                env={**(env or {}), **remote_inputs, **remote_outputs},
            )
        except Exception as exc:
            script_error = exc

        not_copied = [
            remote_outputs[key] for key, path in outputs.items()
            if not self._download(remote_outputs[key], path)
        ]
        to_remove = [remote_script, *remote_inputs.values()]
        to_remove += [
            path for path in remote_outputs.values()
            if path not in not_copied
        ]
        try:
            quoted = ' '.join(shlex.quote(path) for path in to_remove)
            self._run_remote_shell(f'rm -f -- {quoted}')
        except Exception:
            pass

        if not_copied:
            note = (
                'Not copied back, still on the server if it was written: '
                + ', '.join(not_copied)
            )
            raise Exception(f'{script_error} {note}' if script_error else note)
        if script_error:
            raise script_error
        return stdout

    def export_posts(self, output_path):
        """Export posts using the bundled export-posts.php script."""
        self._run_script(
            'export-posts.php',
            outputs={'TAXONOMIST_OUTPUT': output_path},
        )
        return output_path

    def backup(self, output_path):
        """Write a taxonomy backup using the bundled backup.php script."""
        self._run_script(
            'backup.php',
            outputs={'TAXONOMIST_OUTPUT': output_path},
        )
        return output_path

    def apply_tag_changes(self, suggestions_path, log_path, mode='preview'):
        """Run apply-changes.php in tag mode and bring its log back.

        mode is 'preview' or 'apply'. See apply-changes.php for the
        suggestions format. Returns the script's stdout.

        log_path must not exist yet. The script overwrites its log, and
        the file may be the only record of an earlier run's changes.
        """
        if mode not in ('preview', 'apply'):
            raise ValueError(f'mode must be preview or apply, got {mode!r}')
        if os.path.exists(log_path):
            raise ValueError(
                f'{log_path} already exists. Give each run its own log path.'
            )
        return self._run_script(
            'apply-changes.php',
            inputs={'TAXONOMIST_SUGGESTIONS': suggestions_path},
            outputs={'TAXONOMIST_LOG': log_path},
            env={'TAXONOMIST_TAXONOMY': 'post_tag', 'TAXONOMIST_MODE': mode},
        )

    def set_post_categories(self, post_id, category_ids):
        """Set categories for a post by term ID.

        Each term ID is passed as a separate positional argument with
        --by=id. Comma-joining them into one token makes `wp post term
        set` treat the value as a single slug; if no category has that
        slug, wp silently CREATES a junk category named after the value
        (e.g. setting term 390 created a category named "390"). --by=id
        also stops a numeric term ID being matched against a slug.
        """
        args = ['post', 'term', 'set', str(post_id), 'category']
        args.extend(str(cid) for cid in category_ids)
        args.append('--by=id')
        return self._run_command(args)

    def create_category(self, name, slug, description=''):
        """Create a new category."""
        # name is a bare positional; a '-'-leading value would be parsed
        # by wp as a global flag (e.g. --require=). slug/description are
        # bound to their --flag= token, but reject '-'-leading values too
        # as defense in depth.
        _reject_option_like(name, 'category name')
        _reject_option_like(slug, 'category slug')
        args = ['term', 'create', 'category', name, f'--slug={slug}']
        if description:
            args.append(f'--description={description}')
        return self._run_command(args)

    def delete_category(self, term_id):
        """Delete a category."""
        return self._run_command(['term', 'delete', 'category', str(term_id)])

    # --- Tags ---
    #
    # Mirrors the category methods above for WordPress core's flat
    # `post_tag` taxonomy (no parent, no default term). Bulk tag
    # reassignment with logging goes through lib/apply-changes.php with
    # TAXONOMIST_TAXONOMY=post_tag, the same way categories use it; these
    # methods cover listing, one-off assignments, and term create/delete.

    def list_tags(self):
        """List all tags as JSON.

        Same fields as list_categories() minus `parent`, which tags don't
        have.
        """
        output = self._run_command([
            'term', 'list', 'post_tag', '--format=json',
            '--fields=term_id,name,slug,description,count',
        ])
        return json.loads(output)

    def set_post_tags(self, post_id, tag_ids, allow_clear=True):
        """Set a post's complete tag list by term ID.

        Uses the same separate-positional-args + --by=id form as
        set_post_categories(), for the same reason: anything else lets wp
        match the value as a slug and create a junk tag. Matching by ID
        also avoids the name-to-slug collision that makes
        WpcomAdapter.set_post_tags() raise `tags_drift`.

        An empty tag_ids list removes every tag from the post (`wp post
        term set` needs at least one term, so this uses `wp post term
        remove --all`). Tags have no default term, so this is a normal end
        state; pass allow_clear=False to refuse it instead, matching
        WpcomAdapter.set_post_tags().
        """
        tag_ids = [int(tid) for tid in tag_ids]
        if not tag_ids:
            if not allow_clear:
                raise ValueError(
                    f'set_post_tags(post_id={post_id}): refusing to set an '
                    'empty tag list with allow_clear=False.'
                )
            return self._run_command(
                ['post', 'term', 'remove', str(post_id), 'post_tag', '--all']
            )
        args = ['post', 'term', 'set', str(post_id), 'post_tag']
        args.extend(str(tid) for tid in tag_ids)
        args.append('--by=id')
        return self._run_command(args)

    def create_tag(self, name, slug, description=''):
        """Create a new tag."""
        # Same argument-injection guard as create_category(): name is a
        # bare positional that wp would parse as a flag if it began with '-'.
        _reject_option_like(name, 'tag name')
        _reject_option_like(slug, 'tag slug')
        args = ['term', 'create', 'post_tag', name, f'--slug={slug}']
        if description:
            args.append(f'--description={description}')
        return self._run_command(args)

    def delete_tag(self, term_id):
        """Delete a tag by term ID."""
        if not isinstance(term_id, int):
            raise TypeError(
                f'term_id must be int, got {type(term_id).__name__}'
            )
        return self._run_command(['term', 'delete', 'post_tag', str(term_id)])
