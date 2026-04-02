# Add REST API and WordPress.com API adapters

## Problem

The adapter layer (`lib/adapters/`) currently only has a WP-CLI adapter. The REST API and WordPress.com API connection methods — which are the most accessible for non-technical users — have no structured adapter code. Instead, the logic for these methods lives entirely in agent prompt instructions, making it:

- **Fragile**: Agents must reconstruct API calls from prose instructions each session, with no shared code to reuse or test.
- **Inconsistent**: Different sessions may handle pagination, error responses, or edge cases differently.
- **Untestable**: There are no unit tests for REST/WPCOM operations because there's no code to test.

## Current state

`lib/adapters/wp_cli_adapter.py` implements the common interface:
- `list_categories()`
- `export_posts(output_path)`
- `set_post_categories(post_id, category_ids)`
- `create_category(name, slug, description)`
- `delete_category(term_id)`

The connect and export agents contain inline instructions for handling REST API and WordPress.com API calls, but these aren't codified in reusable adapter modules.

## Proposed solution

Create two new adapters following the same interface as the WP-CLI adapter:

### `lib/adapters/rest_api_adapter.py`
- Targets self-hosted WordPress sites using the WP REST API (`/wp-json/wp/v2/`)
- Authenticates via Application Passwords (Basic Auth header)
- Handles pagination via `X-WP-TotalPages` / `per_page` / `page` params
- Maps the common interface methods to REST endpoints

### `lib/adapters/wpcom_adapter.py`
- Targets WordPress.com hosted sites and Jetpack-connected self-hosted sites
- Uses the WordPress.com REST API (`https://public-api.wordpress.com/rest/v1.1/`)
- Authenticates via OAuth2 bearer token (from `config.json`)
- Handles pagination via `page_handle` for posts, `page`/`number` for categories
- Accounts for WPCOM-specific response shapes (e.g., categories as a name-keyed hash on posts)

### Shared concerns

- Both adapters should use `requests` or `urllib` for HTTP calls
- Error handling for auth failures (401/403) should surface a clear message and prompt re-authentication
- Rate limiting / retry logic for large exports
- An adapter factory function that reads `config.json` and returns the correct adapter instance

## Acceptance criteria

- [ ] `rest_api_adapter.py` implements the full common interface
- [ ] `wpcom_adapter.py` implements the full common interface
- [ ] Both adapters handle pagination for large sites (1000+ posts)
- [ ] Auth failures produce clear, actionable error messages
- [ ] Unit tests cover core operations for both adapters (mocked HTTP)
- [ ] Export and apply agents can use the adapters instead of inline API logic
- [ ] Existing WP-CLI adapter and workflows are unaffected
