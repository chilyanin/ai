#!/usr/bin/env python3
"""Offline end-to-end test of the uicheck machinery (no network, no Vault).

    python3 -m uicheck.selftest            # heuristic repairer
    python3 -m uicheck.selftest --llm --env-file .env_test    # real LLM chooser

Scenario, against the fixture pages in uicheck/testdata/:
  1. admin_v1.html  - every selector matches -> all steps ok.
  2. admin_v2.html  - the service "redesigned": data-qa hooks are gone, rows
                      are divs -> every step fails, the repairer must find the
                      new element, Playwright verifies it, an override is saved.
  3. admin_v1.html  - the overrides no longer match but the code defaults do
                      -> overrides are retired automatically.

Writes to a temporary overrides file (UICHECK_OVERRIDES) so the real
uicheck/overrides.json is never touched.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="uicheck-selftest-"))
os.environ["UICHECK_OVERRIDES"] = str(_TMP / "overrides.json")

from services import _selectors as S  # noqa: E402  (after env is set)
from uicheck.fingerprint import Store  # noqa: E402
from uicheck.model import ProbeAbort, ProbeContext  # noqa: E402

SLUG = "selftest"
CANARY = "admin@example.test"


def _register() -> None:
    S.sel(SLUG, "search", '[data-qa="members-search"]')
    S.sel(SLUG, "row", 'tr[data-qa="member-row"]')
    S.sel(SLUG, "kebab", 'button[data-qa="row-actions"]')
    S.sel(SLUG, "remove_item", '[role="menuitem"][data-qa="action-remove"]')
    S.sel(SLUG, "invite_button", 'button[data-qa="invite"]')
    S.sel(SLUG, "dialog", '[role="dialog"]:visible')
    S.sel(SLUG, "invite_email", 'input[data-qa="invite-email"]')
    S.sel(SLUG, "send_button", 'button[data-qa="send"]')


def probe(ctx: ProbeContext, url: str) -> None:
    page = ctx.page
    page.goto(url)
    ctx.state("loaded", "Members" in page.title(), "members page loaded")
    search = ctx.check("search", "the search/filter input above the members list", hints=("search", "filter", "members"))
    search.fill("")
    search.fill(CANARY)
    time.sleep(0.3)
    row = ctx.check("row", "a members-list row container (one element per person) that contains the member's email", must_contain=CANARY, hints=("row", "member"))
    kebab = ctx.check("kebab", "the row's more-actions (⋯) menu button", scope=row, hints=("more", "actions"))
    kebab.click()
    time.sleep(0.3)
    ctx.check("remove_item", "the 'Remove' item in the opened row actions menu (presence only)", hints=("remove",),
              alt=lambda: page.get_by_role("menuitem", name=re.compile(r"^Remove$", re.I)))
    ctx.escape(1)
    search.fill("")
    invite = ctx.check("invite_button", "the button that opens the invite dialog", hints=("invite",))
    invite.click()
    dialog = ctx.check("dialog", "the open invite dialog", hints=("dialog", "invite"))
    ctx.check("invite_email", "the email input inside the invite dialog", scope=dialog, hints=("email",))
    ctx.check("send_button", "the 'Send invite' button inside the invite dialog (presence only)", scope=dialog, hints=("send", "invite"))
    ctx.escape(1)


def run_round(label: str, page, url: str, repairer, store: Store) -> list:
    print(f"\n--- {label}: {Path(url).name if url.startswith('file:') else url}")
    ctx = ProbeContext(slug=SLUG, service="Selftest", page=page, context=page.context, creds={}, canary=CANARY,
                       repairer=repairer, fp_store=store, shot_dir=_TMP / "shots")
    try:
        probe(ctx, url)
    except ProbeAbort as e:
        print(f"    -- aborted: {e}")
    return ctx.results


def main() -> int:
    use_llm = "--llm" in sys.argv
    if "--env-file" in sys.argv:
        from dotenv import load_dotenv

        load_dotenv(sys.argv[sys.argv.index("--env-file") + 1], override=False)
    from playwright.sync_api import sync_playwright
    from uicheck import repair as R

    _register()
    repairer = R.build_repairer(None if use_llm else "heuristic")
    print(f"overrides file: {S.OVERRIDES_PATH}")
    print(f"repairer: {repairer.name}")
    store = Store(_TMP / "fp")
    v1 = (HERE / "testdata" / "admin_v1.html").resolve().as_uri()
    v2 = (HERE / "testdata" / "admin_v2.html").resolve().as_uri()

    failures: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_default_timeout(3_000)
        try:
            r1 = run_round("round 1 (v1, baseline)", page, v1, repairer, store)
            bad = [r for r in r1 if r.status not in ("ok",)]
            if bad:
                failures.append("round 1: " + ", ".join(f"{r.step}={r.status}" for r in bad))

            r2 = run_round("round 2 (v2, redesign -> repair)", page, v2, repairer, store)
            expect_repaired = {"search", "row", "kebab", "remove_item", "invite_button", "invite_email", "send_button"}
            got = {r.step: r.status for r in r2}
            for step in expect_repaired:
                if got.get(step) != "repaired":
                    failures.append(f"round 2: {step} expected repaired, got {got.get(step)}")
            if got.get("dialog") != "ok":
                failures.append(f"round 2: dialog expected ok (selector unchanged), got {got.get('dialog')}")
            data = json.loads(S.OVERRIDES_PATH.read_text(encoding="utf-8"))
            for step in expect_repaired:
                e = data.get(SLUG, {}).get(step, {})
                if e.get("source", "code") == "code":
                    failures.append(f"round 2: no override persisted for {step}")
            print("    overrides now:")
            for k, e in sorted(data.get(SLUG, {}).items()):
                print(f"      {k:<14} {e.get('selector')!r}  ({e.get('source')})")

            r3 = run_round("round 3 (v1 again -> overrides retired)", page, v1, repairer, store)
            got3 = {r.step: r.status for r in r3}
            for step in expect_repaired:
                if got3.get(step) != "repaired":
                    failures.append(f"round 3: {step} expected repaired(retired), got {got3.get(step)}")
            data = json.loads(S.OVERRIDES_PATH.read_text(encoding="utf-8"))
            for step in expect_repaired:
                e = data.get(SLUG, {}).get(step, {})
                if e.get("source") != "code" or e.get("selector") != S.default_for(SLUG, step):
                    failures.append(f"round 3: {step} not back on code default: {e.get('selector')!r}")

            r4 = run_round("round 4 (v1, all green again)", page, v1, repairer, store)
            bad = [r for r in r4 if r.status != "ok"]
            if bad:
                failures.append("round 4: " + ", ".join(f"{r.step}={r.status}" for r in bad))
            diffs = [r for r in r4 if r.diff]
            print(f"    fingerprint diffs on round 4: {len(diffs)} (expected 0 - same page as round 3)")
            if diffs:
                failures.append("round 4: unexpected fingerprint diff")
        finally:
            browser.close()

    print()
    if failures:
        print("SELFTEST FAILED")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"SELFTEST OK  (temp dir {_TMP})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
