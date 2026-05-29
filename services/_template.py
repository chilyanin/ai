"""Template for a service-specific deactivation plugin.

To add support for a service named e.g. "Jira":
  1. Copy this file to `services/svc_jira.py` (slug = lowercased service
     name from the Asana task title, non-alphanumerics → `_`, prefixed with
     `svc_`).
  2. Fill in `deactivate()` below with the real flow.
  3. Make sure SERVICE_JIRA_URL / LOGIN / PASSWORD are set in .env.

Return one of: "deactivated", "user-not-found", "skipped", "needs-confirmation",
or "failed: <reason>".
"""
from __future__ import annotations


def deactivate(context, creds: dict, target_user: str) -> str:
    page = context.new_page()
    page.goto(creds["url"])

    # Example shape — adapt to the real admin panel:
    #
    # page.fill('input[name="username"]', creds["login"])
    # page.fill('input[name="password"]', creds["password"])
    # page.click('button[type="submit"]')
    # page.wait_for_url("**/dashboard")
    #
    # page.goto(creds["url"].rstrip("/") + "/admin/users")
    # page.fill('input[type="search"]', target_user)
    # row = page.locator(f'tr:has-text("{target_user}")').first
    # if row.count() == 0:
    #     return "user-not-found"
    # row.locator('button:has-text("Deactivate")').click()
    # page.click('button:has-text("Confirm")')

    return "failed: template not implemented"
