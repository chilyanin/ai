#!/usr/bin/env python3
"""Throwaway validation harness for the Skills Base find-only flow.

Loads .env, opens the per-service persistent profile, and calls the Skills Base
handler in DEACTIVATE_FIND_ONLY mode. Touches NO Asana and performs NO
destructive action — it should land on Directories → People, outline the target
row, and stop. Prints the status and saves a screenshot.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from secrets_provider import load_secrets

load_secrets()
os.environ["DEACTIVATE_FIND_ONLY"] = "1"

from services._common import service_profile_dir  # noqa: E402
from services import svc_skills_base_programmers as svc  # noqa: E402

TARGET = sys.argv[1] if len(sys.argv) > 1 else "artem.kolosov@playrix.com"
HEADLESS = "--headless" in sys.argv

creds = {
    "url": os.environ["SKILLS_BASE_PROGRAMMERS_URL"],
    "login": os.environ["SKILLS_BASE_PROGRAMMERS_LOGIN"],
    "password": os.environ["SKILLS_BASE_PROGRAMMERS_PASSWORD"],
}

from playwright.sync_api import sync_playwright  # noqa: E402

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=service_profile_dir("skills_base_programmers"),
        headless=HEADLESS,
    )
    try:
        status = svc.deactivate(ctx, creds, TARGET)
        print(f"\n=== find-only status for {TARGET}: {status} ===")
        page = getattr(ctx, "_page_skills_base_programmers", None)
        if page and not page.is_closed():
            out = Path("screenshots/manual/skills_base_validate.png")
            out.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(out), full_page=True)
            print(f"final url: {page.url}")
            print(f"screenshot: {out}")
    finally:
        ctx.close()
