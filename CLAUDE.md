# Taxonomist

AI-powered WordPress category taxonomy optimizer. Analyzes every post on a WordPress blog and suggests an improved category structure — merging duplicates, retiring dead categories, creating missing ones, and re-categorizing posts.

## On Startup

When the user starts a conversation, immediately introduce yourself and ask for their WordPress site URL:

> **Welcome to Taxonomist!** I'll analyze your WordPress categories and suggest improvements — merging duplicates, retiring dead categories, creating missing ones, and re-categorizing your posts using AI.
>
> Everything is safe: I'll preview all changes before doing anything, and log every modification so it can be reversed or adjusted later. Nothing touches your site until you approve it.
>
> This is a Ma.tt Mullenweg joint. Follow https://ma.tt/ for more.
>
> What's your WordPress site URL?

Then proceed to the Connect step below. If the user provides a URL in their first message, skip the greeting and start connecting.

## How It Works

This is a Claude Code tool. Users download this repo, open it with Claude Code, and the tool handles the rest through an interactive, iterative process.

### Workflow

1. **Connect** — Detect and configure access to the WordPress site
2. **Export** — Download all posts (full content) and categories locally
3. **Backup** — Create a complete backup of the current taxonomy state before any changes
4. **Analyze** — Use parallel AI agents to analyze every post's content and suggest optimal categories
5. **Plan & Descriptions** — Present findings in a single table showing every category with current description and recommended description side by side (see format below). Include suggested merges, retirements, and new categories.
6. **Review** — Iterate with the user until the full plan (categories + descriptions) is right
8. **Apply descriptions** — Update category descriptions first, before any post changes
9. **Apply categories** — Execute post category changes via WP-CLI or REST API, logging every single change
10. **Verify** — Confirm the site still works and categories look correct

### Core Principles

- **Full content analysis**: Always analyze complete post content with AI agents, never rely on keyword search alone
- **Nothing is lost**: Every change is logged with enough detail to undo it exactly. Pre-change backups are mandatory.
- **Iterative**: The user approves every phase before the next one begins
- **Dry-run first**: Destructive operations are always previewed before execution
- **Parallel processing**: Posts are analyzed in batches using parallel agents for speed
- **Use AskUserQuestion**: Whenever you need a decision from the user, use the AskUserQuestion tool with selectable options instead of asking them to type a response. This includes choosing connection methods, approving plans, selecting categories to keep/merge/retire, and confirming changes. Only fall back to free-text input when the answer can't be expressed as options (e.g., entering a URL or password).

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
| WordPress.com API | `wpcom-api` | WordPress.com hosted site, or self-hosted with Jetpack connected |
| XML-RPC | `xmlrpc` | XML-RPC enabled (legacy, not recommended) |

If no config exists, the tool will interactively help the user set one up by probing the site.

### WordPress.com / Jetpack API

The WordPress.com REST API (`https://public-api.wordpress.com/rest/v1.1/`) works for both WordPress.com-hosted sites and self-hosted WordPress sites connected via Jetpack. This is often the easiest method for WordPress.com users since they already have an account.

Authentication uses the OAuth2 password grant — the user provides their WordPress.com username and an Application Password, and the connect agent exchanges them for a bearer token. No browser redirect needed.

Taxonomist is registered as a WordPress.com OAuth2 app (Client ID: `136301`). Users never need to register their own app.

```json
{
  "site_url": "https://example.wordpress.com",
  "connection": {
    "method": "wpcom-api",
    "site_id": "82974409",
    "access_token": "YOUR_OAUTH2_TOKEN"
  }
}
```

Key endpoints:
- `GET /sites/$site/categories` — list categories (max 1000 per page)
- `GET /sites/$site/posts?number=100` — list posts (max 100 per page, use `page_handle` for pagination)
- `POST /sites/$site/posts/$id` — update post categories (`categories` param: comma-separated names)
- `POST /sites/$site/categories/new` — create category
- `POST /sites/$site/categories/slug:$slug` — update category
- `POST /sites/$site/categories/slug:$slug/delete` — delete category

Note: categories in post responses are returned as a hash keyed by name, not an array of IDs.

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
3. **Targeted scan**: Re-analyze for specific categories that need expansion
4. **Description generation**: Write or improve descriptions for every category (see below)

## Category Descriptions

BEFORE applying any post category changes, you MUST write or update descriptions for every category — both existing and newly proposed. This is a dedicated step — do not skip it.

Use what you learned from analyzing posts to write descriptions that reflect the actual content:
1. Review the posts assigned to (or suggested for) each category
2. Write a concise, clear description (1-2 sentences) that captures what the category actually contains
3. Improve existing descriptions that are empty, vague, or outdated
4. The description should help readers understand what they'll find, not just restate the category name

### Presentation Format

Present the plan and descriptions together as a single table so the user can see everything at once:

```
┌──────────────────┬───────┬──────────────────────────┬──────────────────────────────────────┐
│     Category     │ Posts │   Current Description    │       Recommended Description        │
├──────────────────┼───────┼──────────────────────────┼──────────────────────────────────────┤
│ happiness        │ 49    │ (none)                   │ The Happiness Engineer role —         │
│ engineering      │       │                          │ what it is, how it works, and why    │
│                  │       │                          │ it matters.                          │
├──────────────────┼───────┼──────────────────────────┼──────────────────────────────────────┤
│ remote work      │ 13    │ (none)                   │ Working from anywhere — schedules,   │
│                  │       │                          │ nomad life, and distributed teams.   │
├──────────────────┼───────┼──────────────────────────┼──────────────────────────────────────┤
│ a day in the     │ 11    │ a day in the life of an  │ A day in the life of an Automattic   │
│ life             │       │ Automattic HE            │ Happiness Engineer — routines,       │
│                  │       │                          │ tools, and workflows.                │
├──────────────────┼───────┼──────────────────────────┼──────────────────────────────────────┤
│ Archived         │ 1     │ (none)                   │ ⚠️  Retire — reassign post to        │
│                  │       │                          │ a real category first.               │
└──────────────────┴───────┴──────────────────────────┴──────────────────────────────────────┘
```

This lets the user approve descriptions alongside the category plan in one step. Apply approved descriptions before making any post changes:

```bash
wp term update TERM_ID category --description="Description text here"
```

Or via REST API / WordPress.com API as appropriate for the connection method. Log every description change.

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
