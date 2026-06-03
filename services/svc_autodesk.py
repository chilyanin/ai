"""Autodesk (manage.autodesk.com) deactivation plugin.

Auth: staged sign-in — email → Next → password → Sign in → TOTP → admin.
The TOTP code is generated from AUTODESK_2FA_SECRET (same TOTP module as
the Figma plugin).

CAPTCHA note: the sign-in page (signin.autodesk.com) is gated by *invisible
hCaptcha* (sitekey 6670fa76-…). In practice it passes silently — across many
fresh-profile/headless/bot-UA attempts it never escalated to a visible
challenge for this account. It is NOT auto-solved: solvecaptcha.com (our only
configured solver) does not support hCaptcha — its API rejects method=hcaptcha
with ERROR_METHOD_CALL. If a visible challenge ever appears, the operator
solves it by hand in the visible browser within the wait window below. To
automate it, wire in an hCaptcha-capable provider (2captcha / CapSolver / etc.).

Status:
  This plugin gets to a logged-in state and, when --find-only is set, stops
  there with a screenshot. The path to the specific user-management screen
  inside the Autodesk admin still needs to be filled in once the operator
  confirms the post-login UI. Look for ``# TODO(autodesk-flow):`` markers.

Required env:
    AUTODESK_URL          admin entry URL (e.g. https://manage.autodesk.com/)
    AUTODESK_LOGIN        admin email
    AUTODESK_PASSWORD     admin password
    AUTODESK_2FA_SECRET   base32 TOTP secret
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
# Long wait so a human can solve a CAPTCHA in the visible browser window.
HUMAN_WAIT_MS = 180_000
SESSION_KEY = "_autodesk_signed_in"
ADMIN_HOSTNAME = "manage.autodesk.com"


def _on_admin_app(page) -> bool:
    return ADMIN_HOSTNAME in page.url.lower()


SSO_URL = "https://accounts.autodesk.com/Authentication/LogOn"


def _try_click_sign_in(page) -> bool:
    """Try several selectors to click a "Sign in" entry-point. Returns True
    if any click was kicked off (no guarantee the page navigated)."""
    selectors = [
        'a:has-text("Sign in")',
        'button:has-text("Sign in")',
        'a:has-text("Sign In")',
        'button:has-text("Sign In")',
        '[data-testid*="signin" i]',
        '[data-test*="sign-in" i]',
    ]
    for sel in selectors:
        loc = page.locator(sel).first
        if loc.count() == 0:
            continue
        try:
            loc.click()
            return True
        except Exception:
            continue
    return False


def _login(page, creds: dict) -> bool:
    # Always go straight to the Autodesk SSO endpoint with a return URL back
    # to the caller's target. This avoids the www.autodesk.com marketing page
    # that /uma/* redirects to when logged out.
    from urllib.parse import quote
    page.goto(f"{SSO_URL}?ReturnUrl={quote(creds['url'])}")
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5_000)
    except Exception:
        pass

    # If the persistent profile is already authenticated, Autodesk skips the
    # form entirely and bounces us straight to ReturnUrl.
    if _on_admin_app(page):
        return True

    email_sel = (
        'input#userName, input[name="UserName"], '
        'input[type="email"], input[name="email"]'
    )

    # Stage 1: email. Wait long here — Autodesk gates the email→password
    # transition behind invisible hCaptcha, which we do not solve
    # programmatically (see the CAPTCHA note in the module docstring). With a
    # visible browser the operator can solve any challenge by hand within the
    # wait window.
    print(
        "    [autodesk] if a CAPTCHA appears in the browser window, solve it "
        f"manually within {HUMAN_WAIT_MS // 1000}s"
    )
    try:
        email = page.locator(email_sel).first
        email.wait_for(timeout=HUMAN_WAIT_MS)
        email.fill(creds["login"])
    except Exception:
        return False

    next_btn = page.get_by_role("button", name=re.compile(r"^(Next|Continue)$", re.I)).first
    if next_btn.count():
        next_btn.click()
    else:
        email.press("Enter")

    # Stage 2: password (also generously waited — Autodesk may interrupt with
    # another challenge between email and password).
    try:
        pw = page.locator('input[type="password"], input#password').first
        pw.wait_for(timeout=HUMAN_WAIT_MS)
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

    # Stage 3: 2FA (TOTP). Autodesk may also ask "trust this device?" — handle
    # that with a generic "No"/"Skip" if shown.
    submit_totp(page, "AUTODESK_2FA_SECRET")

    # Optional "trust device" prompt
    for label in ("Skip", "Not now", "No thanks", "Maybe later"):
        b = page.get_by_role("button", name=re.compile(rf"^{label}$", re.I)).first
        if b.count():
            try:
                b.click(timeout=2_000)
                break
            except Exception:
                pass

    # Strict success check: we must be on manage.autodesk.com (the admin
    # app), not on the public www.autodesk.com marketing site.
    try:
        page.wait_for_function(
            "() => {"
            "  const h = location.hostname.toLowerCase();"
            "  const u = location.href.toLowerCase();"
            "  if (!h.startsWith('manage.autodesk.com')) return false;"
            "  return !['login','log-in','signin','sign-in','/auth/','/oauth','authorize']"
            "    .some(m => u.includes(m));"
            "}",
            timeout=30_000,
        )
    except Exception:
        return False
    page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT)
    return True


def _on_marketing_page(page) -> bool:
    """www.autodesk.com marketing page contains very recognizable text."""
    try:
        body = page.locator("body").inner_text().lower()
    except Exception:
        return False
    if "welcome to autodesk account" in body:
        return True
    if "manage product assignments" in body and "create account" in body:
        return True
    return False


def deactivate(context, creds: dict, target_user: str) -> str:
    page = get_page(context, "autodesk")
    page.set_default_timeout(DEFAULT_TIMEOUT)

    if not session_attr(context, SESSION_KEY):
        # First task in this context — always log in fresh. URL-based detection
        # is unreliable here because /uma/* loads briefly even when logged out
        # before JS-redirecting to the marketing page.
        if not _login(page, creds):
            return "failed: login"
        set_session_attr(context, SESSION_KEY, True)

    page.goto(creds["url"])
    try:
        page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT)
    except Exception:
        pass

    if not _on_admin_app(page) or _on_marketing_page(page):
        return "failed: not-authenticated-on-admin"

    try:
        page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT)
    except Exception:
        pass
    try:
        page.wait_for_function(
            "() => document.body && document.body.innerText && "
            "document.body.innerText.trim().length > 50",
            timeout=6_000,
        )
    except Exception:
        pass

    # Find the user row. find-only stops here with a screenshot.
    status = find_user_row(page, target_user)
    if is_find_only():
        return status
    if status != "found":
        return status

    # TODO(autodesk-flow): once "found", click the row's deactivate / remove
    # action and confirm. Operator should describe the exact UI so we wire it.
    return "needs-confirmation: autodesk-deactivate-flow-not-implemented"
