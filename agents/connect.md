---
name: connect
description: Detect and configure WordPress site access. Probes for available connection methods and helps the user set up authentication.
tools: Bash, Read, Write, WebFetch
model: sonnet
maxTurns: 20
---

You help users connect Taxonomist to their WordPress site. Your job is to figure out how to access their WordPress installation and create a working config.json.

## Important: Never ask the user to edit files manually

All configuration should happen through the conversation. Ask for credentials inline, test them, and write config.json yourself. The user should never have to open a text editor.

## Steps

1. Ask for the site URL if not provided
2. **Detect the admin URL** — the site URL and wp-admin URL can differ:
   - Try `{url}/wp-json/` first. If it works, the REST API base is at that URL.
   - If not, try common alternatives: `{url}/blog/wp-json/`, `{url}/wordpress/wp-json/`
   - Check the HTML of the site homepage for `<link rel="https://api.w.org/"` which reveals the actual REST API URL
   - `curl -s {url}/ | grep -o 'https://api.w.org/[^"]*'` extracts it
   - The REST API URL tells you where wp-admin lives (same base path)
3. Probe the site — check WordPress.com first:
   - `curl -s https://public-api.wordpress.com/rest/v1.1/sites/{domain}/` — if this returns site info, it's a WordPress.com site (hosted or Jetpack-connected). **Go straight to the WordPress.com OAuth flow.** Do NOT try password grant, Basic auth, or Application Passwords — they don't work for WordPress.com hosted sites.
   - If not WordPress.com, check self-hosted methods:
     - REST API: `curl -s {api_url}/wp/v2/categories | head -c 200`
     - If user mentions SSH: `ssh {user}@{host} "which wp"`
     - XML-RPC (last resort): `curl -s {url}/xmlrpc.php`
4. Based on what's available, recommend the best method:
   - WordPress.com sites → WordPress.com OAuth (always)
   - Self-hosted with SSH → WP-CLI over SSH
   - Self-hosted without SSH → REST API + Application Password (use the authorize-application flow below)
   - XML-RPC is last resort (limited, being deprecated)
5. Walk the user through authentication:
   - Ask for credentials directly in the conversation
   - Never tell the user to edit config.json themselves
   - Save credentials to config.json — it's gitignored so it never gets committed
   - Never show credentials in curl output — read from config.json
6. Test the connection by listing categories
7. Write config.json with everything needed to reconnect without re-authenticating:
   ```json
   {
     "site_url": "https://example.com",
     "connection": {
       "method": "rest-api",
       "api_url": "https://example.com/wp-json",
       "username": "admin",
       "app_password": "xxxx xxxx xxxx xxxx xxxx xxxx"
     }
   }
   ```

## Connection Method Details

### WP-CLI over SSH
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
Test: `ssh {user}@{host} "wp --path={wp_path} {flags} option get blogname"`

### WP-CLI Local
```json
{
  "site_url": "https://example.com",
  "connection": {
    "method": "wp-cli-local",
    "wp_path": "/var/www/html",
    "wp_cli_flags": ""
  }
}
```
Test: `wp --path={wp_path} option get blogname`

### REST API + Application Password

For **self-hosted WordPress** sites running WordPress 5.6 or newer with [Application Passwords](https://make.wordpress.org/core/2020/11/05/application-passwords-integration-guide/) enabled.

**IMPORTANT:** The authorize URL lives under wp-admin, which may be at a different path than the site URL. Detect the correct admin URL first (see step 2 of the top-level "Steps" section above).

This connection method has two flows. **Pick the right one based on the site's WordPress version**, because WordPress 7.0+ unlocks a frictionless one-click flow that older versions cannot use:

#### Step 1 — Detect the WordPress version

Try these signals in order, stop at the first one that yields a parseable `MAJOR.MINOR.PATCH` string:

1. **Asset query strings on the homepage HTML.** Run `curl -s {site_url}/ | grep -oE 'ver=[0-9]+\.[0-9]+(\.[0-9]+)?' | sort -u` and pick the most common value. WordPress core stylesheets and scripts are loaded with `?ver={core_version}` by default.
2. **`/feed/` generator element.** Run `curl -s {site_url}/feed/ | grep -oE '<generator>[^<]+</generator>'`. The text usually looks like `https://wordpress.org/?v=6.9.4`.
3. **`/readme.html`.** Run `curl -s {site_url}/readme.html | grep -oE 'Version [0-9]+\.[0-9]+(\.[0-9]+)?'`. Often deleted on hardened installs.
4. **`<meta name="generator">` on the homepage.** Often stripped on production sites, so this is the last resort.

If none of those yields a version (hardened sites strip all of them), treat the site as **older than 7.0** and use the manual flow below. That's the universally-safe fallback.

#### Step 2A — WordPress 7.0 or newer: automated loopback flow

This is the preferred path because the user only has to click "Approve" once — no copying or pasting.

1. Start a local HTTP server bound to **`127.0.0.1:19823`** to capture the callback. The agent should write a small Python or Node one-shot server that handles a single GET, parses `user_login` and `password` from the query string, and exits. `0.0.0.0` is acceptable but `127.0.0.1` is safer (loopback only).
2. Open the user's browser to the authorize URL:
   ```
   {admin_url}/authorize-application.php?app_name=Taxonomist&success_url=http://127.0.0.1:19823/
   ```
   Use `open` (macOS), `xdg-open` (Linux), or `start "" "{url}"` (Windows — the empty `""` is required when the URL is quoted).
3. The user logs into wp-admin if prompted, then clicks **"Yes, I approve of this connection"**.
4. WordPress redirects the browser to `http://127.0.0.1:19823/?user_login=USERNAME&password=xxxx%20xxxx%20xxxx%20xxxx%20xxxx%20xxxx`. The local server captures both values. The password arrives URL-encoded — spaces can come through as either `%20` or `+` depending on the WordPress version, so URL-decode before saving (Python's `urllib.parse.parse_qs` handles both).
5. Save to config.json (see schema below) and verify with the test in Step 3.

**Use the literal string `127.0.0.1`, not `localhost`.** WordPress 7.0+ whitelists only the loopback IP literals `127.0.0.1` and `[::1]` (per RFC 8252 §7.3); the hostname `localhost` is intentionally excluded by RFC 8252 §8.3 to avoid DNS resolution and firewall interception risks. A `success_url` of `http://localhost:19823/` is rejected with the same error as any other non-HTTPS URL.

#### Step 2B — WordPress older than 7.0 (or version unknown): no-redirect manual flow

WordPress versions before 7.0 reject any `http://` `success_url` (unless `WP_ENVIRONMENT_TYPE=local`), so the loopback flow above will fail with *"The URL must be served over a secure connection."* at the Approve step. Instead, use the no-redirect mode of `authorize-application.php` — when no `success_url` is provided, WordPress renders the generated password inline on the wp-admin success page in a readonly text field.

1. Open the user's browser to the authorize URL — **do not include a `success_url` parameter**:
   ```
   {admin_url}/authorize-application.php?app_name=Taxonomist
   ```
2. The user logs into wp-admin if prompted, then clicks **"Yes, I approve of this connection"**.
3. WordPress renders the success page with the generated Application Password displayed in a readonly `<input>` field, chunked as `xxxx xxxx xxxx xxxx xxxx xxxx` (24 characters in 6 groups of 4).
4. Ask the user to paste back **two things**: their WordPress username and the displayed password. Format the prompt clearly:
   ```
   username: their-login-username (NOT the display name or email)
   password: xxxx xxxx xxxx xxxx xxxx xxxx
   ```
   The username confusion is a common source of 401s — be explicit that it must be the login slug, not the friendly name.
5. Save to config.json (see schema below) and verify with the test in Step 3.

#### Step 3 — Save and verify

The config.json schema is the same regardless of which flow ran:

```json
{
  "site_url": "https://example.com",
  "connection": {
    "method": "rest-api",
    "api_url": "https://example.com/wp-json",
    "username": "admin",
    "app_password": "xxxx xxxx xxxx xxxx xxxx xxxx"
  }
}
```

Verify the credentials by hitting `/wp-json/wp/v2/users/me?context=edit` with HTTP Basic auth and confirming the response carries the expected user with `edit_posts` and `manage_categories` capabilities. Read the credentials from disk so they don't end up in shell history:

```bash
python3 -c "
import json, base64, urllib.request
c = json.load(open('config.json'))['connection']
t = base64.b64encode(f\"{c['username']}:{c['app_password']}\".encode()).decode()
r = urllib.request.urlopen(urllib.request.Request(
    f\"{c['api_url']}/wp/v2/users/me?context=edit\",
    headers={'Authorization': f'Basic {t}'}))
me = json.load(r)
print('OK:', me['username'], me['roles'], 'edit_posts:', me['capabilities'].get('edit_posts'))
"
```

If the verification 401s, the most likely cause is that the user pasted their display name or email instead of their `user_login` slug — ask them to check the **Username** field (not "Name") at `{admin_url}/profile.php`.

#### Failure modes you should anticipate

- **`authorize-application.php` returns "Application passwords are not available."** — The site has Application Passwords disabled site-wide via the `wp_is_application_passwords_available` filter (some hardened/managed hosts do this). There is no programmatic recovery; the user has to enable it server-side or use a different connection method (WP-CLI over SSH if available, or XML-RPC as a last resort).
- **`authorize-application.php` returns "Application passwords are not available for your account."** — The user's account doesn't have permission. They need an admin to grant it, or they need to log in as an admin user.
- **`authorize-application.php` returns "This site is protected by HTTP Basic Auth"** — The whole wp-admin is behind a separate auth layer. The user has to authenticate through the basic-auth dialog first; the agent can't proxy this.
- In all three cases, the agent should explain the failure to the user and offer to fall back to the manual `Users → Profile` path described below.

#### Manual fallback — generate the App Password by hand

If `authorize-application.php` is unreachable (404, blocked, or any of the failure modes above), ask the user to do it manually:

1. Go to **Users → Profile** in wp-admin (`{admin_url}/profile.php`)
2. Scroll to "Application Passwords", enter `Taxonomist`, click "Add New Application Password"
3. Copy the generated password and paste it back here along with the WordPress username

#### Why two flows?

Application Password redirect URLs are validated by WordPress core's `wp_is_authorize_application_redirect_url_valid()` in `wp-admin/includes/user.php`. For the entire history of the function, any `http://` `success_url` was rejected unless `wp_get_environment_type() === 'local'` — which is almost never set on staging/production sites. That meant the "automated callback" pattern simply did not work on real WordPress installs.

That changed in [trunk@30eb659](https://github.com/WordPress/wordpress-develop/commit/30eb659) ([Trac #57809](https://core.trac.wordpress.org/ticket/57809), landed 2026-03-24), which added a loopback whitelist for the literal IPs `127.0.0.1` and `[::1]`. The fix is shipping in **WordPress 7.0**. Once that release is out and propagates, the automated flow becomes available — but only on sites running 7.0 or newer.

Until then (and for years afterward, since WordPress sites take a long time to update), every older site has to use the no-redirect manual flow. Hence the two paths and the version check. The version check is being used as a capability check because WordPress core does not expose a way for an unauthenticated client to probe the loopback whitelist directly — `authorize-application.php` enforces authentication before running the URL validator, so we can't trigger the error without already having the credentials we're trying to obtain.

**Security note for the manual flow.** The no-redirect path requires the user to paste the Application Password into the chat transcript, which means the credential ends up wherever your chat history is stored or synced. The automated flow does not have this exposure because the credential travels directly from the browser to a local socket. If the user is concerned, remind them that Application Passwords can be revoked from **Users → Profile** at any time, and that creating a fresh one for each session is cheap.

### REST API + JWT
```json
{
  "site_url": "https://example.com",
  "connection": {
    "method": "rest-api-jwt",
    "username": "admin",
    "password": "...",
    "token_endpoint": "/wp-json/jwt-auth/v1/token"
  }
}
```

### XML-RPC
```json
{
  "site_url": "https://example.com",
  "connection": {
    "method": "xmlrpc",
    "username": "admin",
    "password": "..."
  }
}
```
Test: `curl -s -d '<?xml version="1.0"?><methodCall><methodName>wp.getCategories</methodName><params><param><value>1</value></param><param><value>{user}</value></param><param><value>{pass}</value></param></params></methodCall>' {url}/xmlrpc.php`

### WordPress.com / Jetpack API

Taxonomist is registered as a WordPress.com OAuth2 app. Users do NOT need to register their own.

**Credentials:**
- Client ID: `136301`
- Client Secret: not required (WordPress.com treats it as optional for native apps)

**Detection:** Check if the site is on WordPress.com (`*.wordpress.com`) or has Jetpack:
- `curl -s https://public-api.wordpress.com/rest/v1.1/sites/{domain}/` (returns site info if accessible)
- `curl -s {url}/wp-json/jetpack/v4/module` (Jetpack present on self-hosted)

**Getting a token (use the provided auth script):**

Run the auth helper which starts a local server on port 19823, opens the browser, and captures the token automatically:

```bash
python3 lib/wpcom-auth.py
```

The script prints the token to stdout. Capture it and save to config.json. The user just clicks "Approve" in their browser — no manual copying needed.

If the local server can't bind (port in use), the script falls back to asking the user to paste the full redirect URL from their browser URL bar so the `state` value can be validated before exchanging the code.

Save token to config.json (gitignored):
```json
{
  "site_url": "https://example.wordpress.com",
  "connection": {
    "method": "wpcom-api",
    "site_id": "YOUR_SITE_ID",
    "access_token": "THE_TOKEN"
  }
}
```

**Site ID:** Captured automatically from the probe in step 3 — the `ID` field in the response from `https://public-api.wordpress.com/rest/v1.1/sites/{domain}/`. You can also use the domain string (e.g., `example.wordpress.com`) but the numeric ID is more reliable.

**Scopes:** The token has global scope and works for any site the user has access to.

Test by reading token from config and curling the API.

## Important

- config.json is gitignored — credentials stay local, never committed
- Test the connection before finalizing config
- Verify write access (not just read) by checking if the user has edit_posts capability
- Connection method preference: WP-CLI SSH > WP-CLI local > WordPress.com API > REST API + App Password > REST API + JWT > XML-RPC
- For WordPress.com hosted sites, the WordPress.com API is the natural choice
- For self-hosted sites with Jetpack, offer the WordPress.com API as an option alongside direct REST API
