# Remove AGENTS.md to eliminate duplication with CLAUDE.md

## Problem

`AGENTS.md` duplicates content that already exists in `CLAUDE.md`. When `CLAUDE.md` is updated, `AGENTS.md` drifts out of sync, creating a maintenance burden and a risk of contradictory instructions reaching agents.

Since `CLAUDE.md` is the canonical source of truth for how the tool works, having a second file that restates the same information serves no purpose and actively causes confusion when the two diverge.

## Proposed solution

1. Audit `AGENTS.md` for any content that is **not** already covered in `CLAUDE.md` or the individual agent files in `agents/`.
2. Migrate any unique content into the appropriate location (`CLAUDE.md` or the relevant agent file).
3. Delete `AGENTS.md`.

## Acceptance criteria

- [ ] All unique content from `AGENTS.md` has been preserved in the correct location
- [ ] `AGENTS.md` is deleted
- [ ] No references to `AGENTS.md` remain in the codebase
- [ ] Existing agent workflows are unaffected
