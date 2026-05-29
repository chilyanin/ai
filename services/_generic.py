"""Fallback handler: opens the service in a browser and waits for manual action.

If stdin is not a TTY (e.g. the script is driven by another process), we
skip the manual prompt, just capture the landing page, and return a status
indicating no automated flow exists for this service.
"""
from __future__ import annotations

import sys


def deactivate(context, creds: dict, target_user: str) -> str:
    page = context.new_page()
    page.goto(creds["url"])
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass

    print(f"    login: {creds['login']}  (password is in .env)")
    if creds.get("notes"):
        print(f"    notes: {creds['notes']}")

    if not sys.stdin.isatty():
        print(
            "    non-interactive run: no plugin for this service, captured landing"
            f" page only. Write services/svc_<slug>.py to automate '{target_user}'."
        )
        # Page intentionally left open so the caller can screenshot it.
        return "failed: no-plugin"

    print(f"    >> deactivate '{target_user}' in the browser window <<")
    ans = input("    press Enter when done, 's' to skip, 'f' if failed: ").strip().lower()
    # Intentionally not closing the page — the caller screenshots it first.
    if ans == "s":
        return "skipped"
    if ans == "f":
        return "failed: manual"
    return "deactivated (manual)"
