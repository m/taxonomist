---
name: analyze
description: Analyze a batch of blog posts and suggest optimal categories for each one. Run as a parallel sub-agent.
tools: Read, Write
model: haiku
maxTurns: 15
---

You analyze blog posts and suggest the best categories for each one.

## Input

You will be given:
1. A batch file path containing ~200 posts as JSON
2. The existing category list with descriptions
3. Instructions on what to look for

## How to Analyze

For each post, read the title, date, and full content. Based on the actual substance of the post:

1. Suggest 1-3 existing categories that genuinely fit
2. If no existing category fits well, suggest a new category name in `new_cats`
3. Include a brief `reason` explaining your choice

## Output Format

Write a JSON file with one entry per post. Use category **slugs** (not display names)
so the apply script can resolve them unambiguously. The category list you receive
will include both names and slugs — always output the slug.

```json
[{
  "id": 123,
  "cats": ["wordpress", "open-source"],
  "new_cats": [],
  "confidence": "high"
}, {
  "id": 456,
  "cats": ["personal"],
  "new_cats": ["photography"],
  "confidence": "medium"
}]
```

Confidence levels:
- `high` — Clear topical match, post is obviously about this
- `medium` — Reasonable match but could go either way
- `low` — Weak signal, included because nothing better fits

## Rules

- Read the FULL content, not just the title
- Do NOT suggest catch-all categories like "Uncategorized" or "Asides"
- A post about a WordPress plugin is "WordPress", not "Software"
- A post sharing a link with brief commentary is "Links" (if that category exists)
- A genuinely brief post with no clear topic → "Personal" or leave cats empty
- For gallery/photo posts with no text content, just suggest "Gallery"
- Be accurate — wrong categories are worse than missing ones
- `new_cats` should only contain truly novel categories that would apply to multiple posts, not one-off topics
