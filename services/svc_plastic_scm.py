"""Plastic SCM (cloud dashboard) deactivation plugin.

Auth flow:
    Plastic SCM login page → "Sign in with Unity" → Unity ID OAuth
    (email → password → TOTP) → redirect back to the requested
    /dashboard/cloud/<org>/users-and-groups URL.

Required env:
    PLASTIC_SCM_URL          users-and-groups URL
    PLASTIC_SCM_LOGIN        Unity-ID email
    PLASTIC_SCM_PASSWORD     Unity-ID password
    PLASTIC_SCM_2FA_SECRET   base32 TOTP secret (or otpauth:// URL)

Deactivation flow:
    After locating the user row, click its red trash button
    (button.btn-delete[data-email=…]). dashboard.min.js pops a native
    window.confirm("… remove user '<email>'?") and, on accept, fires
    DELETE /api/cloud/organizations/<org>/users/<email>, then removes the
    row from the DOM on success. We accept the confirm via a one-shot dialog
    handler and verify the row detached.

Status:
    "found"                          — find-only located the user row
    "user-not-found"                 — search returned no match
    "deactivated"                    — row removed after confirmed delete
    "needs-confirmation: row-still-present" — clicked + confirmed but row lingered
    "failed: <reason>"               — auth, navigation, or delete failure
"""
from __future__ import annotations

import re

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
SESSION_KEY = "_plastic_scm_signed_in"
APP_HOSTNAME = "plasticscm.com"


def _on_app(page) -> bool:
    return APP_HOSTNAME in page.url.lower()


def _click_sign_in_with_unity(page) -> bool:
    """Click the "Sign in with Unity" button on Plastic SCM's login page."""
    selectors = [
        'a:has-text("Sign in with Unity")',
        'button:has-text("Sign in with Unity")',
        'a:has-text("Unity")',
        'button:has-text("Unity")',
    ]
    for sel in selectors:
        loc = page.locator(sel).first
        if loc.count():
            try:
                loc.click()
                return True
            except Exception:
                continue
    return False


def _login(page, creds: dict) -> bool:
    page.goto(creds["url"])
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5_000)
    except Exception:
        pass

    # If persistent cookies already authenticated us, the URL stays on the
    # users-and-groups page and there's no auth UI to handle.
    if _on_app(page) and not is_on_auth_page(page):
        return True

    # We're either on Plastic SCM's own login page (with "Sign in with Unity"
    # button) or already redirected to Unity. Click the SSO entry-point if
    # we're on Plastic SCM's login.
    if "plasticscm.com" in page.url.lower() and is_on_auth_page(page):
        _click_sign_in_with_unity(page)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except Exception:
            pass

    # ---- Unity ID login ----
    # Stage 1: email
    email_sel = (
        'input[type="email"], input[name="email"], '
        'input[name="userEmail"], input[autocomplete="username"], '
        'input[id*="email" i]'
    )
    try:
        email = page.locator(email_sel).first
        email.wait_for(timeout=20_000)
        email.fill(creds["login"])
    except Exception:
        return False

    next_btn = page.get_by_role(
        "button", name=re.compile(r"^(Next|Continue|Sign\s*in|Log\s*in)$", re.I)
    ).first
    if next_btn.count():
        next_btn.click()
    else:
        email.press("Enter")

    # Stage 2: password (Unity may use a separate page)
    pw_sel = 'input[type="password"], input#password, input[name="password"]'
    try:
        pw = page.locator(pw_sel).first
        pw.wait_for(timeout=20_000)
        pw.fill(creds["password"])
    except Exception:
        return False

    sign_btn = page.get_by_role(
        "button", name=re.compile(r"^(Sign\s*in|Log\s*in|Continue)$", re.I)
    ).first
    if sign_btn.count():
        sign_btn.click()
    else:
        pw.press("Enter")

    # Stage 3: TOTP — Unity 2FA. Plugin reads PLASTIC_SCM_2FA_SECRET via
    # submit_totp's env-var name lookup.
    submit_totp(page, "PLASTIC_SCM_2FA_SECRET")

    # Optional "trust this device?" prompts.
    for label in ("Skip", "Not now", "No thanks", "Maybe later", "Remind me later"):
        b = page.get_by_role("button", name=re.compile(rf"^{label}$", re.I)).first
        if b.count():
            try:
                b.click(timeout=2_000)
                break
            except Exception:
                pass

    # Wait for the redirect back to the Plastic SCM domain to settle.
    try:
        page.wait_for_function(
            f"() => location.hostname.includes('{APP_HOSTNAME}')",
            timeout=30_000,
        )
    except Exception:
        return False
    try:
        page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT)
    except Exception:
        pass
    return True


def deactivate(context, creds: dict, target_user: str) -> str:
    page = get_page(context, "plastic_scm")
    page.set_default_timeout(DEFAULT_TIMEOUT)

    if not session_attr(context, SESSION_KEY):
        if not _login(page, creds):
            return "failed: login"
        # After login, ensure we're on the users-and-groups URL.
        page.goto(creds["url"])
        set_session_attr(context, SESSION_KEY, True)
    else:
        page.goto(creds["url"])

    try:
        page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT)
    except Exception:
        pass

    if not _on_app(page):
        return "failed: not-on-plastic-scm"

    # Plastic SCM renders user rows as <div class="row user-row …">, which
    # our default row roles don't cover.
    status = find_user_row(
        page, target_user, extra_row_selectors=("div.user-row",)
    )
    if is_find_only():
        return status
    if status != "found":
        return status

    return _remove_user(page, target_user)


def _remove_user(page, target_user: str) -> str:
    """Click the row's red trash button and confirm removal.

    The per-row delete button (``button.btn-delete[data-email=…]``) is wired in
    dashboard.min.js to:
      1. pop a native ``window.confirm('… remove user "<email>"?')``
      2. on accept, ``$.ajax DELETE /api/cloud/organizations/<org>/users/<email>``
      3. on success, remove the ``.user-row`` from the DOM
      4. on failure, reveal an inline ``.alert-danger`` inside the row.

    So we register a one-shot dialog handler that accepts only the matching
    "remove user" confirm, click the button, then verify the row detached.
    """
    # data-email holds the exact registered address; match case-insensitively
    # in case the Asana title differs in case from what Plastic stored.
    btn = page.locator(
        f'div.user-row button.btn-delete[data-email="{target_user}" i]'
    ).first
    if btn.count() == 0:
        return "failed: delete-button-not-found"

    # Accept the native confirm — but only if it's the expected remove-user
    # prompt for this user, so a stray dialog can never trigger a deletion.
    def _on_dialog(dialog) -> None:
        msg = (getattr(dialog, "message", "") or "").lower()
        if "remove user" in msg and target_user.lower() in msg:
            dialog.accept()
        else:
            dialog.dismiss()

    page.once("dialog", _on_dialog)

    try:
        btn.scroll_into_view_if_needed()
        btn.click()
    except Exception as e:  # noqa: BLE001
        return f"failed: delete-click ({type(e).__name__})"

    # Success = the row is removed from the DOM by the AJAX success callback.
    row = page.locator(f'div.user-row:has-text("{target_user}")').first
    try:
        row.wait_for(state="detached", timeout=10_000)
        return "deactivated"
    except Exception:
        pass

    # Still present — surface the inline error if the API rejected the delete.
    try:
        err = page.locator("div.user-row .alert-danger:not(.d-none)").first
        if err.count():
            detail = (err.inner_text() or "").strip()[:120]
            return f"failed: delete-rejected ({detail})" if detail else "failed: delete-rejected"
    except Exception:
        pass
    return "needs-confirmation: row-still-present"
