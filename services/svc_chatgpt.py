"""ChatGPT (chatgpt.com Team admin) invite plugin.

Session-auth: relies on the Playwright persistent profile (.browser_profile/)
having an authenticated ChatGPT admin session. Do a one-time HEADED run and
sign in manually if cookies are stale — the plugin reports that state via
`needs-login: ...` instead of trying to type a password.

Domain guard: this plugin is intentionally scoped to fluytstudio.net users
(per workflow rule). Other domains are refused with
`skipped: not-fluytstudio-domain`.

Required env (.env):
    SERVICE_CHATGPT_URL=https://chatgpt.com/admin/members
    SERVICE_CHATGPT_AUTH=session
"""
from __future__ import annotations

import re
import time

from services._common import (
    get_page,
    is_on_auth_page,
    solve_cloudflare_turnstile,
    submit_totp,
)

DEFAULT_TIMEOUT = 20_000
SETTLE_TIMEOUT = 4_000
LOGIN_TIMEOUT = 25_000
MANUAL_FALLBACK_TIMEOUT = 300_000  # 5 min for human to finish a stuck login
SESSION_KEY = "_chatgpt_signed_in"

ALLOWED_DOMAIN = "fluytstudio.net"


def _login(page, creds: dict) -> bool:
    """Attempt scripted login at chatgpt.com / auth.openai.com.

    Returns True if the post-login URL lands on /admin/members. Returns False
    if anything in the scripted path fails (e.g. captcha, unexpected layout) —
    the caller falls back to manual wait.
    """
    login = creds.get("login")
    password = creds.get("password")
    if not (login and password):
        return False

    # Step 1: email field. OpenAI's auth screen uses input[name="username"].
    try:
        email_input = page.locator(
            'input[name="username"], input[type="email"], input#email'
        ).first
        email_input.wait_for(timeout=LOGIN_TIMEOUT)
        email_input.fill(login)
    except Exception:
        return False

    # Click Continue / Submit.
    try:
        cont = page.get_by_role("button", name=re.compile(r"^(Continue|Next|Submit)\b", re.I)).first
        if cont.count() == 0:
            cont = page.locator('button[type="submit"], button[name="action"]').first
        cont.click(timeout=8_000)
    except Exception:
        try:
            email_input.press("Enter")
        except Exception:
            return False

    # Step 2: password field.
    try:
        pwd_input = page.locator('input[type="password"], input#password').first
        pwd_input.wait_for(timeout=LOGIN_TIMEOUT)
        pwd_input.fill(password)
    except Exception:
        return False

    try:
        cont = page.get_by_role("button", name=re.compile(r"^(Continue|Sign\s*in|Log\s*in|Submit)\b", re.I)).first
        if cont.count() == 0:
            cont = page.locator('button[type="submit"]').first
        cont.click(timeout=8_000)
    except Exception:
        try:
            pwd_input.press("Enter")
        except Exception:
            return False

    # Step 3: optional TOTP. submit_totp returns False if no OTP input shows up,
    # which is the expected case when 2FA isn't enabled on the admin account.
    submit_totp(page, "SERVICE_CHATGPT_2FA_SECRET", retries=1)

    # Final wait — landing on /admin (any sub-path) is success.
    try:
        page.wait_for_url(re.compile(r"chatgpt\.com/admin"), timeout=LOGIN_TIMEOUT)
        return True
    except Exception:
        return False


def _is_allowed_domain(email: str) -> bool:
    if "@" not in email:
        return False
    return email.rsplit("@", 1)[1].strip().lower() == ALLOWED_DOMAIN


def _on_admin_members(page) -> bool:
    url = page.url.lower()
    return "chatgpt.com/admin/members" in url or "chatgpt.com/admin" in url


# Selector that should match SOMETHING on the rendered members admin: the
# Invite button, the page search input, the "Members" heading, or any iframe-
# free member row. If none of these exist, the page is either still loading or
# blocked by a Cloudflare / similar challenge.
ADMIN_SHELL_SEL = (
    'button:has-text("Invite members"), button:has-text("Invite"), '
    'button:has-text("Add members"), '
    'input[type="search"], input[placeholder*="Search" i], '
    'h1:has-text("Members"), h2:has-text("Members"), '
    '[role="heading"]:has-text("Members"), '
    'table tbody tr'
)


def _admin_shell_visible(page) -> bool:
    try:
        return page.locator(ADMIN_SHELL_SEL).count() > 0
    except Exception:
        return False


def _wait_for_admin_shell(page, quick_timeout_s: float = 8.0, slow_timeout_s: float = 180.0) -> bool:
    """Wait for the members admin to render. First a quick poll; if that
    times out, attempt to solve any Cloudflare Turnstile via solvecaptcha,
    then fall back to a longer wait so a human can also intervene."""
    # Quick path: page is just slow to render.
    deadline = time.time() + quick_timeout_s
    while time.time() < deadline:
        if _admin_shell_visible(page):
            return True
        time.sleep(0.4)

    # Try to solve any Cloudflare Turnstile gate via solvecaptcha.
    print("    members admin didn't render — attempting to solve Cloudflare Turnstile via solvecaptcha...")
    ok, detail = solve_cloudflare_turnstile(page)
    print(f"    solvecaptcha: {detail}")
    if ok:
        # Token was injected; give Cloudflare a moment to validate, then
        # check whether the gate cleared. If the page didn't auto-reload,
        # nudge it so the verified cookie/header takes effect.
        time.sleep(2.0)
        if not _admin_shell_visible(page):
            try:
                page.reload()
                page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT)
            except Exception:
                pass
        # Quick re-check after the reload.
        for _ in range(15):
            if _admin_shell_visible(page):
                return True
            time.sleep(0.5)

    # Slow path: still gated. Let the operator solve it by hand.
    print(
        "    still gated — solve the Cloudflare challenge manually in the "
        f"visible window; waiting up to {int(slow_timeout_s)}s..."
    )
    deadline = time.time() + slow_timeout_s
    while time.time() < deadline:
        if _admin_shell_visible(page):
            try:
                page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT)
            except Exception:
                pass
            return True
        time.sleep(1.0)
    return False


def _open_invite_dialog(page) -> str | None:
    """Click the 'Invite members' / '+ Invite' button on the Members page.
    Returns None on success or a 'failed: ...' status."""
    # Strategy 1 — role-based with the common label patterns.
    btn = page.get_by_role(
        "button", name=re.compile(r"^(Invite\s+members?|\+\s*Invite|Add\s+members?|Invite)\b", re.I)
    ).first
    if btn.count() == 0:
        btn = page.locator(
            'button:has-text("Invite members"), button:has-text("Invite"), '
            'button:has-text("Add members"), a:has-text("Invite members")'
        ).first
    if btn.count() == 0:
        return "failed: invite-button-not-found"
    try:
        btn.click(timeout=8_000)
    except Exception as e:
        return f"failed: invite-click ({type(e).__name__})"

    try:
        page.locator('[role="dialog"]:visible').last.wait_for(timeout=8_000)
    except Exception:
        return "failed: invite-dialog-not-shown"
    return None


def _fill_invite_email(dialog, email: str) -> bool:
    """Type the email into the dialog. Chatgpt's invite UIs typically use
    a tag-style input — commit the chip with Enter or comma."""
    candidates = dialog.locator(
        'input[type="email"]:visible, '
        'input[placeholder*="email" i]:visible, '
        'textarea[placeholder*="email" i]:visible, '
        'input[aria-label*="email" i]:visible, '
        'textarea[aria-label*="email" i]:visible'
    )
    if candidates.count() == 0:
        candidates = dialog.locator(
            'input[type="text"]:visible, input:not([type]):visible, textarea:visible'
        )
    if candidates.count() == 0:
        return False
    field = candidates.first
    try:
        field.click()
        field.fill("")
        field.type(email, delay=15)
        # Commit the chip — most tag-inputs accept Enter; harmless if the
        # field is plain text and already accepts the value as-is.
        field.press("Enter")
    except Exception:
        return False
    return True


def _click_send_invite(dialog) -> str | None:
    """Click the dialog's primary send button. Wait for it to enable first."""
    primary_re = re.compile(r"^(Send\s+invites?|Send|Invite|Add)\b", re.I)
    send = dialog.get_by_role("button", name=primary_re).first
    if send.count() == 0:
        send = dialog.locator(
            'button:has-text("Send invite"), button:has-text("Send invites"), '
            'button:has-text("Send"), button:has-text("Invite"), button:has-text("Add")'
        ).last
    if send.count() == 0:
        return "failed: send-button-not-found"

    # Wait for the button to be enabled.
    try:
        dialog.page.wait_for_function(
            "() => Array.from(document.querySelectorAll('[role=dialog]'))"
            "  .filter(d => d.offsetWidth || d.offsetHeight || d.getClientRects().length)"
            "  .flatMap(d => Array.from(d.querySelectorAll('button')))"
            "  .some(b => /^(send(\\s+invites?)?|invite|add)\\b/i.test(b.textContent.trim())"
            "    && !b.disabled && b.getAttribute('aria-disabled') !== 'true')",
            timeout=5_000,
        )
    except Exception:
        return "failed: send-button-not-enabled"

    try:
        send.click(timeout=8_000)
    except Exception as e:
        return f"failed: send-click ({type(e).__name__})"
    return None


def invite(context, creds: dict, target_user: str, role: str = "full") -> str:
    """Invite `target_user` (must be @fluytstudio.net) into the ChatGPT Team
    workspace via chatgpt.com/admin/members.

    `role` is accepted for interface parity with svc_figma.invite but is not
    used here — ChatGPT Team invites use a default seat/role.

    Returns one of:
      "invited"                          — send clicked, dialog dismissed.
      "already-invited"                  — target email already in members list.
      "skipped: not-fluytstudio-domain"  — guard refused a non-fluytstudio email.
      "needs-login: <details>"           — session cold, do a headed run + sign in.
      "needs-confirmation: ..." / "failed: ..." — UI step failed mid-flow.
    """
    if not _is_allowed_domain(target_user):
        return "skipped: not-fluytstudio-domain"

    page = get_page(context, "chatgpt")
    page.set_default_timeout(DEFAULT_TIMEOUT)

    # Go straight to the admin members page — the persistent profile should
    # already have an authenticated session.
    url = creds.get("url") or "https://chatgpt.com/admin/members"
    page.goto(url)
    try:
        page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT)
    except Exception:
        pass

    # If the persistent profile isn't authenticated yet, try scripted login
    # first (using SERVICE_CHATGPT_LOGIN/PASSWORD from .env). If that doesn't
    # complete — usually because OpenAI surfaces an hCaptcha — fall back to
    # waiting for the operator to finish signing in manually.
    if not _on_admin_members(page) or is_on_auth_page(page):
        scripted_ok = False
        if creds.get("login") and creds.get("password"):
            print("    attempting scripted login...")
            scripted_ok = _login(page, creds)
            if scripted_ok:
                try:
                    page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT)
                except Exception:
                    pass
                # Make sure we're actually on the members page (login may have
                # redirected to /admin root or another sub-path).
                if not _on_admin_members(page):
                    try:
                        page.goto(url)
                    except Exception:
                        pass

        if not _on_admin_members(page) or is_on_auth_page(page):
            print(
                "    scripted login didn't complete (likely a captcha or 2FA). "
                "waiting up to 5 minutes for you to finish signing in manually..."
            )
            try:
                page.wait_for_url(
                    re.compile(r"chatgpt\.com/admin/members"),
                    timeout=MANUAL_FALLBACK_TIMEOUT,
                )
            except Exception:
                return (
                    "needs-login: not signed in after 5 min — run headed and "
                    "sign into chatgpt.com/admin/members; cookies will persist "
                    "in .browser_profile/"
                )
            try:
                page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT)
            except Exception:
                pass

    # Cloudflare gates /admin/members behind a Turnstile challenge even when
    # logged in. The URL contains /admin so _on_admin_members returns True,
    # but the body holds only the iframe-wrapped challenge — the real members
    # table never renders. The simplest signal we have is the absence of any
    # known admin-shell element (Invite button, search box, Members heading,
    # member row). Wait actively for one of those to show up.
    if not _wait_for_admin_shell(page):
        return "needs-human: admin shell never rendered (challenge unsolved?)"

    setattr(context, SESSION_KEY, True)

    # Quick "already a member?" probe — scan the page text. The members list
    # renders email addresses, so a substring match is enough.
    try:
        body = page.locator("body").inner_text()
        if target_user.lower() in body.lower():
            return "already-invited"
    except Exception:
        pass

    open_err = _open_invite_dialog(page)
    if open_err:
        return open_err

    dialog = page.locator('[role="dialog"]:visible').last

    if not _fill_invite_email(dialog, target_user):
        return "failed: email-field-not-found"

    # Brief settle so any role/seat picker can render — we don't touch it
    # (ChatGPT Team defaults to Member, which is what's wanted).
    time.sleep(0.4)

    send_err = _click_send_invite(dialog)
    if send_err:
        return send_err

    # Soft success: dialog dismisses or a toast confirms.
    try:
        dialog.wait_for(state="detached", timeout=8_000)
        return "invited"
    except Exception:
        try:
            body = page.locator("body").inner_text().lower()
            if "invite sent" in body or "invitation sent" in body or "invited" in body:
                return "invited"
        except Exception:
            pass
        return "needs-confirmation: dialog-still-open"
