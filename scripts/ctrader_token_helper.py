#!/usr/bin/env python3
"""One-shot OAuth token exchange for cTrader Open API.

Reads CTRADER_CLIENT_ID + CTRADER_CLIENT_SECRET from env/.env, opens the
browser to Spotware's authorization page, runs a local HTTP server on
:8080 to catch the redirect, exchanges the auth code for an access token,
and writes CTRADER_ACCESS_TOKEN + CTRADER_REFRESH_TOKEN back to .env.

Run this once after app approval. Refresh tokens last ~30 days.
"""

from __future__ import annotations

import http.server
import os
import socketserver
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: requests is required. Run `pip install requests`.")
    sys.exit(1)

try:
    from ctrader_open_api import EndPoints
    AUTH_URL = EndPoints.AUTH_URI
    TOKEN_URL = EndPoints.TOKEN_URI
except ImportError:
    AUTH_URL = "https://openapi.ctrader.com/apps/auth"
    TOKEN_URL = "https://openapi.ctrader.com/apps/token"

REDIRECT_HOST = "127.0.0.1"
REDIRECT_PORT = 8080
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
SCOPE = "accounts trading"


_received_code: dict[str, str] = {}


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            _received_code["code"] = params["code"][0]
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h1>Authorization received.</h1>"
                b"<p>You can close this window and return to the terminal.</p></body></html>"
            )
        elif "error" in params:
            _received_code["error"] = params["error"][0]
            self.send_response(400)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(
                f"<html><body><h1>Error: {params['error'][0]}</h1></body></html>".encode()
            )
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args) -> None:
        return  # quiet


def _load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def _write_env_tokens(path: Path, access_token: str, refresh_token: str) -> None:
    lines: list[str] = []
    seen_access = False
    seen_refresh = False
    if path.exists():
        for raw in path.read_text().splitlines():
            if raw.startswith("CTRADER_ACCESS_TOKEN="):
                lines.append(f"CTRADER_ACCESS_TOKEN={access_token}")
                seen_access = True
            elif raw.startswith("CTRADER_REFRESH_TOKEN="):
                lines.append(f"CTRADER_REFRESH_TOKEN={refresh_token}")
                seen_refresh = True
            else:
                lines.append(raw)
    if not seen_access:
        lines.append(f"CTRADER_ACCESS_TOKEN={access_token}")
    if not seen_refresh:
        lines.append(f"CTRADER_REFRESH_TOKEN={refresh_token}")
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    env_path = Path(".env")
    env_values = _load_env_file(env_path)

    client_id = os.getenv("CTRADER_CLIENT_ID") or env_values.get("CTRADER_CLIENT_ID", "")
    client_secret = os.getenv("CTRADER_CLIENT_SECRET") or env_values.get("CTRADER_CLIENT_SECRET", "")

    if not client_id or not client_secret:
        print("ERROR: CTRADER_CLIENT_ID and CTRADER_CLIENT_SECRET must be set.")
        print("Create a .env file from .env.example and fill in the credentials")
        print("from the openapi.ctrader.com application page.")
        return 1

    auth_params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
    }
    auth_full_url = f"{AUTH_URL}?{urllib.parse.urlencode(auth_params)}"

    print(f"Starting local callback server on {REDIRECT_URI}")
    print(f"Opening browser to Spotware authorization page...")
    print()

    httpd = socketserver.TCPServer((REDIRECT_HOST, REDIRECT_PORT), _CallbackHandler)
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    webbrowser.open(auth_full_url)

    print("Waiting for you to authorize the app in the browser...")
    print("(If your browser didn't open, paste this URL manually:)")
    print(f"  {auth_full_url}")
    print()

    # Wait up to 5 minutes for the callback
    import time
    start = time.time()
    while not _received_code and (time.time() - start) < 300:
        time.sleep(0.5)

    httpd.shutdown()
    httpd.server_close()

    if "error" in _received_code:
        print(f"ERROR: Spotware returned an error: {_received_code['error']}")
        return 1
    if "code" not in _received_code:
        print("ERROR: Timed out waiting for authorization callback (5 minutes).")
        return 1

    auth_code = _received_code["code"]
    print(f"Got authorization code. Exchanging for tokens...")

    token_params = {
        "grant_type": "authorization_code",
        "code": auth_code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    response = requests.post(TOKEN_URL, data=token_params, timeout=30)
    if response.status_code != 200:
        print(f"ERROR: token exchange failed: {response.status_code}")
        print(response.text)
        return 1

    data = response.json()
    access_token = data.get("accessToken") or data.get("access_token", "")
    refresh_token = data.get("refreshToken") or data.get("refresh_token", "")

    if not access_token:
        print(f"ERROR: token response missing access token: {data}")
        return 1

    _write_env_tokens(env_path, access_token, refresh_token)
    print(f"✓ Tokens written to {env_path.absolute()}")
    print(f"  access_token:  {access_token[:16]}...{access_token[-8:]}")
    if refresh_token:
        print(f"  refresh_token: {refresh_token[:16]}...{refresh_token[-8:]}")
    print()
    print("Next step: run the trading account linkage (scripts/ctrader_account_setup.py)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
