"""Maxon (my.maxon.net) deactivation plugin.

Auth: email + password. No TOTP secret is configured for Maxon, so 2FA is
skipped — if Maxon enables it on this account, login will halt and the
operator will see the 2FA challenge in the screenshot.

Status:
  This plugin gets to a logged-in state and (when --find-only) stops there
  with a screenshot. The exact flow to locate and deactivate a specific
  user inside the Maxon team admin still needs to be filled in once the
  operator confirms the post-login UI path. Look for the
  ``# TODO(maxon-flow):`` markers below.

Required env:
    MAXON_URL        login URL (e.g. https://my.maxon.net/)
    MAXON_LOGIN      admin email
    MAXON_PASSWORD   admin password
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
)

DEFAULT_TIMEOUT = 25_000
SETTLE_TIMEOUT = 4_000
SESSION_KEY = "_maxon_signed_in"
APP_HOSTNAME = "my.maxon.net"


def _on_app(page) -> bool:
    return APP_HOSTNAME in page.url.lower()


def _click_button_named(page, *labels) -> bool:
    for label in labels:
        btn = page.get_by_role("button", name=re.compile(rf"^{label}$", re.I)).first
        if btn.count():
            try:
                btn.click()
                return True
            except Exception:
                continue
    return False


def _dismiss_cookie_banner(page) -> None:
    """Maxon shows a Cookiebot banner that sometimes intercepts clicks."""
    for label in ("Use necessary cookies only", "Allow all", "Accept all", "Reject all"):
        btn = page.get_by_role("button", name=re.compile(rf"^{label}$", re.I)).first
        if btn.count():
            try:
                btn.click(timeout=2_000)
                return
            except Exception:
                pass


def _login(page, creds: dict) -> bool:
    page.goto(creds["url"])
    _dismiss_cookie_banner(page)

    # If persistent cookies already authenticated us, the email field won't
    # be there — Maxon redirects straight to /teams/.
    if not is_on_auth_page(page) and "my.maxon.net" in page.url.lower():
        return True

    # Email field — Maxon uses Auth0/Okta-style forms; the input may be
    # labelled username, email, or be the first text input on the page.
    email_input = page.locator(
        'input[type="email"], input[name="email"], input[name="username"], input[autocomplete="username"]'
    ).first
    try:
        email_input.wait_for(timeout=15_000)
    except Exception:
        return not is_on_auth_page(page)
    email_input.fill(creds["login"])

    # Some forms put email and password on the same screen; others stage it.
    pw_input = page.locator('input[type="password"], input[name="password"]').first
    if pw_input.count() == 0:
        # Click "Sign in" / "Continue" / "Next" / "Log in" to advance.
        if not _click_button_named(page, "Sign in", "Continue", "Next", "Log in"):
            email_input.press("Enter")
        try:
            page.locator('input[type="password"]').first.wait_for(timeout=15_000)
        except Exception:
            return False
        pw_input = page.locator('input[type="password"]').first

    pw_input.fill(creds["password"])
    if not _click_button_named(page, "Sign in", "Log in", "Login", "Continue"):
        pw_input.press("Enter")

    # Wait until we're off any auth surface.
    try:
        page.wait_for_function(
            "() => {"
            "  const u = location.href.toLowerCase();"
            "  return !['login','log-in','signin','sign-in','/auth/','/oauth','authorize']"
            "    .some(m => u.includes(m));"
            "}",
            timeout=25_000,
        )
    except Exception:
        return not is_on_auth_page(page)
    page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT)
    return True


def deactivate(context, creds: dict, target_user: str) -> str:
    import time

    page = get_page(context, "maxon")
    page.set_default_timeout(DEFAULT_TIMEOUT)

    # Defensive cleanup: dismiss any leftover dropdown/dialog from the
    # previous task — its backdrop would intercept clicks on the next row.
    for _ in range(3):
        try:
            page.keyboard.press("Escape")
        except Exception:
            break
        time.sleep(0.2)

    if not session_attr(context, SESSION_KEY):
        page.goto(creds["url"])
        if is_on_auth_page(page) or not _on_app(page):
            if not _login(page, creds):
                return "failed: login"
            page.goto(creds["url"])
        set_session_attr(context, SESSION_KEY, True)
    else:
        page.goto(creds["url"])

    try:
        page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT)
    except Exception:
        pass

    # Click Users tab by its stable ID (role="tab" — get_by_role("link") may
    # not match reliably across renders).
    users_tab = page.locator("#dashboard-tabs-tab-users").first
    if users_tab.count():
        try:
            users_tab.click()
            page.wait_for_selector(
                "#dashboard-tabs-tabpane-users", state="visible", timeout=8_000
            )
        except Exception:
            pass

    # Find the user row. find-only stops here with a screenshot.
    status = find_user_row(page, target_user)
    if is_find_only():
        return status
    if status != "found":
        return status

    # ---- Destructive flow ----
    pane = page.locator("#dashboard-tabs-tabpane-users").first
    row_scope = pane if pane.count() else page
    row = row_scope.locator(f'tr:has-text("{target_user}")').first
    if row.count() == 0:
        return "needs-confirmation: row-disappeared-after-find"
    item_id = row.get_attribute("data-item-id")
    if not item_id:
        return "needs-confirmation: row-has-no-item-id"

    # From here on, anchor everything by data-item-id to be safe.
    row = page.locator(f'tr[data-item-id="{item_id}"]').first
    if target_user.lower() not in row.inner_text().lower():
        return "failed: row-text-mismatch"

    # 1. Open kebab menu in the row
    kebab = row.locator(".more-button .dropdown-toggle").first
    if kebab.count() == 0:
        return "failed: kebab-not-found"
    try:
        kebab.scroll_into_view_if_needed()
        kebab.click()
    except Exception as e:
        return f"failed: kebab-click ({type(e).__name__})"

    # 2. Click "Remove from team" menu item
    remove_item = page.locator(
        '.dropdown-menu.show a:has-text("Remove from team")'
    ).first
    try:
        remove_item.wait_for(timeout=5_000)
        remove_item.click()
    except Exception:
        return "failed: remove-menu-item-not-found"

    # 3. Wait for confirmation dialog
    try:
        dialog = page.locator('[role="dialog"]').first
        dialog.wait_for(timeout=5_000)
    except Exception:
        return "needs-confirmation: no-confirm-dialog"

    dlg_text = dialog.inner_text().lower()
    # Maxon's dialog says "Are you sure you want to remove user <Name> from your
    # team?" — verify it's the remove-user dialog (no email comparison since
    # the dialog shows display-name only, not email).
    if "remove user" not in dlg_text or "team" not in dlg_text:
        return "needs-confirmation: dialog-text-unexpected"

    # 4. Confirm — click "YES, DELETE"
    confirm = dialog.get_by_role(
        "button", name=re.compile(r"^(yes,?\s*delete|delete|remove)$", re.I)
    ).first
    if confirm.count() == 0:
        confirm = dialog.locator("button.btn-primary").last
    if confirm.count() == 0:
        return "failed: confirm-button-not-found"
    confirm.click()

    # 5. Soft success check — row by data-item-id should be gone
    try:
        page.locator(f'tr[data-item-id="{item_id}"]').first.wait_for(
            state="detached", timeout=8_000
        )
        return "deactivated"
    except Exception:
        return "needs-confirmation: row-still-present"
