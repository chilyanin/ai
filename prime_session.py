#!/usr/bin/env python3
"""Open a service's admin URL in the persistent Playwright profile and wait
for the operator to sign in by hand. Once authenticated, cookies persist in
.browser_profile/ and subsequent grant.py / deactivate.py runs can reuse the
session without typing credentials.

Use this whenever a session-auth service (SERVICE_<KEY>_AUTH=session in .env)
needs a one-time login, or when its cookies have expired.

Usage:
    ./prime_session.py chatgpt
    ./prime_session.py chatgpt --url https://chatgpt.com/admin/members
    ./prime_session.py chatgpt --wait 1800   # wait up to 30 min
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("service", help="Service slug used in .env (e.g. 'chatgpt')")
    ap.add_argument("--url", help="Override URL (defaults to SERVICE_<KEY>_URL)")
    ap.add_argument("--wait", type=int, default=900,
                    help="Seconds to keep the window open after page-load (default: 900 = 15 min)")
    args = ap.parse_args()

    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

    key = args.service.upper().replace("-", "_")
    url = args.url or os.environ.get(f"SERVICE_{key}_URL") or os.environ.get(f"{key}_URL")
    if not url:
        print(
            f"error: no URL provided and SERVICE_{key}_URL is not set in .env",
            file=sys.stderr,
        )
        return 2

    from playwright.sync_api import sync_playwright

    user_data_dir = Path(".browser_profile").resolve()
    user_data_dir.mkdir(parents=True, exist_ok=True)

    print(f"opening {url} (visible window) — sign in manually if needed.")
    print(f"window will stay open for up to {args.wait}s; press Enter here to close early.")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=False,
        )
        try:
            page = context.new_page()
            page.goto(url)
            # Race a stdin read against the wait timer; whichever happens first
            # closes the browser.
            try:
                import select
                rlist, _, _ = select.select([sys.stdin], [], [], args.wait)
                if rlist:
                    sys.stdin.readline()
            except Exception:
                # Fallback: just sleep.
                import time as _t
                _t.sleep(args.wait)
            print("closing browser; cookies saved to .browser_profile/")
        finally:
            context.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
