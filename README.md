# Taxonomist

AI-powered WordPress category taxonomy optimizer, built for [Claude Code](https://claude.ai/code).

Analyzes every post on your WordPress blog and suggests an improved category structure — merging duplicates, retiring dead categories, creating missing ones, writing descriptions, and re-categorizing posts with full content analysis.

## Quick Start

**macOS / Linux:**
```bash
curl -sL https://github.com/m/taxonomist/archive/main.tar.gz | tar xz && cd taxonomist-main && claude "start"
```

**Windows (PowerShell):**
```powershell
Invoke-WebRequest https://github.com/m/taxonomist/archive/main.zip -OutFile taxonomist.zip; Expand-Archive taxonomist.zip; cd taxonomist\taxonomist-main; claude "start"
```

[Install Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview) first if you haven't already.

## What It Does

- **Finds duplicates**: "Tech" vs "Technology", "Switcher" vs "Switchers"
- **Retires dead categories**: Old categories with 1-2 posts about defunct services
- **Creates missing categories**: Identifies topics you write about but haven't categorized
- **Re-categorizes posts**: Uses AI to read every post and suggest the right categories
- **Writes descriptions**: Generates clear descriptions for every category
- **Rescues hidden categories**: Finds categories with few posts that should have many more

## Connection Methods

Works with any WordPress site:

- **WordPress.com** — hosted sites and Jetpack-connected self-hosted sites (millions of sites)
- **WP-CLI** — over SSH or locally
- **REST API** — with Application Passwords
- **XML-RPC** — legacy fallback

Don't know which to use? Just run the tool — it figures it out.

## Safety

Every change is logged and reversible. Full backup before any modifications. Dry-run mode previews changes before applying. You approve every step.

## How It Works

Posts are exported locally and split into batches. Parallel AI agents analyze the full content of every post and suggest optimal categories. Results are aggregated, reviewed with you, and applied via WP-CLI or REST API.

## Thanks

Thanks to Automattician [Arun Sathiya](https://github.com/arunsathiya) for help testing.

## License

[GPLv2 or later](LICENSE), same as WordPress.
