# Taxonomist

AI-powered WordPress category taxonomy optimizer, built for [Claude Code](https://claude.ai/code).

Analyzes every post on your WordPress blog and suggests an improved category structure — merging duplicates, retiring dead categories, creating missing ones, writing descriptions, and re-categorizing posts with full content analysis.

## Quick Start

```bash
git clone https://github.com/m/taxonomist.git
cd taxonomist
claude
```

Then tell Claude: **"Analyze and optimize my WordPress categories at example.com"**

Claude will walk you through:
1. Connecting to your WordPress site
2. Exporting all posts and categories
3. AI analysis of every post's content
4. Suggesting taxonomy improvements
5. Applying changes (with your approval at every step)

## Requirements

- [Claude Code](https://claude.ai/code) CLI
- A WordPress site you have admin access to

## Connection Methods

Taxonomist supports multiple ways to connect to your WordPress site:

| Method | Best for | Requirements |
|---|---|---|
| **WP-CLI over SSH** | Full-access servers | SSH + WP-CLI on server |
| **WP-CLI local** | Local development | WP-CLI + local WordPress |
| **WordPress.com API** | WordPress.com sites, Jetpack-connected sites | WordPress.com account or Jetpack |
| **REST API** | Managed hosting | WordPress 5.6+, Application Password |
| **XML-RPC** | Legacy setups | XML-RPC enabled |

The WordPress.com API works for both WordPress.com-hosted sites and self-hosted WordPress sites connected via Jetpack — covering millions of sites.

Don't know which to use? Just run the tool — it will probe your site and recommend the best method.

## What It Does

- **Finds duplicates**: "Tech" vs "Technology", "Switcher" vs "Switchers"
- **Retires dead categories**: Old categories with 1-2 posts about defunct services
- **Creates missing categories**: Identifies topics you write about but haven't categorized
- **Re-categorizes posts**: Uses AI to read every post and suggest the right categories
- **Writes descriptions**: Generates clear descriptions for every category
- **Rescues hidden categories**: Finds categories with few posts that should have many more

## Safety

- **Full backup** before any changes
- **Every change logged** with enough detail to undo exactly
- **Dry-run mode** — preview all changes before applying
- **Iterative** — you approve every step
- **Revert** — restore to the exact previous state at any time

## How It Works

Posts are exported locally and split into batches of ~200. Parallel AI agents analyze the full content of every post (not just keywords) and suggest optimal categories. Results are aggregated, reviewed with you, and applied via WP-CLI or REST API.

## License

[GPLv2 or later](LICENSE), same as WordPress.
