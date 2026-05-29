"""Unity Cloud organization-members deactivation plugin.

Flow:
    Unity Cloud organization members URL → Unity ID login → TOTP → search user
    → row 3-dots → "Remove from organization" → confirm.

Required env:
    UNITY_URL          organization members URL
    UNITY_LOGIN        Unity ID email
    UNITY_PASSWORD     Unity ID password
    UNITY_2FA_SECRET   base32 TOTP secret or otpauth:// URL
"""
from __future__ import annotations

import re
import time

from services._common import (
    find_user_row,
    get_page,
    is_find_only,
    is_on_auth_page,
    session_attr,
    set_session_attr,
    submit_totp,
)

DEFAULT_TIMEOUT = 25_000
SETTLE_TIMEOUT = 4_000
SESSION_KEY = "_unity_signed_in"
APP_HOSTNAME = "cloud.unity.com"


def _on_app(page) -> bool:
    return APP_HOSTNAME in page.url.lower()


def _dismiss_cookie_banner(page) -> None:
    for label in ("Reject All", "Accept All", "Accept Cookies", "Cookie Settings"):
        btn = page.get_by_role("button", name=re.compile(rf"^{label}$", re.I)).first
        if btn.count():
            try:
                btn.click(timeout=2_000)
                time.sleep(0.3)
                return
            except Exception:
                pass
    close = page.locator('button[aria-label*="close" i], button:has-text("×")').first
    if close.count():
        try:
            close.click(timeout=2_000)
        except Exception:
            pass


def _login(page, creds: dict) -> bool:
    page.goto(creds["url"])
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5_000)
    except Exception:
        pass

    # Persistent profile can already be authenticated and land directly on the
    # Unity Cloud admin page.
    if _on_app(page) and not is_on_auth_page(page):
        _dismiss_cookie_banner(page)
        return True

    email_sel = (
        'input[type="email"], input[name="email"], '
        'input[name="userEmail"], input[autocomplete="username"], '
        'input[id*="email" i]'
    )
    try:
        email = page.locator(email_sel).first
        email.wait_for(timeout=30_000)
        email.fill(creds["login"])
    except Exception:
        # Unity may already have redirected us into Cloud via the persistent
        # browser profile while we were waiting for the email field.
        return _on_app(page) and not is_on_auth_page(page)

    next_btn = page.get_by_role(
        "button", name=re.compile(r"^(Next|Continue|Sign\s*in|Log\s*in)$", re.I)
    ).first
    if next_btn.count():
        next_btn.click()
    else:
        email.press("Enter")

    pw_sel = 'input[type="password"], input#password, input[name="password"]'
    try:
        pw = page.locator(pw_sel).first
        pw.wait_for(timeout=30_000)
        pw.fill(creds["password"])
    except Exception:
        return _on_app(page) and not is_on_auth_page(page)

    sign_btn = page.get_by_role(
        "button", name=re.compile(r"^(Sign\s*in|Log\s*in|Continue)$", re.I)
    ).first
    if sign_btn.count():
        sign_btn.click()
    else:
        pw.press("Enter")

    submit_totp(page, "UNITY_2FA_SECRET")

    for label in ("Skip", "Not now", "No thanks", "Maybe later", "Remind me later"):
        b = page.get_by_role("button", name=re.compile(rf"^{label}$", re.I)).first
        if b.count():
            try:
                b.click(timeout=2_000)
                break
            except Exception:
                pass

    try:
        page.wait_for_function(
            f"() => location.hostname.toLowerCase().includes('{APP_HOSTNAME}')",
            timeout=35_000,
        )
    except Exception:
        return _on_app(page) and not is_on_auth_page(page)
    try:
        page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT)
    except Exception:
        pass
    _dismiss_cookie_banner(page)
    return True


def _wait_for_members_page(page) -> bool:
    try:
        page.wait_for_function(
            "() => document.body && document.body.innerText && "
            "document.body.innerText.toLowerCase().includes('member')",
            timeout=8_000,
        )
    except Exception:
        pass
    return _on_app(page)


def _find_target_row(page, target_user: str):
    selectors = (
        f'[role="row"]:has-text("{target_user}")',
        f'tr:has-text("{target_user}")',
        f'[role="listitem"]:has-text("{target_user}")',
        f'li:has-text("{target_user}")',
        f'div[class*="row" i]:has-text("{target_user}")',
        f'div[class*="member" i]:has-text("{target_user}")',
    )
    row = page.locator(", ".join(selectors)).first
    if row.count() == 0:
        return None
    try:
        if target_user.lower() not in row.inner_text().lower():
            return None
    except Exception:
        return None
    return row


def _click_remove_from_org(page, row) -> bool:
    kebab = row.locator(
        'button[aria-haspopup], button[aria-label*="More" i], '
        'button[aria-label*="options" i], button[aria-label*="actions" i], '
        'button:has-text("…"), button:has-text("...")'
    ).first
    if kebab.count() == 0:
        row.hover()
        time.sleep(0.3)
        kebab = row.locator("button").last
    if kebab.count() == 0:
        return False
    kebab.scroll_into_view_if_needed()
    kebab.click()
    time.sleep(0.5)

    remove = page.get_by_role(
        "menuitem", name=re.compile(r"remove\s+from\s+organization", re.I)
    ).first
    if remove.count() == 0:
        remove = page.locator(
            '[role="menuitem"]:has-text("Remove from organization"), '
            'button:has-text("Remove from organization"), '
            'a:has-text("Remove from organization")'
        ).first
    if remove.count() == 0:
        return False
    remove.click()
    return True


def _confirm_remove(page) -> str | None:
    try:
        dialog = page.locator('[role="dialog"], .modal, [class*="modal" i]').first
        dialog.wait_for(timeout=8_000)
    except Exception:
        return "needs-confirmation: no-confirm-dialog"

    try:
        text = dialog.inner_text().lower()
    except Exception:
        text = ""
    if "remove" not in text or "organization" not in text:
        return "needs-confirmation: dialog-text-unexpected"

    confirm = dialog.get_by_role(
        "button", name=re.compile(r"^(remove|remove\s+member|confirm|yes|delete)$", re.I)
    ).first
    if confirm.count() == 0:
        confirm = dialog.locator(
            'button:has-text("Remove"), button:has-text("Confirm"), '
            'button:has-text("Delete"), button:has-text("Yes")'
        ).last
    if confirm.count() == 0:
        return "failed: confirm-button-not-found"
    confirm.click()
    return None


def _deactivate(context, creds: dict, target_user: str, *, page_slug: str) -> str:
    page = get_page(context, page_slug)
    page.set_default_timeout(DEFAULT_TIMEOUT)

    for _ in range(3):
        try:
            page.keyboard.press("Escape")
        except Exception:
            break
        time.sleep(0.2)

    if not session_attr(context, SESSION_KEY):
        if not _login(page, creds):
            return "failed: login"
        set_session_attr(context, SESSION_KEY, True)

    page.goto(creds["url"])
    try:
        page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT)
    except Exception:
        pass
    if not _wait_for_members_page(page):
        return "failed: not-on-unity-cloud"
    _dismiss_cookie_banner(page)

    status = find_user_row(
        page,
        target_user,
        extra_row_selectors=(
            'div[class*="row" i]',
            'div[class*="member" i]',
        ),
    )
    if is_find_only():
        return status
    if status != "found":
        return status

    row = _find_target_row(page, target_user)
    if row is None:
        return "needs-confirmation: row-disappeared-after-find"

    if not _click_remove_from_org(page, row):
        return "failed: remove-from-organization-not-found"

    confirm_err = _confirm_remove(page)
    if confirm_err:
        return confirm_err

    try:
        row.wait_for(state="detached", timeout=10_000)
        return "deactivated"
    except Exception:
        return "needs-confirmation: row-still-present"


def deactivate(context, creds: dict, target_user: str) -> str:
    return _deactivate(context, creds, target_user, page_slug="unity")
