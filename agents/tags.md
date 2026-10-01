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

`data/export/tags.json`'s `term_id`/`slug` are authoritative for later updates/deletes, exactly like `data/export/categories.json` — resolve against this file via `lib.helpers.resolve_tag_export_row()`, never guess a slug from a display name.

Report the total tag count and a top-20-by-count distribution, same as the category export's post-export summary.

## Step 2 — Backup (hard gate)

Before any analysis, create a backup — `WpcomAdapter.backup()` already captures tags (`tags`/`post_tags`/`total_tags`) alongside categories in the same call, so if a category-pass backup from this session is recent, a fresh one isn't strictly required, but err on the side of a fresh backup if any time has passed:

```python
from datetime import datetime, timezone
ts = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
adapter.backup(f'data/backups/pre-tag-analysis-{ts}.json')
```

**Validate it immediately, matching the hard-gate requirement in `agents/export.md`'s Backup section** — `lib.helpers.validate_backup()` doesn't check `tags`/`post_tags` specifically (they're additive fields, not in its required-keys list), but it does verify the backup is well-formed JSON with the base structure intact. Run it and STOP if invalid, same as the category pass.

**Known limitation — read before relying on this for tags specifically:** unlike categories, tag mutations do **not** yet have automated restore support. The backup captures tag state and `set_logging()`'s `tag_changes_log_path`/`tag_terms_log_path` record every tag change made during apply, but `restore()` doesn't know how to replay either yet. Tell the user this explicitly before applying anything: a tag-apply run is not currently a "one command to undo" operation the way a category-apply run is — recovery today means manually reading `tags`/`post_tags` back out of the backup JSON and reapplying by hand, or reading the change logs and reversing entries yourself. Get an explicit go-ahead from the user on this basis before applying tag changes, not just before applying category changes.

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

Write results to `data/tag-results/tag-result-NNN.json` (note the `tag-result-` prefix, not `result-` — `lib.helpers.aggregate_tag_results()` and `validate_tag_suggestions()`/`validate_tag_ids()` specifically look for this prefix so a tag-results directory can never be accidentally cross-read with a category-results directory in the same `data/` tree).

## Step 5 — Validate (hard gate, same standard as categories)

Before presenting anything to the user:

```python
from helpers import validate_result_ids, validate_tag_ids, aggregate_tag_results
import json

id_check = validate_result_ids('data/tag-results/', 'data/batches/')
# validate_result_ids works on any result-file naming; point batch_dir at
# whatever batches you split posts into for this pass.

tags = json.load(open('data/export/tags.json'))
valid_ids = {t['term_id'] for t in tags}
results = aggregate_tag_results('data/tag-results/')
tag_check = validate_tag_ids(results['suggestions'], valid_ids)
```

This is not optional, for the same reason it isn't for categories — an index-vs-ID mixup here would apply consolidation changes to the wrong posts.

## Step 6 — Plan & review

Present one table combining:
- The fragmentation groups from Step 3 (deterministic, high-confidence — present these as the primary recommendation).
- Any additional synonym merges or retire candidates from Step 4 (AI-assisted — flag confidence level).
- Any genuinely new tags proposed (should be rare; justify each one).

Use `AskUserQuestion` for the whole-plan approval, same as the category workflow — one approval step, not one question per tag group. Call out the restore limitation from Step 2 again here, right before asking for approval, so it's the last thing the user sees before saying yes.

## Step 7 — Apply

Re-check the capability probe if meaningful time has passed since Step 0, same as `agents/apply.md`'s re-check-before-apply step.

Enable tag logging alongside (or instead of) category logging:

```python
adapter.set_logging(
    tag_changes_log_path=f'data/logs/tag-changes-{ts}.tsv',
    tag_terms_log_path=f'data/logs/tag-terms-{ts}.tsv',
)
```

Execution order, mirroring `agents/apply.md`'s category order:
1. Create any approved new tags first (`adapter.create_tag(...)`), so they exist for post assignment.
2. Update tag descriptions if any were part of the plan.
3. Reassign posts: for each post whose tag set changes, call `adapter.set_post_tags(post_id, tag_ids, old_tag_ids=..., post_title=...)` — pass `old_tag_ids` from the export so the log entry is complete. Note `set_post_tags()` defaults `allow_clear=True` (unlike `set_post_categories()`'s `allow_clear=False` default) since a post ending up with zero tags is a normal, intended outcome for some merges/retires, not an accident.
4. Delete retired tags last, after every post referencing them has been reassigned — `adapter.delete_tag(term_id)`, resolved via the exported `term_id`, never a guessed slug.

Expect the same per-call latency (20–30s) and occasional `remote_request_timeout` behavior documented in `agents/apply.md`'s "Observed latency and timeout behavior" section — it's the identical WordPress.com/Jetpack API underneath, and `set_post_tags()` has the same read-back-on-timeout handling `set_post_categories()` does.

**Before this first real apply run against tags on any given site**, do a small live smoke test first: create one throwaway tag, assign it to a single test post via `set_post_tags()`, read the post back to confirm it landed, then delete the tag. This isn't paranoia for its own sake — `set_post_tags()`'s use of a `tags_by_id` parameter on the v1.2 posts endpoint is a documented *assumption* (by direct symmetry with categories' `categories_by_id`, and confirmed for reads), not yet empirically verified against a live POST. Confirming it once, cheaply, before running it across dozens of real posts is worth the extra two minutes.

## Step 8 — Verify

Same spot-check discipline as `agents/apply.md`'s Post-Apply Verification: re-fetch a sample of updated posts and confirm their tags match the plan, and scan any tag with many members for obvious mismatches.

## Revert

There is no automated tag revert yet (see the Step 2 limitation). If the user asks to undo a tag-apply run:
1. Point them at the pre-apply backup file's `tags`/`post_tags` keys and the `tag-changes-{timestamp}.tsv`/`tag-terms-{timestamp}.tsv` logs.
2. Offer to manually reconstruct and apply the inverse operations yourself, reading the logs the same way `agents/restore.md`'s inverse-replay does for categories, but do this as a one-off manual process — do not claim this is the same reliability guarantee the category `restore()` path provides.
