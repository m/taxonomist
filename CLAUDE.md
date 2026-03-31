# Taxonomist

AI-powered WordPress category taxonomy optimizer. Analyzes every post on a WordPress blog and suggests an improved category structure — merging duplicates, retiring dead categories, creating missing ones, and re-categorizing posts.

## How It Works

This is a Claude Code tool. Users clone this repo, configure their WordPress connection, and let Claude Code handle the rest through an interactive, iterative process.

### Workflow

1. **Connect** — Detect and configure access to the WordPress site
2. **Export** — Download all posts (full content) and categories locally
3. **Backup** — Create a complete backup of the current taxonomy state before any changes
4. **Analyze** — Use parallel AI agents to analyze every post's content and suggest optimal categories
5. **Plan** — Present findings: category usage stats, suggested merges/retirements/new categories, and improved descriptions
6. **Review** — Iterate with the user until the plan is right
7. **Apply** — Execute changes via WP-CLI or REST API, logging every single change
8. **Verify** — Confirm the site still works and categories look correct

### Core Principles

- **Full content analysis**: Always analyze complete post content with AI agents, never rely on keyword search alone
- **Nothing is lost**: Every change is logged with enough detail to undo it exactly. Pre-change backups are mandatory.
- **Iterative**: The user approves every phase before the next one begins
- **Dry-run first**: Destructive operations are always previewed before execution
- **Parallel processing**: Posts are analyzed in batches using parallel agents for speed

## Configuration

The tool needs to connect to a WordPress site. Configuration is stored in `config.json`:

```json
{
  "site_url": "https://example.com",
  "connection": {
    "method": "wp-cli-ssh",
    "ssh_user": "root",
    "ssh_host": "example.com",
    "wp_path": "/var/www/html",
    "wp_cli_flags": "--allow-root"
  }
}
```

### Supported Connection Methods

| Method | Key | Requirements |
|---|---|---|
| WP-CLI over SSH | `wp-cli-ssh` | SSH access + WP-CLI installed on server |
| WP-CLI local | `wp-cli-local` | WP-CLI installed locally, WordPress on same machine |
| REST API + App Password | `rest-api` | WordPress 5.6+, Application Passwords enabled |
| REST API + JWT | `rest-api-jwt` | JWT Authentication plugin installed |
| XML-RPC | `xmlrpc` | XML-RPC enabled (legacy, not recommended) |

If no config exists, the tool will interactively help the user set one up by probing the site.

## Directory Structure

```
taxonomist/
├── CLAUDE.md              # This file — instructions for Claude Code
├── config.json            # WordPress connection config (user creates)
├── agents/                # Claude Code agent definitions
│   ├── connect.md         # Detect and configure WordPress access
│   ├── export.md          # Export all posts and categories
│   ├── analyze.md         # Analyze a batch of posts for categories
│   └── apply.md           # Apply category changes
├── lib/                   # PHP scripts for WP-CLI operations
│   ├── export-posts.php   # Export posts with full content
│   ├── apply-changes.php  # Apply category changes with logging
│   ├── backup.php         # Create taxonomy backup
│   └── restore.php        # Restore from backup
├── data/                  # Working data (gitignored)
│   ├── export/            # Exported posts and categories
│   ├── batches/           # Split post batches for analysis
│   ├── results/           # Agent analysis results
│   ├── backups/           # Pre-change backups
│   └── logs/              # Change logs
└── .gitignore
```

## Running the Tool

1. Clone this repo
2. Open with Claude Code: `claude` (in the repo directory)
3. Tell Claude: "Analyze and optimize my WordPress categories at example.com"
4. Claude will walk you through connection setup, export, analysis, and changes

## Change Logging

Every operation that modifies the site is logged to `data/logs/`. Each log file is a TSV with columns:

```
timestamp  action  post_id  post_title  old_categories  new_categories  category_added  category_removed  notes
```

Log files:
- `backup-{timestamp}.json` — Complete pre-change state (post→category mappings)
- `changes-{timestamp}.tsv` — Every individual change made
- `terms-deleted-{timestamp}.tsv` — Deleted category terms with their original data

### Reverting Changes

To undo all changes from a session:
```
"Revert the changes from {timestamp}"
```

Claude will read the log and backup files and restore the exact previous state.

## Analysis Approach

Posts are split into batches of ~200 and analyzed by parallel AI agents. Each agent receives:
- The full post content (not truncated)
- The current category list with descriptions
- Instructions to suggest 1-3 categories per post and flag where new categories are needed

The analysis runs in phases:
1. **Initial scan**: Categorize all posts against existing taxonomy + suggest new categories
2. **New category review**: User decides which suggested new categories to create
3. **Targeted scan**: Re-analyze for specific categories that need expansion (like the Audrey Capital example)
4. **Description generation**: Generate/improve category descriptions based on actual post content

## WordPress Access Adapters

All WordPress operations go through an adapter layer (`lib/adapters/`) so the same logic works regardless of connection method.

Required operations:
- `list_categories()` — Get all categories with counts and descriptions
- `list_posts(fields)` — Get all published posts with specified fields
- `get_post_content(id)` — Get full content of a specific post
- `get_post_categories(id)` — Get categories for a post
- `set_post_categories(id, categories)` — Set categories for a post
- `create_category(name, slug, description)` — Create a new category
- `update_category(id, fields)` — Update category name/slug/description
- `delete_category(id)` — Delete a category
- `export_all()` — Bulk export all posts with content and categories

## Notes for Contributors

- This tool is designed to be driven by Claude Code, not run as a standalone script
- The CLAUDE.md file is the primary interface — it tells Claude how to use the tool
- PHP scripts in `lib/` are meant to be run via `wp eval-file` or called via REST API
- Keep the adapter layer thin — just translate between connection methods and a common interface
