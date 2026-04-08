"""
WordPress.com OAuth2 authorization flow for CLI tools.

Starts a local HTTP server on port 19823, opens the WordPress.com
authorization page in the user's default browser, captures the
redirect with the auth code, exchanges it for a bearer token, and
prints the token to stdout.

Usage:
    python3 lib/wpcom-auth.py

Exit codes:
    0  Success — token printed to stdout (last line)
    1  Authorization failed or timed out

The token has global scope and works for any site the user has access
to. If port 19823 is unavailable, the script falls back to asking the
user to paste the code from their browser's URL bar.
"""

import http.server
import json
import os
import secrets
import sys
import time
import urllib.parse
import urllib.request
import webbrowser

CLIENT_ID = "136301"
# Client secret is optional for native apps on WordPress.com.
# If set via env var, it will be included in the token exchange.
# If not set, the exchange still works — WP.com treats it as optional.
CLIENT_SECRET = os.environ.get("WPCOM_CLIENT_SECRET")
LISTEN_PORT = 19823
REDIRECT_URI = f"http://localhost:{LISTEN_PORT}"
TIMEOUT = 120  # seconds to wait for user to authorize


def check_port_available(port):
    """Check if a port is available for binding."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("localhost", port))
        s.close()
        return True
    except OSError:
        return False


def exchange_code(code):
    """Exchange an authorization code for a bearer token."""
    params = {
        "client_id": CLIENT_ID,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
    # Only include client_secret if set — sending empty string causes 400.
    if CLIENT_SECRET:
        params["client_secret"] = CLIENT_SECRET
    data = urllib.parse.urlencode(params).encode()

    req = urllib.request.Request(
        "https://public-api.wordpress.com/oauth2/token",
        data=data,
        method="POST",
    )
    resp = urllib.request.urlopen(req)
    return json.loads(resp.read())


def main():
    auth_code = None
    auth_error = None
    server = None
    oauth_state = secrets.token_urlsafe(32)

    # Build the authorization URL.
    # Note: do NOT include a "blog" parameter here — that triggers
    # Jetpack's separate auth flow for self-hosted sites, which
    # redirects to the site's wp-login.php instead of back to us.
    # The token we get works for any site the user has access to.
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "global",
        "state": oauth_state,
    }

    auth_url = (
        "https://public-api.wordpress.com/oauth2/authorize?"
        + urllib.parse.urlencode(params)
    )

    # Start a local server to capture the redirect.
    port = LISTEN_PORT if check_port_available(LISTEN_PORT) else None
    if port:
        class CallbackHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                nonlocal auth_code, auth_error
                query = urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query
                )
                returned_state = query.get("state", [""])[0]
                if returned_state != oauth_state:
                    auth_error = "State mismatch in OAuth callback."
                    self.send_response(400)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(
                        b"<html><body><h2>Authorization failed</h2>"
                        b"<p>The OAuth state did not match. "
                        b"Return to the terminal and try again.</p>"
                        b"</body></html>"
                    )
                    return

                auth_code = query.get("code", [""])[0]
                if not auth_code:
                    auth_error = "No authorization code received in OAuth callback."
                    self.send_response(400)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(
                        b"<html><body><h2>Authorization failed</h2>"
                        b"<p>The callback did not include an authorization code.</p>"
                        b"</body></html>"
                    )
                    return

                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h2>Authorized!</h2>"
                    b"<p>You can close this tab and return to your terminal.</p>"
                    b"</body></html>"
                )

            def log_message(self, *args):
                pass  # Suppress request logging.

        server = http.server.HTTPServer(("localhost", port), CallbackHandler)
        server.timeout = 2
        print(f"Listening on localhost:{port} for OAuth callback...", file=sys.stderr)

    # Open the browser.
    print("Opening WordPress.com authorization page...", file=sys.stderr)
    webbrowser.open(auth_url)

    if server:
        # Poll the server until we get the code or time out.
        deadline = time.time() + TIMEOUT
        while not auth_code and time.time() < deadline:
            server.handle_request()

        server.server_close()

    # If the local server rejected the callback, stop before offering
    # the manual fallback. A mismatched state means the flow is not safe to
    # continue.
    if auth_error:
        print(auth_error, file=sys.stderr)
        sys.exit(1)

    # If the local server didn't catch the redirect, ask the user to
    # paste the full redirect URL from their browser's URL bar so we can
    # validate the state before exchanging the code.
    if not auth_code:
        print(
            "\nThe redirect didn't reach the local server.",
            file=sys.stderr,
        )
        print(
            "Check your browser's URL bar — it should show something like:",
            file=sys.stderr,
        )
        print(
            f"  http://localhost:{LISTEN_PORT}/?code=XXXXXXXXXX&state=YYYYYYYYYY",
            file=sys.stderr,
        )
        print(file=sys.stderr)
        redirect_url = input("Paste the full redirect URL here: ").strip()
        query = urllib.parse.parse_qs(urllib.parse.urlparse(redirect_url).query)
        auth_code = query.get("code", [""])[0]
        returned_state = query.get("state", [""])[0]
        if returned_state != oauth_state:
            print("State mismatch in pasted redirect URL.", file=sys.stderr)
            sys.exit(1)

    if not auth_code:
        print("No authorization code received.", file=sys.stderr)
        sys.exit(1)

    # Exchange the code for a token.
    print("Exchanging code for token...", file=sys.stderr)
    try:
        result = exchange_code(auth_code)
    except Exception as e:
        print(f"Token exchange failed: {e}", file=sys.stderr)
        sys.exit(1)

    if "access_token" not in result:
        print(f"Error: {json.dumps(result)}", file=sys.stderr)
        sys.exit(1)

    token = result["access_token"]
    blog_id = result.get("blog_id", "")
    blog_url = result.get("blog_url", "")

    print(f"Authorized! Blog ID: {blog_id}, URL: {blog_url}", file=sys.stderr)

    # Print token to stdout so callers can capture it.
    print(token)


if __name__ == "__main__":
    main()
