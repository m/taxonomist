# Add incremental resume support for batch analysis

## Problem

If a batch analysis fails partway through (e.g., batch 47 of 80), the entire analysis must be re-run from scratch. On large sites with thousands of posts, this wastes significant time and API calls. Network interruptions, rate limits, or session timeouts can all cause partial failures that currently require a full restart.

## Current behavior

- Posts are split into batches and written to `data/batches/`
- Parallel analyze agents process each batch and write results to `data/results/`
- If the session is interrupted, there is no mechanism to detect which batches completed successfully and resume from the next unfinished one
- `helpers.py:write_batches()` clears stale batch files on every run, so previous batch splits are lost

## Proposed solution

1. **Detect completed work**: Before launching analyze agents, check `data/results/` for existing `result-NNN.json` files that pass validation (`helpers.validate_suggestions()`).
2. **Skip completed batches**: Only launch agents for batches that don't have a valid corresponding result file.
3. **Preserve batches on resume**: `write_batches()` should not clear existing batch files when the post set hasn't changed. Add a check (e.g., hash of post IDs) to determine whether batches need regenerating.
4. **Report progress**: Log how many batches were skipped vs. queued so the user knows resume is working.

## Considerations

- Batch numbering must remain stable across runs for resume to work. If batch sizes change between runs, results from a previous split won't align. The resume logic should detect this and warn the user.
- A `--fresh` flag or equivalent should be available to force a full re-analysis when the user wants to discard partial results.

## Testing

Most of the resume logic lives in `helpers.py` and can be unit tested using the same `tempfile`/`tempdir` patterns already established in `tests/test_helpers.py`.

### Unit tests (add to `test_helpers.py`)

**Completed batch detection**
- Write valid `result-NNN.json` files for some batches, leave others missing. Call the resume function and assert it returns the correct set of incomplete batch numbers.
- Cases: all complete, none complete, gaps in the middle (e.g., 000 and 002 exist but 001 is missing), corrupt/invalid result files treated as incomplete.

**Batch stability check**
- Write batches from a known post list. Call the stability check with the same post list — should return stable. Call with a different post list (added/removed/reordered posts) — should return changed.
- This tests the hash/fingerprint comparison that prevents resuming against a stale batch split.

**Stale file cleanup respects resume mode**
- Current `write_batches()` deletes all old `batch-NNN.json` files. Add tests that confirm: in resume mode, existing batch files are preserved; when forcing a fresh run, they are cleared as before.

**Aggregation with mixed old/new results**
- `TestAggregateResults` already covers basic multi-file aggregation. Add a case where result files were produced in separate sessions (partial overlap of post IDs) and confirm deduplication still works correctly with later files winning.

### Integration test (manual)

Interrupting a session mid-analysis is impractical to automate. The manual smoke test is:

1. Run the export and analysis on a real site
2. After some `result-NNN.json` files appear in `data/results/`, kill the session
3. Restart and trigger analysis again
4. Confirm it skips completed batches and only runs the remaining ones
5. Confirm the final aggregated result matches a full clean run

### Not worth testing separately

The `--fresh` flag is just "delete the results directory and proceed normally." The existing tests already cover the fresh-run code path, so a dedicated test adds no value.

## Acceptance criteria

- [ ] Resuming after a partial failure only re-analyzes incomplete batches
- [ ] Previously completed results are preserved and reused
- [ ] Changed post sets or batch sizes trigger a warning and full re-run
- [ ] Users can force a clean re-analysis when desired
- [ ] `aggregate_results()` works correctly with a mix of old and new result files
- [ ] Unit tests cover completed batch detection, batch stability checks, resume-mode file preservation, and mixed-session aggregation
