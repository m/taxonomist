---
name: tags
description: Analyze and optimize a WordPress site's tags (post_tag taxonomy) — a separate, optional pass from the category workflow in export.md/analyze.md/apply.md.
tools: Bash, Read, Write
model: sonnet
maxTurns: 30
---

You analyze and optimize a WordPress site's tags. This is a separate, optional pass from the main category workflow — a user runs this in addition to (not instead of) `agents/export.md` / `agents/analyze.md` / `agents/apply.md`, typically after the category pass, since the two taxonomies are independent and a user may only want one.

## Why tags need a different lens than categories

Categories are few, curated, and hierarchical. Their dominant failure mode is **over-concentration** — one catch-all category (often the site default) eating a large share of posts because nothing more specific was ever assigned.

Tags are the opposite. A blog accumulates tags freely over its lifetime — often one per post idea, never curated, no hierarchy, no default term to fall back on. Their dominant failure mode is **fragmentation**: hundreds of near-duplicate, mostly-singleton tags. `"WordPress"`, `"wordpress"`, `"word-press"`, and `"Wordpress"` existing as four separate terms, each used once or twice, is a far more common tag problem on a real site than any one tag being overused. On one real site checked while building this workflow: **199 tags across 115 posts** — a ~1.7-posts-per-tag ratio dominated by singleton and case/hyphenation-variant tags.

This means the plan you present will look different in character from a category plan:
- **Merge candidates dominate**, not new-category proposals. The interesting output of analysis is mostly "these N tags are the same idea, consolidate them," not "here's a new tag we're missing."
- **Retiring a low-use tag is normal and expected**, not a sign something went wrong. A tag used on one post that adds no findability is a legitimate retire candidate — there's no "everything needs at least one tag" pressure the way categories have a default-term concern.
- **No hierarchy tree, no default-term protection.** Tags are flat, so skip `render_category_tree()` entirely for this pass; there's nothing to reparent, and no default-tag setting to check before deleting one.

## Setup

Connection and capability verification are identical to the category workflow — reuse `agents/connect.md`'s config.json and its capability probe. In WordPress core, tags use the **same `manage_categories` capability** as categories (verified: a live site's authenticated-user capabilities object has a single `manage_categories` key covering both taxonomies, no separate `manage_post_tags`/`manage_tags` key). If the capability check already passed for the category workflow in this session, it's already satisfied for tags — no need to re-probe.

## Step 1 — Export

Both `wpcom-api` and `wp-cli-*` connections are supported. Each step below gives the WordPress.com version first and the WP-CLI version after it where they differ.

Export all tags and each post's tag assignments to local JSON:

```python
import sys, json
sys.path.insert(0, 'lib')
sys.path.insert(0, 'lib/adapters')
from wpcom_adapter import WpcomAdapter

config = json.load(open('config.json'))
adapter = WpcomAdapter(config)

tags = adapter.list_tags()
export_tags = [{
    'term_id': t['ID'],
    'name': t['name'],
    'slug': t['slug'],
    'description': t.get('description', ''),
    'count': t.get('post_count', 0),
} for t in tags]
json.dump(export_tags, open('data/export/tags.json', 'w'), indent=2)

# export_posts() already captures tags/tag_ids/tag_slugs alongside
# categories in the same pass — reuse the category export if this run
# is happening right after the category workflow, rather than
# re-fetching from the API.
adapter.export_posts('data/export/posts.json')
```

For `wp-cli-ssh` / `wp-cli-local`, `WpCliAdapter.list_tags()` already returns `term_id`/`count`, and `lib/export-posts.php` writes the same `tags`/`tag_ids`/`tag_slugs` fields per post:

```python
import sys, json
sys.path.insert(0, 'lib')
sys.path.insert(0, 'lib/adapters')
from wp_cli_adapter import WpCliAdapter

config = json.load(open('config.json'))
adapter = WpCliAdapter(config)

json.dump(adapter.list_tags(), open('data/export/tags.json', 'w'), indent=2)
adapter.export_posts('data/export/posts.json')
```

`data/export/tags.json`'s `term_id`/`slug` are authoritative for later updates/deletes, exactly like `data/export/categories.json` — resolve against this file via `lib.helpers.resolve_tag_export_row()`, never guess a slug from a display name.

Report the total tag count and a top-20-by-count distribution, same as the category export's post-export summary.

## Step 2 — Backup (hard gate)

Before any analysis, create a backup — `WpcomAdapter.backup()` and `lib/backup.php` both capture tags (`tags`/`post_tags`/`total_tags`) alongside categories in the same call, so if a category-pass backup from this session is recent, a fresh one isn't strictly required, but err on the side of a fresh backup if any time has passed:

```python
from datetime import datetime, timezone
ts = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
adapter.backup(f'data/backups/pre-tag-analysis-{ts}.json')
```

On `wp-cli-ssh` / `wp-cli-local` the same call works. `WpCliAdapter.backup()` uploads `lib/backup.php` over SSH and copies the backup back to the local path.

**Validate it immediately, matching the hard-gate requirement in `agents/export.md`'s Backup section** — `lib.helpers.validate_backup()` doesn't check `tags`/`post_tags` specifically (they're additive fields, not in its required-keys list), but it does verify the backup is well-formed JSON with the base structure intact. Run it and STOP if invalid, same as the category pass.

**Known limitation — read before relying on this for tags specifically:** unlike categories, tag mutations do **not** yet have automated restore support. The backup captures tag state and `set_logging()`'s `tag_changes_log_path`/`tag_terms_log_path` (or, on WP-CLI, `lib/apply-changes.php`'s change log) record every tag change made during apply, but neither `restore()` nor `lib/restore.php` knows how to replay them yet. Tell the user this explicitly before applying anything: a tag-apply run is not currently a "one command to undo" operation the way a category-apply run is — recovery today means manually reading `tags`/`post_tags` back out of the backup JSON and reapplying by hand, or reading the change logs and reversing entries yourself. Get an explicit go-ahead from the user on this basis before applying tag changes, not just before applying category changes.

## Step 3 — Find fragmentation candidates

Before any AI analysis, run the cheap, deterministic pass first — it does most of the real work for tags:

```python
from helpers import find_similar_tags
import json

tags = json.load(open('data/export/tags.json'))
groups = find_similar_tags(tags)
for group in groups:
    names = [t['name'] for t in group]
    total = sum(t['count'] for t in group)
    print(f"{names} -> {total} posts combined")
```

This groups tags by normalized name (case, punctuation, simple plural stripping) — deliberately conservative, exact-match-after-normalization only, no fuzzy scoring, so it won't suggest merging genuinely unrelated tags. Every group it returns is a near-certain merge candidate; review them yourself for anything that looks wrong before including them in the plan, but expect the large majority to be correct.

This pass will not catch every duplicate (synonyms like `"js"` / `"javascript"` normalize differently and need AI judgment in Step 4), but it should resolve a large fraction of a fragmented tag cloud with no AI cost at all.

## Step 4 — AI analysis for what the deterministic pass can't catch

For posts and tags the fragmentation pass didn't resolve, use parallel AI agents — same batching infrastructure as the category workflow (`lib.helpers.write_batches()`, `check_largest_batch()`), same "read full post content" principle. Two things to ask the agents for, per post:

1. **Synonym/near-duplicate detection the normalization pass missed** — semantically identical tags with different words (`"js"`/`"javascript"`, `"pics"`/`"photos"`).
2. **Retire candidates** — tags used on this post that add no findability value (overly specific, a one-off detail rather than a topic).

Do **not** ask agents to invent new tags freely, the way `agents/analyze.md` asks for new categories. Tags already over-proliferate; the job here is consolidation, not expansion. Only flag a genuinely missing tag if a post's content is about a clear, recurring topic with no tag at all and that gap is likely to repeat across other posts.

Output format — mirrors `agents/analyze.md`'s suggestion shape, using `tags`/`new_tags` instead of `cats`/`new_cats`:

```json
[{
  "post_id": 123,
  "tags": [12, 34],
  "new_tags": [],
  "confidence": "high"
}]
```

Split tag batches into their own directory, not the category pass's `data/batches/`:

```python
import json
from helpers import write_batches, check_largest_batch
posts = json.load(open('data/export/posts.json'))
write_batches(posts, 'data/tag-batches/')
ok, largest_file, largest_chars = check_largest_batch('data/tag-batches/')
```

`write_batches()` deletes any existing `batch-*.json` files in the directory it writes to, so reusing `data/batches/` would wipe the category pass's batches (and break that pass's `validate_result_ids()` check if it hasn't run yet).

Write results to `data/tag-results/tag-result-NNN.json`, one per `batch-NNN.json`. Note the `tag-result-` prefix, not `result-`: `lib.helpers.aggregate_tag_results()` only reads files with this prefix, and `validate_result_ids()` reads them when called with `result_prefix='tag-result-'` (Step 5), so tag results and category results are never cross-read.

## Step 5 — Validate (hard gate, same standard as categories)

After analysis and before presenting anything to the user, you MUST run every check below. This is not optional, for the same reason it isn't for categories (see `AGENTS.md`'s Result Validation section): an index-vs-ID mixup here would apply consolidation changes to the wrong posts.

```python
import json, os
from helpers import (
    validate_result_ids, validate_tag_suggestions, validate_tag_ids,
    aggregate_tag_results,
)

errors = []

# 1. Post IDs: every result post_id must exist in the tag batches, and
#    every batch post must have a result entry.
id_check = validate_result_ids(
    'data/tag-results/', 'data/tag-batches/', result_prefix='tag-result-',
)
errors += id_check['errors']

# 2. Structure: every entry needs a post_id and a `tags` list of ints.
#    This is the only check that catches a missing `tags` key; the others
#    treat a missing key as an empty list, and set_post_tags() allows an
#    empty list by default, so a malformed entry would strip every tag
#    from the post.
for filename in sorted(os.listdir('data/tag-results/')):
    if filename.startswith('tag-result-') and filename.endswith('.json'):
        with open(os.path.join('data/tag-results/', filename)) as f:
            check = validate_tag_suggestions(json.load(f))
        errors += [f'{filename}: {e}' for e in check['errors']]

# 3. Tag IDs: every tag ID must be a known term.
tags = json.load(open('data/export/tags.json'))
valid_ids = {t['term_id'] for t in tags}
results = aggregate_tag_results('data/tag-results/')
errors += validate_tag_ids(results['suggestions'], valid_ids)['errors']

for error in errors:
    print(error)
print('suspect index files:', id_check['suspect_index_files'])
```

If `errors` is non-empty, STOP. Do not build a plan or apply anything until every error is resolved:
- If `suspect_index_files` is non-empty, those result files MUST be re-generated by re-running the analysis for their batches. Do NOT attempt to fix them by mapping indices to IDs.
- Any other structural error or missing post also means re-running the analysis for that batch, not editing the result file by hand.
- Unknown tag IDs must be resolved (a stale ID from a re-exported taxonomy, or a new tag the user hasn't approved yet) before applying.

## Step 6 — Plan & review

Present one table combining:
- The fragmentation groups from Step 3 (deterministic, high-confidence — present these as the primary recommendation).
- Any additional synonym merges or retire candidates from Step 4 (AI-assisted — flag confidence level).
- Any genuinely new tags proposed (should be rare; justify each one).

Use `AskUserQuestion` for the whole-plan approval, same as the category workflow — one approval step, not one question per tag group. Call out the restore limitation from Step 2 again here, right before asking for approval, so it's the last thing the user sees before saying yes.

## Step 7 — Apply

Re-check the capability probe if meaningful time has passed since Step 0, same as `agents/apply.md`'s re-check-before-apply step.

**WordPress.com.** Enable tag logging alongside (or instead of) category logging:

```python
adapter.set_logging(
    tag_changes_log_path=f'data/logs/tag-changes-{ts}.tsv',
    tag_terms_log_path=f'data/logs/tag-terms-{ts}.tsv',
)
```

Execution order, mirroring `agents/apply.md`'s category order:
1. Create any approved new tags first (`adapter.create_tag(...)`), so they exist for post assignment.
2. Update tag descriptions if any were part of the plan.
3. Reassign posts: for each post whose tag set changes, call `adapter.set_post_tags(post_id, tag_ids, old_tag_ids=..., post_title=...)` — pass `old_tag_ids` from the export so the log entry is complete. If it raises `tags_drift`, follow the handling below. Note `set_post_tags()` defaults `allow_clear=True` (unlike `set_post_categories()`'s `allow_clear=False` default) since a post ending up with zero tags is a normal, intended outcome for some merges/retires, not an accident.
4. Delete retired tags last, after every post referencing them has been reassigned — `adapter.delete_tag(term_id)`, resolved via the exported `term_id`, never a guessed slug.

Expect the same per-call latency (20–30s) and occasional `remote_request_timeout` behavior documented in `agents/apply.md`'s "Observed latency and timeout behavior" section — it's the identical WordPress.com/Jetpack API underneath, and `set_post_tags()` has the same read-back-on-timeout handling `set_post_categories()` does.

**WP-CLI.** Use `lib/apply-changes.php` in tag mode for the post reassignments (step 3 above), not a loop of `set_post_tags()` calls. Build one suggestions file from the approved plan where each entry's `tags` is the post's complete new tag list (`[]` clears it), then preview before applying:

```python
# Preview (default mode)
print(adapter.apply_tag_changes(
    'data/tag-suggestions.json', f'data/logs/tag-changes-{ts}.tsv',
))

# Apply
print(adapter.apply_tag_changes(
    'data/tag-suggestions.json', f'data/logs/tag-changes-{ts}.tsv',
    mode='apply',
))
```

Over SSH, `apply_tag_changes()` uploads the script and the suggestions file, then copies the change log back to the local path, including when the run fails. That log is the only post-level record for a tag revert. If the copy back fails, the call raises an error naming the path still on the server: fetch that file before doing anything else.

Tag mode replaces each post's tag set rather than adding to it, rejects any entry without a `tags` list, and writes `SET_TAGS` rows with the same columns as the WordPress.com tag change log. Create and delete tags with `WpCliAdapter.create_tag()` / `delete_tag(term_id)`, in the same order as above. WordPress resolves the integer tag IDs by ID, so `tags_drift` does not apply to WP-CLI runs and the smoke test below is only needed for WordPress.com.

**Handling `tags_drift`** (WordPress.com only). `tags_by_id` does not resolve tags the way `categories_by_id` resolves categories. The v1.2 posts endpoint turns each tag ID into the tag's *name*, and WordPress core then looks that name up by slug first. If a tag's name sanitizes to a slug that belongs to a different tag, the post gets the other tag. This is most likely on sites with renamed or case-variant tags, which is exactly what a fragmentation cleanup works on: keeping `"Wordpress"` and retiring `"WordPress"` can send posts to the tag that is about to be deleted. `set_post_tags()` compares the post's resulting tags to what it sent and raises `WpcomApiError` with error `tags_drift` on any mismatch.

When `set_post_tags()` raises `tags_drift`:
1. STOP the apply run. Do not continue to other posts, and do not run any Step 7.4 deletes.
2. The POST already succeeded, so the post now has the wrong tags, and no change-log row was written for it. Restore it with `set_post_tags(post_id, old_tag_ids, old_tag_ids=old_tag_ids)`, using its `tag_ids` from `data/export/posts.json`.
3. If the restore raises `tags_drift` too (the same collision can affect the post's original tags), do not retry. Give the user the post ID, the tag IDs it should have, and the tags it ended up with from the error message, so they can fix it in wp-admin.
4. Report the colliding tags to the user (the `dropped`/`extra` IDs in the error message) and revise the plan for them before resuming, for example by keeping the other tag of the pair as the merge target.

**Before this first real apply run against tags on any given WordPress.com site**, do a small live smoke test first: create one throwaway tag, assign it to a single test post via `set_post_tags()`, read the post back to confirm it landed, then delete the tag. The POST side of `tags_by_id` has not yet been verified against a live site (reads are confirmed), so confirming it once, cheaply, before running it across dozens of real posts is worth the extra two minutes. A passing smoke test only shows that the basic POST works. It does not show that renamed or case-variant tags are safe, so the `tags_drift` handling above still applies to every call.

## Step 8 — Verify

Same spot-check discipline as `agents/apply.md`'s Post-Apply Verification: re-fetch a sample of updated posts and confirm their tags match the plan, and scan any tag with many members for obvious mismatches.

## Revert

There is no automated tag revert yet (see the Step 2 limitation). If the user asks to undo a tag-apply run:
1. Point them at the pre-apply backup file's `tags`/`post_tags` keys and the `tag-changes-{timestamp}.tsv`/`tag-terms-{timestamp}.tsv` logs (WP-CLI runs write only `tag-changes-{timestamp}.tsv`; tags it deleted are in the backup's `tags` list).
2. Offer to manually reconstruct and apply the inverse operations yourself, reading the logs the same way `agents/restore.md`'s inverse-replay does for categories, but do this as a one-off manual process — do not claim this is the same reliability guarantee the category `restore()` path provides.
