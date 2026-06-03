#!/usr/bin/env python3
"""Read-only DOM discovery for Skills Base nav + People list. No writes."""
from __future__ import annotations

import os
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from services._common import service_profile_dir  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

URL = os.environ["SKILLS_BASE_PROGRAMMERS_URL"]

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=service_profile_dir("skills_base_programmers"),
        headless=False,
    )
    page = ctx.new_page()
    page.set_default_timeout(20_000)
    page.goto(URL, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass
    # Wait for the SPA nav to render (the word "People" in the sidebar).
    try:
        page.wait_for_function(
            "() => /people/i.test(document.body.innerText) && document.querySelectorAll('a,button').length > 10",
            timeout=30_000,
        )
    except Exception:
        pass
    time.sleep(2)
    print("landing url:", page.url)

    # 1) Dump ALL clickable nav-ish elements: text + href.
    links = page.evaluate(
        """() => Array.from(document.querySelectorAll('a, button, [role=link], [role=menuitem], [role=treeitem]'))
            .map(a => ({text: (a.innerText||a.getAttribute('aria-label')||'').trim().replace(/\\s+/g,' '),
                        href: a.getAttribute('href'), tag: a.tagName.toLowerCase()}))
            .filter(x => x.text && x.text.length < 30)"""
    )
    print("\n=== sidebar links ===")
    seen = set()
    for l in links:
        key = (l["text"], l["href"])
        if key in seen:
            continue
        seen.add(key)
        print(f"  [{l['tag']:6}] {l['text'][:28]:28}  -> {l['href']}")

    # 2) Click the People link specifically.
    people = page.get_by_role("link", name=__import__("re").compile(r"^people$", __import__("re").I)).first
    print("\nPeople link count:", people.count())
    if people.count():
        try:
            print("People href:", people.get_attribute("href"))
        except Exception as e:
            print("href err:", e)
        people.click()
        time.sleep(3)
        print("after People click url:", page.url)
        # Is there a search box + table/rows?
        info = page.evaluate(
            """() => ({
                hasSearch: !!document.querySelector('input[type=search], input[placeholder*="earch" i]'),
                rowCount: document.querySelectorAll('[role=row], tbody tr').length,
                hasEmail: /[\\w.+-]+@[\\w-]+\\.[\\w.-]+/.test(document.body.innerText),
                heading: (document.querySelector('h1,h2,[role=heading]')||{}).innerText || ''
            })"""
        )
        print("people-list info:", info)
        out = Path("screenshots/manual/skills_base_people_list.png")
        page.screenshot(path=str(out), full_page=True)
        print("screenshot:", out)

        # 3) Try to reveal a per-row action menu on the first data row.
        print("\n=== probing first row action controls ===")
        row_html = page.evaluate(
            """() => {
                const rows = document.querySelectorAll('[role=row], tbody tr');
                for (const r of rows) {
                    if (/[\\w.+-]+@[\\w-]+\\.[\\w.-]+/.test(r.innerText)) {
                        const btns = Array.from(r.querySelectorAll('button, a[role=button]'))
                            .map(b => ({label: (b.getAttribute('aria-label')||b.innerText||'').trim().slice(0,30),
                                        haspopup: b.getAttribute('aria-haspopup')}));
                        return {text: r.innerText.replace(/\\s+/g,' ').slice(0,120), buttons: btns};
                    }
                }
                return null;
            }"""
        )
        print("first data row:", row_html)

    ctx.close()
