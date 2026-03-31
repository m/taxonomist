import json
import os
import posixpath
import shlex
import subprocess
import tempfile


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
        """Build the base WP-CLI command as a list of safe argv tokens."""
        cmd = ['wp', f'--path={self.wp_path}']
        if self.wp_cli_flags:
            cmd.extend(shlex.split(self.wp_cli_flags))
        return cmd

    def _ssh_target(self):
        ssh_user = self.connection.get('ssh_user')
        ssh_host = self.connection.get('ssh_host')
        if not ssh_user or not ssh_host:
            raise ValueError('wp-cli-ssh requires ssh_user and ssh_host')
        return f'{ssh_user}@{ssh_host}'

    def _run_remote_shell(self, command):
        """Run a safely quoted command string on the remote host over SSH."""
        result = subprocess.run(
            ['ssh', self._ssh_target(), command],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise Exception(f'SSH error: {result.stderr}')
        return result.stdout

    def _run_command(self, args, env=None):
        """Build and run a WP-CLI command without invoking a local shell."""
        env = {key: str(value) for key, value in (env or {}).items()}
        cmd = [*self._wp_base_command(), *args]

        if self.method == 'wp-cli-ssh':
            env_prefix = ' '.join(
                f'{key}={shlex.quote(value)}' for key, value in env.items()
            )
            quoted_cmd = ' '.join(shlex.quote(part) for part in cmd)
            remote_cmd = f'{env_prefix} {quoted_cmd}'.strip()
            return self._run_remote_shell(remote_cmd)

        local_env = os.environ.copy()
        local_env.update(env)
        result = subprocess.run(cmd, capture_output=True, text=True, env=local_env)
        if result.returncode != 0:
            raise Exception(f'WP-CLI error: {result.stderr}')
        return result.stdout

    def list_categories(self):
        """List all categories as JSON."""
        output = self._run_command([
            'term', 'list', 'category', '--format=json',
            '--fields=term_id,name,slug,description,count,parent',
        ])
        return json.loads(output)

    def export_posts(self, output_path):
        """Export posts using the lib/export-posts.php script."""
        php_script = 'lib/export-posts.php'

        if self.method == 'wp-cli-ssh':
            remote_output = posixpath.join(
                '/tmp',
                f'taxonomist-export-{next(tempfile._get_candidate_names())}.json',
            )
            try:
                self._run_command(
                    ['eval-file', php_script],
                    env={'TAXONOMIST_OUTPUT': remote_output},
                )
                result = subprocess.run(
                    ['scp', f'{self._ssh_target()}:{remote_output}', output_path],
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    raise Exception(f'SCP error: {result.stderr}')
            finally:
                try:
                    self._run_remote_shell(f'rm -f -- {shlex.quote(remote_output)}')
                except Exception:
                    pass
            return output_path

        self._run_command(
            ['eval-file', php_script],
            env={'TAXONOMIST_OUTPUT': output_path},
        )
        return output_path

    def set_post_categories(self, post_id, category_ids):
        """Set categories for a post."""
        ids_str = ','.join(map(str, category_ids))
        return self._run_command(['post', 'term', 'set', str(post_id), 'category', ids_str])

    def create_category(self, name, slug, description=''):
        """Create a new category."""
        args = ['term', 'create', 'category', name, f'--slug={slug}']
        if description:
            args.append(f'--description={description}')
        return self._run_command(args)

    def delete_category(self, term_id):
        """Delete a category."""
        return self._run_command(['term', 'delete', 'category', str(term_id)])
