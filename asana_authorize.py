#!/usr/bin/env python3
"""One-time Asana OAuth authorization — mint a refresh token with write scopes.

Why this exists
---------------
Scopes are bound to the OAuth *grant*, not to the app. Enabling a scope in the
Asana developer hub only widens what the app may REQUEST — an already-issued
ASANA_REFRESH_TOKEN keeps the scope set it was minted with, and refreshing it
returns the same scopes (verified: refresh still 403s on `stories:read`).
So a new scope requires a new consent. This runs that flow and prints the new
refresh token for you to paste into `.env`.

Prerequisites (Asana developer hub, done once)
----------------------------------------------
1. The app must PERMIT every scope requested below (you already enabled
   `stories:read`; `tasks:write` is the other one the workflow needs).
2. Add this exact redirect URI to the app's "Redirect URLs":
       http://localhost:8080/callback
   Override the port with --port; the URI must match character for character.

Usage
-----
    python3 asana_authorize.py
    python3 asana_authorize.py --port 9000
    python3 asana_authorize.py --verify-task 1218048262339289
    ASANA_SCOPES=default python3 asana_authorize.py     # full permissions

Reads ASANA_CLIENT_ID / ASANA_CLIENT_SECRET from .env, opens the consent page,
captures the redirect on localhost, exchanges the code, and — with
--verify-task — immediately checks that reading comments actually works.
"""
from __future__ import annotations

import argparse
import http.server
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser

import requests
from dotenv import load_dotenv

AUTHORIZE_URL = "https://app.asana.com/-/oauth_authorize"
TOKEN_URL = "https://app.asana.com/-/oauth_token"
API_ROOT = "https://app.asana.com/api/1.0"

# Everything the two runners touch:
#   tasks:read        task search, fetch_task
#   tasks:write       complete_task  (currently 403)
#   stories:read      fetch_task_comments — the re-run guard  (currently 403)
#   stories:write     create_task_comment
#   attachments:write upload_task_attachment
#   users:read        me()
#   workspaces:read   workspace resolution
# Set ASANA_SCOPES=default to request full permissions instead (only works if
# the app is configured for that rather than granular scopes).
DEFAULT_SCOPES = (
    "tasks:read tasks:write stories:read stories:write "
    "attachments:read attachments:write users:read workspaces:read"
)


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result: dict = {}
    expected_path = "/callback"

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != _CallbackHandler.expected_path:
            self.send_response(404)
            self.end_headers()
            return
        _CallbackHandler.result = {
            k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()
        }
        ok = "code" in _CallbackHandler.result and "error" not in _CallbackHandler.result
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = (
            "Authorization received. Close this tab and return to the terminal."
            if ok
            else f"Authorization failed: {_CallbackHandler.result}"
        )
        self.wfile.write(f"<html><body><h3>{msg}</h3></body></html>".encode())

    def log_message(self, *args):  # silence default stderr logging
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8080, help="localhost callback port")
    ap.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the consent URL instead of opening a browser",
    )
    ap.add_argument(
        "--verify-task",
        metavar="GID",
        help="After minting, read this task's comments to prove stories:read works",
    )
    args = ap.parse_args()

    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))
    client_id = os.environ.get("ASANA_CLIENT_ID")
    client_secret = os.environ.get("ASANA_CLIENT_SECRET")
    if not (client_id and client_secret):
        print(
            "error: ASANA_CLIENT_ID and ASANA_CLIENT_SECRET must be set in .env",
            file=sys.stderr,
        )
        return 2

    redirect_uri = f"http://localhost:{args.port}/callback"
    scopes = os.environ.get("ASANA_SCOPES") or DEFAULT_SCOPES
    state = secrets.token_urlsafe(16)

    _CallbackHandler.result = {}
    consent_url = f"{AUTHORIZE_URL}?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
            "scope": scopes,
        }
    )

    try:
        server = http.server.HTTPServer(("localhost", args.port), _CallbackHandler)
    except OSError as e:
        print(f"error: cannot listen on {redirect_uri}: {e}", file=sys.stderr)
        print("       (is another process using that port?)", file=sys.stderr)
        return 1
    t = threading.Thread(target=server.handle_request, daemon=True)
    t.start()

    print(f"Requesting scopes:\n  {scopes}\n")
    print(f"Redirect URI (must be registered on the app):\n  {redirect_uri}\n")
    if args.no_browser:
        print("Open this URL to authorize:\n")
        print(consent_url + "\n")
    else:
        print("Opening the consent page in your browser...\n")
        webbrowser.open(consent_url)

    t.join(timeout=300)
    server.server_close()

    res = _CallbackHandler.result
    if not res:
        print("error: timed out waiting for the OAuth redirect (300s)", file=sys.stderr)
        return 1
    if res.get("state") != state:
        print("error: state mismatch — possible CSRF, aborting", file=sys.stderr)
        return 1
    if "error" in res:
        print(f"error: authorization denied: {res}", file=sys.stderr)
        return 1
    code = res.get("code")
    if not code:
        print(f"error: no authorization code in redirect: {res}", file=sys.stderr)
        return 1

    r = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "code": code,
        },
        timeout=30,
    )
    if not r.ok:
        print(
            f"error: token exchange failed: {r.status_code} {r.text[:400]}",
            file=sys.stderr,
        )
        return 1
    data = r.json()
    refresh = data.get("refresh_token")
    access = data.get("access_token")
    if not refresh:
        print(f"error: no refresh_token in response: {data}", file=sys.stderr)
        return 1

    print("\n=== SUCCESS ===")
    print(f"Granted scopes: {data.get('scope', '(not reported)')}")

    if args.verify_task and access:
        print(f"\nVerifying stories:read on task {args.verify_task} ...")
        vr = requests.get(
            f"{API_ROOT}/tasks/{args.verify_task}/stories",
            params={"opt_fields": "type,text,created_at"},
            headers={"Authorization": f"Bearer {access}"},
            timeout=30,
        )
        if vr.ok:
            stories = vr.json().get("data") or []
            comments = [s for s in stories if s.get("type") == "comment"]
            print(f"  OK — {len(comments)} comment(s) readable. The guard will work.")
        else:
            print(f"  STILL FAILING: {vr.status_code} {vr.text[:250]}")
            print("  Check that the app permits every scope listed above.")

    print("\nReplace this line in your .env (keep the value out of chat):\n")
    print("ASANA_REFRESH_TOKEN=<the value printed below>\n")
    print(refresh)
    print("\nThen re-run: python3 grant.py --date <YYYY-MM-DD> --dry-run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
