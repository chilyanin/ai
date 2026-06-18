"""Slack (Workspace Redbark2) deactivation — browser/admin-UI based.

Regular (non-Enterprise-Grid) Slack workspaces expose NO API to deactivate a
member (`admin.users.*` and SCIM are Enterprise-Grid-only). So this drives the
admin Members UI:

    /admin (People tab) → filter by email → row "Actions" → Deactivate account
    → confirm dialog (primary_action).

Auth — reuses a logged-in Slack session in the persistent browser profile when
one exists. If the session is missing/expired and LOGIN+PASSWORD are configured,
it logs in automatically via the no-SSO form (email + password + TOTP). You can
still prime a session by hand with:

    ./prime_session.py slack --url https://redbark2.slack.com/admin

Required env:
    SLACK_WORKSPACE_REDBARK2_URL   admin URL (https://redbark2.slack.com/admin)
Optional (enables automated login):
    SLACK_WORKSPACE_REDBARK2_LOGIN       email (e.g. services@fluytstudio.net)
    SLACK_WORKSPACE_REDBARK2_PASSWORD    password
    SLACK_WORKSPACE_REDBARK2_2FA_SECRET  base32 TOTP secret
    SLACK_WORKSPACE_REDBARK2_LOGIN_URL   no-SSO login URL (defaults below)

Statuses:
    found / deactivated / already-deactivated / user-not-found /
    needs-confirmation: … / failed: …
"""
from __future__ import annotations

import os
import re
import time

from services._common import get_page, is_find_only, submit_totp

DEFAULT_TIMEOUT = 20_000
SLUG = "slack_workspace_redbark2"
DEFAULT_URL = "https://redbark2.slack.com/admin"
DEFAULT_LOGIN_URL = "https://redbark2.slack.com/?no_sso=1&redir=%2Fadmin"
SECRET_ENV = "SLACK_WORKSPACE_REDBARK2_2FA_SECRET"

SEARCH = '[data-qa="workspace-members__table-header-search_input"]'
ROW = '[data-qa="workspace-members_table_data_table_row"]'
ACTIONS_BTN = '[data-qa="table_row_actions_button"]'
DEACTIVATE_ITEM = '[data-qa="ws-members-action_deactivate"]'
ACTIVATE_ITEM = '[data-qa="ws-members-action_activate"]'
CONFIRM_BTN = '[data-qa="primary_action"]'


def _dismiss_cookie_banner(page) -> None:
    """Slack's no-SSO page shows a OneTrust cookie banner that can overlay the
    Sign in button. Reject (or accept) it so the click lands."""
    for sel in ("#onetrust-reject-all-handler", "#onetrust-accept-btn-handler"):
        try:
            btn = page.locator(sel)
            if btn.count() and btn.first.is_visible():
                btn.first.click(timeout=2_000)
                time.sleep(0.3)
                return
        except Exception:
            pass


def _login(page, creds: dict) -> bool:
    """Sign in via the no-SSO form: email + password + TOTP. Returns True once
    the admin Members search input is present."""
    login = creds.get("login")
    password = creds.get("password")
    if not (login and password):
        return False

    login_url = os.environ.get("SLACK_WORKSPACE_REDBARK2_LOGIN_URL") or DEFAULT_LOGIN_URL
    page.goto(login_url)
    try:
        page.wait_for_load_state("domcontentloaded")
    except Exception:
        pass
    time.sleep(1.5)
    _dismiss_cookie_banner(page)

    # A still-valid session redirects the login URL straight to /admin — the
    # email form never appears. Treat that as already-logged-in rather than
    # throwing on a missing #email (which would misreport as a login failure).
    if page.locator(SEARCH).count():
        return True

    email = page.locator('#email, [data-qa="login_email"]').first
    try:
        email.wait_for(timeout=8_000)
    except Exception:
        # No email form and no members table — nothing we can drive here.
        return page.locator(SEARCH).count() > 0

    try:
        email.fill(login)
        page.fill('#password, [data-qa="login_password"]', password)
    except Exception:
        return False

    try:
        page.locator('#signin_btn, [data-qa="signin_button"]').first.click()
    except Exception:
        return False
    time.sleep(3)

    # 2FA — Slack uses a six single-character box widget; submit_totp handles it.
    submit_totp(page, SECRET_ENV)

    try:
        page.wait_for_selector(SEARCH, timeout=15_000)
        return True
    except Exception:
        return page.locator(SEARCH).count() > 0


def _looks_logged_out(page) -> bool:
    u = page.url.lower()
    if "signin" in u or "/sign_in" in u:
        return True
    # Slack's signed-out page asks for a workspace/email; the members search
    # input is absent.
    try:
        if page.locator('input[data-qa="signin_domain_input"], input[data-qa="login_email"]').count():
            return True
    except Exception:
        pass
    return False


def _ensure_members_table(page, url: str) -> bool:
    """Land on the Members table. Returns True if the search input is present."""
    for attempt in range(2):
        page.goto(url)
        try:
            page.wait_for_load_state("networkidle", timeout=12_000)
        except Exception:
            pass

        if page.locator(SEARCH).count():
            return True

        # The People tab renders the members table; click it if needed.
        ppl = page.locator('button:has-text("People"), a:has-text("People")').first
        if ppl.count():
            try:
                ppl.click()
                page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass

        try:
            page.wait_for_selector(SEARCH, timeout=8_000)
            return True
        except Exception:
            # Slack's SPA sometimes shows "There's been a glitch" — reload once.
            try:
                body = page.locator("body").inner_text(timeout=2_000).lower()
            except Exception:
                body = ""
            if "glitch" in body and attempt == 0:
                continue
            return page.locator(SEARCH).count() > 0
    return False


def _find_row(page, target_user: str):
    """Search by email and return the matching row locator, or None."""
    search = page.locator(SEARCH).first
    if search.count():
        try:
            search.fill("")
            search.fill(target_user)
            time.sleep(1.5)  # live filter debounce
        except Exception:
            pass
    row = page.locator(f'{ROW}:has-text("{target_user}")').first
    if row.count() == 0:
        return None
    # Safety: the row really must contain the target email.
    try:
        if target_user.lower() not in row.inner_text().lower():
            return None
    except Exception:
        return None
    return row


def deactivate(context, creds: dict, target_user: str) -> str:
    page = get_page(context, SLUG)
    page.set_default_timeout(DEFAULT_TIMEOUT)
    url = creds.get("url") or DEFAULT_URL

    # Tidy any leftover modal/menu from a previous task.
    for _ in range(2):
        try:
            page.keyboard.press("Escape")
        except Exception:
            break
        time.sleep(0.15)

    if not _ensure_members_table(page, url):
        # Session missing/expired. Try automated login if creds are configured.
        if creds.get("login") and creds.get("password"):
            if not _login(page, creds):
                return "failed: login (check SLACK creds / 2FA secret)"
            if not _ensure_members_table(page, url):
                return "failed: members-table-not-rendered-after-login"
        elif _looks_logged_out(page):
            return "failed: not-logged-in (run ./prime_session.py slack)"
        else:
            return "failed: members-table-not-rendered"

    row = _find_row(page, target_user)
    if row is None:
        return "user-not-found"

    row_text = row.inner_text()
    if re.search(r"deactivated", row_text, re.I):
        return "already-deactivated"

    if is_find_only():
        try:
            row.scroll_into_view_if_needed()
            row.evaluate("el => el.style.outline = '3px solid #ff3b30'")
        except Exception:
            pass
        return "found"

    # ---- Open the action menu ----
    # Slack splits the members table into a frozen left column (name + the
    # actions "⋯" button) and a scrollable right column (email). The row we
    # matched by EMAIL lives in the scrollable table, so the actions button is
    # NOT its descendant. After the search filter narrows to exactly one
    # member we target the page-level actions button — and require exactly one
    # so we never act on an ambiguous result.
    actions = page.locator(ACTIONS_BTN)
    n = actions.count()
    if n != 1:
        return f"needs-confirmation: expected-1-filtered-row-got-{n}"
    try:
        actions.first.scroll_into_view_if_needed()
        actions.first.click()
    except Exception as e:
        return f"failed: actions-click ({type(e).__name__})"

    # ---- Click "Deactivate account" ----
    deact = page.locator(DEACTIVATE_ITEM).first
    try:
        deact.wait_for(timeout=4_000)
    except Exception:
        # If only "Activate account" exists, the member is already deactivated.
        if page.locator(ACTIVATE_ITEM).count():
            page.keyboard.press("Escape")
            return "already-deactivated"
        return "failed: deactivate-item-not-found"
    deact.click()

    # ---- Confirm dialog ----
    confirm = page.locator(CONFIRM_BTN).first
    try:
        confirm.wait_for(timeout=5_000)
    except Exception:
        return "needs-confirmation: no-confirm-dialog"
    confirm.click()

    # ---- Verify: re-search and check the row now reads "Deactivated" ----
    time.sleep(1.5)
    try:
        verify = _find_row(page, target_user)
        if verify is not None and re.search(r"deactivated", verify.inner_text(), re.I):
            return "deactivated"
    except Exception:
        pass
    return "needs-confirmation: deactivate-clicked-unverified"
