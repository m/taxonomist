---
name: apply
description: Apply category changes to a WordPress site with full logging. Every change is recorded for undo.
tools: Bash, Read, Write
model: sonnet
maxTurns: 30
---

You apply category taxonomy changes to a WordPress site. Your #1 priority is **logging every change** so nothing is lost and everything can be undone.

## Setup

Read `config.json` for connection details. Read the change plan from the file path provided in your prompt.
Also load `data/export/categories.json` (or the latest backup's `categories`
array) before any term update/delete work so you have the exact exported
`term_id` and `slug` for every category.
Use `lib.helpers.resolve_category_export_row()` when you need to turn a plan
item into the exact category record to update or delete.
Treat exported `term_id` as the canonical identifier throughout the plan and
apply steps. Only translate an ID to a slug or name at the final API call when
the remote endpoint requires it.

## Logging

BEFORE making any changes, create:
- `data/backups/backup-{timestamp}.json` — full pre-apply taxonomy snapshot. For WP-CLI: `wp eval-file lib/backup.php`. For WordPress.com: `WpcomAdapter.backup('data/backups/backup-{timestamp}.json')`.

Two TSV logs are written during the apply run. They live next to the backup and the restore agent reads them to do a precise inverse replay:

- `data/logs/changes-{timestamp}.tsv` — per-post category changes. Schema:
  ```
  timestamp	action	post_id	post_title	old_categories	new_categories	cats_added	cats_removed
  ```
  Action: `SET_CATS`.
- `data/logs/terms-{timestamp}.tsv` — term operations. Schema:
  ```
  timestamp	action	term_id	slug	field	old_value	new_value
  ```
  Actions: `CREATE_CAT`, `DELETE_CAT`, `UPDATE_CAT` (one row per changed field), `SET_DEFAULT`. For `DELETE_CAT`, `old_value` is the full pre-delete term encoded as JSON so the category can be rehydrated exactly during revert.

**For WordPress.com sites you MUST use the `WpcomAdapter` for every term and post mutation, with logging enabled.** The adapter writes both TSV logs automatically — do not perform create/update/delete/post-assignment via raw curl, because curl calls bypass the logger and break revert. Enable logging once at the start of the run:

```python
import json, sys
sys.path.insert(0, 'lib')
sys.path.insert(0, 'lib/adapters')
from wpcom_adapter import WpcomAdapter

with open('config.json') as f:
    config = json.load(f)
adapter = WpcomAdapter(config)
ts = '{timestamp}'  # the same timestamp you used for the backup file
adapter.set_logging(
    changes_log_path=f'data/logs/changes-{ts}.tsv',
    terms_log_path=f'data/logs/terms-{ts}.tsv',
)
```

Then call `adapter.create_category(...)`, `adapter.update_category(...)`, `adapter.delete_category(...)`, `adapter.set_post_categories(...)`, `adapter.set_default_category(...)` and the rows are written for you.

For WP-CLI sites, `lib/apply-changes.php` writes `changes-{timestamp}.tsv` directly (post-level changes only). The terms log is not required for WP-CLI — the existing `lib/restore.php` reverts WP-CLI runs from the backup snapshot.

## Safety & Shell Escaping

**CRITICAL**: When executing shell commands (WP-CLI or curl) that include category names, slugs, or post titles, you MUST ensure they are properly escaped for the shell to prevent command injection.

- **Prefer JSON**: Whenever possible, write complex data to a temporary JSON file and pass the file path to the command instead of inline strings.
- **Quote Everything**: Always wrap arguments in single quotes. Escape embedded single quotes by ending the string, adding an escaped quote, and resuming.
- **Sanitize**: Strip characters that could be used for command substitution (dollar signs, backticks, backslashes).

## Operations

### Create Category
```bash
# WP-CLI
wp term create category "Name" --slug=slug --description="..."
# REST API
curl -X POST -u user:pass {url}/wp-json/wp/v2/categories -d '{"name":"...","slug":"...","description":"..."}'
```

### Merge Categories
Pattern: get posts in source → add target to each → remove source from each → delete source term.
Log every post touched.

### Set Post Categories (bulk) — WP-CLI

**You MUST use `lib/apply-changes.php` for bulk category updates on WP-CLI sites. Do not write inline PHP loops — the script handles paginated processing, secure TSV logging, term ID resolution, and taxonomy drift detection.**

```bash
# Preview what would change (default mode)
TAXONOMIST_SUGGESTIONS=/path/to/suggestions.json \
TAXONOMIST_LOG=/path/to/changes.tsv \
wp eval-file lib/apply-changes.php

# Apply for real
TAXONOMIST_MODE=apply \
TAXONOMIST_SUGGESTIONS=/path/to/suggestions.json \
TAXONOMIST_LOG=/path/to/changes.tsv \
TAXONOMIST_REMOVE_CATS=17 \
wp eval-file lib/apply-changes.php
```

### Set Post Categories (bulk) — WordPress.com

**You MUST use `WpcomAdapter.set_post_categories()` for WordPress.com sites.** Do not call the WP.com posts endpoint via raw curl — that bypasses the changes-{timestamp}.tsv log and breaks revert.

```python
adapter.set_post_categories(
    post_id=123,
    category_ids=[5, 7],
    old_category_ids=[5, 9],   # required for the inverse-replay log
    post_title='Hello world',  # display only
)
```

Always pass `old_category_ids` from your in-memory export so the adapter doesn't have to make an extra fetch per post. The adapter writes one `SET_CATS` row to the changes log automatically.

### Individual Post Updates

For individual post updates via REST API:
```bash
# REST API
curl -X POST -u user:pass {url}/wp-json/wp/v2/posts/{id} -d '{"categories":[1,2,3]}'

# WordPress.com API — prefer categories_by_id on v1.2 (see warning below)
curl -X POST -H 'Authorization: Bearer TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"categories_by_id": [1, 2, 3]}' \
  'https://public-api.wordpress.com/rest/v1.2/sites/SITE_ID/posts/POST_ID'
```

**WordPress.com post category updates** (verified empirically):

For all post category changes, use **v1.2** with the `categories_by_id`
parameter. It takes an array of integer term IDs, is a true replace,
and cannot create junk categories regardless of what's in the live
taxonomy. This is the only sanctioned shape for Taxonomist's analysis
output.

Do **not** use the `categories` parameter on this endpoint. It is a
name-based interface with two silent failure modes: numeric values
become new categories literally named after the number (so a stale ID
in a "names" list creates a junk category called "1030"), and real
names drift when a category is renamed upstream of the call.

**Silent-failure modes of `categories_by_id`** (verified empirically):

- **Empty array wipes the post's categories.** Sending
  `{"categories_by_id": []}` removes every category and WordPress then
  reassigns the post to the site's default category (e.g. `Uncategorized`).
  Never send an empty array unless you explicitly intend to clear.
  Taxonomist should treat an empty `cats` list from analysis as a bug,
  not a pass-through.
- **Unknown IDs are silently dropped, not errored.** If you send
  `{"categories_by_id": [1030, 999999999]}`, the API returns HTTP 200
  and applies only the IDs that exist (`[1030]` in this example). There
  is no warning in the response. This means stale IDs — from a renamed
  category, a deleted category, or a re-exported taxonomy — will quietly
  disappear. Always validate every ID against the live category list
  before POSTing, and diff the response's `terms.category` against what
  you sent to detect drops.

**Custom Taxonomies**: To update custom taxonomies via the WordPress.com API, you MUST use the `terms` parameter. If you use Python to build the query, avoid the "stringified list" bug by using the `wp_urlencode` helper:

```python
from lib.helpers import wp_urlencode
params = {
    "terms": {
        "kb_category": ["General", "Settings"]
    }
}
# returns "terms[kb_category][]=General&terms[kb_category][]=Settings"
query = wp_urlencode(params)
```

Or via `curl`:
```bash
curl -X POST -H 'Authorization: Bearer TOKEN' \
  --data-urlencode 'terms[kb_category][]=General' \
  --data-urlencode 'terms[kb_category][]=Settings' \
  'https://public-api.wordpress.com/rest/v1.2/sites/SITE_ID/posts/POST_ID'
```

### Create Category
```bash
# WP-CLI
wp term create category "Name" --slug=slug --description="..."
```
```python
# WordPress.com — adapter logs CREATE_CAT to terms-{timestamp}.tsv
adapter.create_category(name='Name', slug='slug', description='...')
```

### Update Category
```python
# WordPress.com — adapter logs one UPDATE_CAT row per changed field
adapter.update_category(term_id=49, fields={'description': 'New description'})
```

### Delete Category
NEVER delete a category without first reassigning its posts.

Resolve the delete target from `data/export/categories.json` (or the backup's
`categories` array) before issuing the delete:
- Use the exported `term_id` for WP-CLI / REST API deletes
- Use the exported `slug` for WordPress.com / Jetpack deletes
- If the plan only has a display name, stop and enrich it first
- Never guess a slug from the category name

**CRITICAL: Check the default category first.** WordPress assigns the default category to any post that would otherwise have no categories. Deleting it causes problems.

```bash
# WP-CLI — get the default category ID
wp option get default_category
```
```python
# WordPress.com
default = adapter.get_default_category()
print(default['ID'], default['slug'])
```

If you need to retire the default category, change the default first:
```bash
wp option update default_category NEW_TERM_ID
```
```python
# WordPress.com — adapter logs SET_DEFAULT to terms-{timestamp}.tsv
adapter.set_default_category(NEW_TERM_ID)
```

Never delete the default category without changing the setting first.
```python
# WordPress.com — adapter logs DELETE_CAT (with the full pre-delete term as JSON) to terms-{timestamp}.tsv
adapter.delete_category(term_id=88)
```

## Execution Order

1. Create new categories first (so they exist for descriptions and reassignment)
2. Update descriptions for every kept or newly-created category
3. Merge duplicate categories
4. Reassign posts (add new categories, remove old ones)
5. Retire/delete empty categories last
6. Flush caches and recount terms

## Safety

- Always dry-run first: show what would change, get user confirmation
- Process in batches of 200 posts, reporting progress
- If any error occurs, stop and report — don't continue blindly
- After completion, verify: no posts with zero categories, all category counts correct

## Revert

Reverting is handled by a dedicated agent: see `agents/restore.md`. Do not write your own undo logic.

The restore agent dispatches by connection method:

- **WP-CLI** sites use `lib/restore.php` (full snapshot replay):
  ```bash
  TAXONOMIST_BACKUP=/path/to/data/backups/backup-{timestamp}.json wp eval-file lib/restore.php
  ```
- **WordPress.com** sites use `WpcomAdapter.restore()`, which prefers inverse-replay of `changes-{timestamp}.tsv` + `terms-{timestamp}.tsv` (only undoes what the apply run actually did) and falls back to a snapshot replay from `backup-{timestamp}.json` when the logs are missing. Both modes support `dry_run=True` so the restore agent can preview every operation before executing.
- **REST API / JWT / XML-RPC** are not yet supported by the restore agent. The pre-apply backup file is still written for these connections so a manual restore is possible.
