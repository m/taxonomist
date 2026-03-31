import subprocess
import json
import os
import sys

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

    def _run_command(self, cmd):
        """Build and run a WP-CLI command."""
        base_cmd = f"wp --path={self.wp_path} {self.wp_cli_flags} {cmd}"
        
        if self.method == 'wp-cli-ssh':
            ssh_user = self.connection.get('ssh_user')
            ssh_host = self.connection.get('ssh_host')
            final_cmd = f"ssh {ssh_user}@{ssh_host} \"{base_cmd}\""
        else:
            final_cmd = base_cmd

        result = subprocess.run(final_cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"WP-CLI error: {result.stderr}")
        return result.stdout

    def list_categories(self):
        """List all categories as JSON."""
        output = self._run_command("term list category --format=json --fields=term_id,name,slug,description,count,parent")
        return json.loads(output)

    def export_posts(self, output_path):
        """Export posts using the lib/export-posts.php script."""
        # This assumes the PHP script is available on the remote server
        # in the same relative path if using SSH.
        php_script = "lib/export-posts.php"
        env_vars = f"TAXONOMIST_OUTPUT={output_path}"
        output = self._run_command(f"eval-file {php_script}")
        return output

    def set_post_categories(self, post_id, category_ids):
        """Set categories for a post."""
        ids_str = ",".join(map(str, category_ids))
        return self._run_command(f"post term set {post_id} category {ids_str}")

    def create_category(self, name, slug, description=""):
        """Create a new category."""
        return self._run_command(f"term create category '{name}' --slug='{slug}' --description='{description}'")

    def delete_category(self, term_id):
        """Delete a category."""
        return self._run_command(f"term delete category {term_id}")
