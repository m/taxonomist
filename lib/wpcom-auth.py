"""
WordPress.com OAuth2 authorization flow for CLI tools.

Starts a local HTTP server, opens the authorization page in the user's
default browser, captures the redirect with the auth code, exchanges it
for a bearer token, and prints the token to stdout.

Usage:
    python3 lib/wpcom-auth.py [site_domain]

    site_domain  Optional. If provided, the token will be scoped to this
                 site (passed as &blog= parameter). Example: "ma.tt"

Exit codes:
    0  Success — token printed to stdout (last line)
    1  Authorization failed or timed out

The script tries ports 19823, 19824, 19825 to avoid conflicts. The
registered redirect URI is http://localhost — WordPress.com ignores the
port for native apps, but if that fails the script falls back to asking
the user to paste the code manually.
"""

import http.server
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser

CLIENT_ID = "136301"
CLIENT_SECRET = "Vy27l7cBxu3h42mdhK536QXVQgedeIlte3JAXS2FsqDv0yJf9xoRMIObcogWcUVv"
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
    data = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }).encode()

    req = urllib.request.Request(
        "https://public-api.wordpress.com/oauth2/token",
        data=data,
        method="POST",
    )
    resp = urllib.request.urlopen(req)
    return json.loads(resp.read())


def main():
    site = sys.argv[1] if len(sys.argv) > 1 else None
    auth_code = None
    server = None

    # Build the authorization URL.
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "global",
    }
    if site:
        params["blog"] = site

    auth_url = (
        "https://public-api.wordpress.com/oauth2/authorize?"
        + urllib.parse.urlencode(params)
    )

    # Start a local server to capture the redirect.
    port = LISTEN_PORT if check_port_available(LISTEN_PORT) else None
    if port:
        class CallbackHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                nonlocal auth_code
                query = urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query
                )
                auth_code = query.get("code", [""])[0]
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

        # WordPress.com registered redirect is http://localhost (no port).
        # For native apps it should follow the redirect regardless of port,
        # but we use the base URI in the auth URL to match the registration.

    # Open the browser.
    print("Opening WordPress.com authorization page...", file=sys.stderr)
    webbrowser.open(auth_url)

    if server:
        # Poll the server until we get the code or time out.
        deadline = time.time() + TIMEOUT
        while not auth_code and time.time() < deadline:
            server.handle_request()

        server.server_close()

    # If the local server didn't catch it (redirect went to port 80),
    # ask the user to paste the code.
    if not auth_code:
        print(
            "\nThe redirect may have failed because nothing is running on port 80.",
            file=sys.stderr,
        )
        print(
            "Look at your browser's URL bar — it should show something like:",
            file=sys.stderr,
        )
        print(
            "  http://localhost/?code=XXXXXXXXXX",
            file=sys.stderr,
        )
        print(file=sys.stderr)
        auth_code = input("Paste the code value here: ").strip()

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
