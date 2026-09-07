"""Figma deactivation plugin.

Flow per user instructions: login → 2FA → Admin → People → row 3-dots → Remove.

Required env (set in .env):
    FIGMA_URL          login URL (typically https://www.figma.com/login)
    FIGMA_LOGIN        admin email
    FIGMA_PASSWORD     admin password
    FIGMA_2FA_SECRET   base32 TOTP secret

Safety: before clicking the destructive "Remove" item we re-assert the row's
text contains target_user. If it doesn't, we abort with `failed: row-mismatch`.
"""
from __future__ import annotations

import os
import re
import time

from services._common import get_page, is_on_auth_page
from totp import generate as totp_generate, seconds_remaining

DEFAULT_TIMEOUT = 20_000
SETTLE_TIMEOUT = 4_000
SESSION_KEY = "_figma_signed_in"


def _submit_2fa_once(page, code_input, secret: str) -> None:
    # Wait until at least 8s remain in the current TOTP window so the code
    # is unlikely to expire between fill and server-side validation.
    while seconds_remaining() < 8:
        time.sleep(1)
    code = totp_generate(secret)
    code_input.fill("")
    code_input.fill(code)
    # Click the explicit "Log in" button rather than relying on Enter — focus
    # may have left the input after fill.
    btn = page.get_by_role("button", name=re.compile(r"^Log\s*in$", re.I)).first
    if btn.count():
        btn.click()
    else:
        code_input.press("Enter")


def _fill_2fa(page) -> bool:
    """Submit a TOTP code on the 2FA page if one is shown.

    Retries once if Figma reports "Wrong code" — sometimes a stale TOTP slips
    through on the first attempt.
    """
    secret = os.environ.get("FIGMA_2FA_SECRET")
    if not secret:
        return False
    code_input = page.locator(
        'input[autocomplete="one-time-code"], input[name="code"], input[name="totp"]'
    ).first
    try:
        code_input.wait_for(timeout=10_000)
    except Exception:
        return False

    for attempt in range(2):
        _submit_2fa_once(page, code_input, secret)
        # Give Figma a few seconds to either redirect or surface "Wrong code".
        try:
            page.wait_for_url(
                re.compile(r"figma\.com/(files|admin|team|org)"), timeout=8_000
            )
            return True
        except Exception:
            pass
        body = page.locator("body").inner_text().lower()
        if "wrong code" not in body and "invalid" not in body:
            # No error shown — assume the redirect just hasn't completed yet.
            return True
        # Wrong code: wait for the next TOTP window and retry once.
        if attempt == 0:
            time.sleep(seconds_remaining() + 2)
    return False


def _click_first_present(page, *locators):
    for loc in locators:
        if loc.count():
            loc.first.click()
            return True
    return False


def _fill_dialog_name_input(dialog, display_name: str) -> bool:
    """Fill Figma's confirm-name input in a way React reliably observes."""
    name_input = dialog.locator(
        'input[type="text"]:visible, input:not([type]):visible, '
        'input[type="search"]:visible, textarea:visible'
    ).last
    if name_input.count() == 0:
        return False

    try:
        name_input.click()
        name_input.press("ControlOrMeta+a")
        name_input.press("Delete")
        name_input.type(display_name, delay=15)
    except Exception:
        pass

    # If normal typing didn't stick, set the value via the native property setter
    # and dispatch input/change events. This is the React-friendly imperative path.
    try:
        current = name_input.input_value()
    except Exception:
        current = ""
    if current.strip() == display_name:
        return True

    try:
        name_input.evaluate(
            """(el, value) => {
                const proto = el instanceof HTMLTextAreaElement
                    ? HTMLTextAreaElement.prototype
                    : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
                setter.call(el, value);
                el.dispatchEvent(new Event("input", { bubbles: true }));
                el.dispatchEvent(new Event("change", { bubbles: true }));
            }""",
            display_name,
        )
    except Exception:
        return False

    try:
        return name_input.input_value().strip() == display_name
    except Exception:
        return True


def _understand_checkbox_checked(dialog) -> bool:
    """True if the 'I understand…' confirmation box is currently ticked."""
    cb = dialog.locator('input[type="checkbox"]').first
    if cb.count():
        try:
            return cb.is_checked()
        except Exception:
            pass
    rc = dialog.locator('[role="checkbox"]').first
    if rc.count():
        try:
            return rc.get_attribute("aria-checked") == "true"
        except Exception:
            pass
    return False


def _check_understand_box(dialog) -> bool:
    """Tick the remove dialog's 'I understand that this action can't be undone'
    checkbox. Figma renders a styled checkbox whose native <input> is visually
    hidden, so Playwright's .check()/.click() on the input fails actionability.
    Try the input with force, then the clickable label/text, then an ARIA
    checkbox. Returns True only once the box is observably checked."""
    if _understand_checkbox_checked(dialog):
        return True

    # Strategy 1 — native checkbox, forced past the visually-hidden input.
    cb = dialog.locator('input[type="checkbox"]').first
    if cb.count():
        for action in ("check", "click"):
            try:
                getattr(cb, action)(force=True, timeout=2_000)
            except Exception:
                continue
            if _understand_checkbox_checked(dialog):
                return True

    # Strategy 2 — click the visible label/row carrying the confirmation text.
    label = dialog.get_by_text(
        re.compile(r"I understand that this action", re.I)
    ).first
    if label.count():
        try:
            label.click(timeout=2_000)
        except Exception:
            pass
        if _understand_checkbox_checked(dialog):
            return True

    # Strategy 3 — ARIA checkbox role.
    rc = dialog.locator('[role="checkbox"]').first
    if rc.count():
        try:
            rc.click(timeout=2_000)
        except Exception:
            pass

    return _understand_checkbox_checked(dialog)


def _wait_remove_dialog_ready(dialog, timeout_s: float = 12.0) -> bool:
    """The remove-confirmation dialog opens with just a title and a loading
    spinner, then fetches its body (warning text + 'I understand' checkbox, or
    the name input) asynchronously. Reading the form before that arrives sees an
    empty body. Poll until a confirmation control has rendered. Returns True if
    one appeared, False on timeout (caller still proceeds defensively)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if dialog.locator(
                'input[type="checkbox"], [role="checkbox"], '
                'input[type="text"]:visible, textarea:visible'
            ).count():
                return True
            if re.search(
                r"I understand that this action", dialog.inner_text(), re.I
            ):
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def _dismiss_in_app_overlay(page) -> bool:
    """Close any Figma in-app survey/feedback overlay (curator-portal-target).
    These overlays can pop up at any time and intercept pointer events. Returns
    True if a portal was found and an attempt to close it was made."""
    portal = page.locator("#curator-portal-target").first
    if portal.count() == 0:
        return False
    # Is it actually rendering anything? An empty portal div is harmless.
    try:
        if portal.evaluate("el => el.offsetWidth === 0 || el.offsetHeight === 0"):
            return False
    except Exception:
        pass
    # Try close / dismiss buttons inside the portal.
    for sel in (
        'button[aria-label*="Close" i]',
        'button[aria-label*="Dismiss" i]',
        'button[aria-label*="close" i]',
        'button:has-text("×")',
        'button:has-text("Skip")',
        'button:has-text("Maybe later")',
        'button:has-text("Not now")',
    ):
        loc = portal.locator(sel).first
        if loc.count():
            try:
                loc.click(timeout=1500)
                time.sleep(0.4)
                return True
            except Exception:
                continue
    return False


def _login(page, creds) -> bool:
    page.goto(creds["url"])
    # If persistent cookies already authenticated us, the login form won't be
    # rendered — Figma redirects past /login. Treat that as success.
    try:
        page.locator('input[name="email"]').first.wait_for(timeout=5_000)
    except Exception:
        return not is_on_auth_page(page)
    page.fill('input[name="email"]', creds["login"])
    page.fill('input[name="password"]', creds["password"])
    page.locator('button[type="submit"]').first.click()
    _fill_2fa(page)
    try:
        page.wait_for_url(re.compile(r"figma\.com/(files|admin|team|org)"), timeout=25_000)
    except Exception:
        return False
    # Short post-login settle — 2s is enough for the sidebar to render the
    # Admin link in practice; networkidle returning earlier is a bonus.
    try:
        page.wait_for_load_state("networkidle", timeout=2_000)
    except Exception:
        pass
    return True


def _find_href(page, must_include: str, must_not: tuple = ()) -> str | None:
    """Return the first <a> href on the page containing must_include, that
    does NOT contain any of must_not."""
    for a in page.locator(f'a[href*="{must_include}"]').all():
        try:
            href = a.get_attribute("href") or ""
        except Exception:
            continue
        low = href.lower()
        if any(bad in low for bad in must_not):
            continue
        return href
    return None


def _wait_for_href(page, must_include: str, must_not: tuple = (), timeout_s: float = 20.0) -> str | None:
    """Poll for an <a> href to appear (the file browser/admin shell is an SPA
    that mounts the sidebar after page load — `networkidle` returns too early)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        href = _find_href(page, must_include, must_not)
        if href:
            return href
        time.sleep(0.4)
    return None


def _click_or_goto_admin(page) -> bool:
    """Get to the admin shell. Try in order: role-based click, href goto,
    visible text click. Returns True if navigation was kicked off."""
    # Strategy 1 — role-based click. The accessible name is e.g. "Admin 30",
    # so use a prefix regex. The sidebar mounts within a few hundred ms of
    # the post-login redirect — keep this short.
    admin_link = page.get_by_role("link", name=re.compile(r"^Admin\b", re.I)).first
    try:
        admin_link.wait_for(timeout=5_000)
        admin_link.click()
        return True
    except Exception:
        pass

    # Strategy 2 — find any anchor href containing /admin.
    href = _find_href(page, "/admin", must_not=("ical",))
    if href:
        if href.startswith("/"):
            href = "https://www.figma.com" + href
        page.goto(href)
        return True

    # Strategy 3 — wait for the link to appear via polling and try again.
    href = _wait_for_href(page, "/admin", must_not=("ical",), timeout_s=5.0)
    if href:
        if href.startswith("/"):
            href = "https://www.figma.com" + href
        page.goto(href)
        return True

    # Strategy 4 — last resort, click any visible "Admin" text.
    txt = page.get_by_text(re.compile(r"^Admin\b", re.I)).first
    try:
        txt.click(timeout=5_000)
        return True
    except Exception:
        return False


def _navigate_to_people(page) -> str | None:
    """Navigate Admin → People. Returns None on success, status string on failure."""
    if not _click_or_goto_admin(page):
        return "failed: admin-link-not-found"
    try:
        page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT)
    except Exception:
        pass

    # Wait for the admin shell to render the People nav item.
    try:
        page.wait_for_selector("text=People", timeout=8_000)
    except Exception:
        return "failed: people-link-not-rendered"

    people_href = _wait_for_href(page, "/people", timeout_s=10.0) or _wait_for_href(
        page, "/members", timeout_s=3.0
    )
    if people_href:
        if people_href.startswith("/"):
            people_href = "https://www.figma.com" + people_href
        page.goto(people_href)
    elif not _click_first_present(
        page,
        page.get_by_role("link", name=re.compile(r"^People", re.I)),
        page.get_by_role("button", name=re.compile(r"^People", re.I)),
        page.get_by_text("People", exact=True),
    ):
        return "failed: people-link-not-found"
    try:
        page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT)
    except Exception:
        pass
    return None


SEARCH_SEL = (
    'input[type="search"], '
    'input[placeholder*="Search" i], '
    'input[placeholder*="search" i]'
)


def _on_people_page(page) -> bool:
    """True if the current page is the Figma admin → People view (search box ready)."""
    url = page.url.lower()
    if "/people" not in url and "/members" not in url:
        return False
    return page.locator(SEARCH_SEL).count() > 0


ROLE_LABELS = {
    "full": re.compile(r"\bFull(\s+seat)?\b", re.I),
    "editor": re.compile(r"\b(Can\s+edit|Editor|Edit)\b", re.I),
    "viewer": re.compile(r"\b(Can\s+view|View(er)?|Restricted)\b", re.I),
    "dev": re.compile(r"\bDev(\s+seat)?\b", re.I),
}

# Asana's "Роль:" field is filled by humans and doesn't use our key names —
# real tasks carry "view", not "viewer". An unrecognised role must NOT quietly
# fall back to "full": that grants a more expensive, more privileged seat than
# was asked for, so it is refused instead (see _select_invite_role).
ROLE_ALIASES = {
    "full": "full",
    "full seat": "full",
    "editor": "editor",
    "edit": "editor",
    "can edit": "editor",
    "viewer": "viewer",
    "view": "viewer",
    "can view": "viewer",
    "restricted": "viewer",
    "dev": "dev",
    "dev seat": "dev",
    "developer": "dev",
}


def _resolve_role(role: str) -> str | None:
    """Map a task's role string onto a ROLE_LABELS key, or None if unknown."""
    key = re.sub(r"\s+", " ", (role or "").strip().lower())
    return ROLE_ALIASES.get(key)


def _ensure_on_people(page, creds) -> str | None:
    """Reach Admin → People, handling fresh login + cached-session cases.

    Returns None on success; a "failed: ..." status string on failure.
    """
    if not getattr(page.context, SESSION_KEY, False):
        if not _login(page, creds):
            return "failed: login-no-redirect"
        setattr(page.context, SESSION_KEY, True)
    elif not _on_people_page(page):
        page.goto("https://www.figma.com/files/recent")
        if is_on_auth_page(page):
            if not _login(page, creds):
                return "failed: login-no-redirect"

    for _ in range(3):
        try:
            page.keyboard.press("Escape")
        except Exception:
            break
        time.sleep(0.2)

    if not _on_people_page(page):
        nav_err = _navigate_to_people(page)
        if nav_err:
            return nav_err
        try:
            page.wait_for_selector(SEARCH_SEL, timeout=8_000)
        except Exception:
            return "failed: people-list-not-rendered"
    return None


def _open_invite_dialog(page) -> str | None:
    """Click the People-page 'Invite' button. Returns None on success or a
    'failed: ...' status."""
    _dismiss_in_app_overlay(page)
    invite_btn = page.get_by_role("button", name=re.compile(r"^Invite\b", re.I)).first
    if invite_btn.count() == 0:
        invite_btn = page.locator(
            'button:has-text("Invite"), a[role="button"]:has-text("Invite")'
        ).first
    if invite_btn.count() == 0:
        return "failed: invite-button-not-found"
    try:
        invite_btn.click(timeout=8_000)
    except Exception:
        _dismiss_in_app_overlay(page)
        try:
            invite_btn.click(timeout=5_000)
        except Exception as e:
            return f"failed: invite-click ({type(e).__name__})"

    try:
        page.locator('[role="dialog"]:visible').last.wait_for(timeout=8_000)
    except Exception:
        return "failed: invite-dialog-not-shown"
    return None


def _fill_invite_email(dialog, email: str) -> bool:
    """Type `email` into the dialog's email field. Figma's invite dialog uses
    a tag-style input that accepts comma/Enter to commit a chip."""
    candidates = dialog.locator(
        'input[type="email"]:visible, '
        'input[placeholder*="email" i]:visible, '
        'input[placeholder*="Email" i]:visible, '
        'textarea[placeholder*="email" i]:visible, '
        'input[aria-label*="email" i]:visible'
    )
    if candidates.count() == 0:
        # Last resort: any visible text input inside the dialog.
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
        # Commit the chip — Figma typically wants Enter or comma.
        field.press("Enter")
    except Exception:
        return False
    return True


def _find_seat_trigger(dialog):
    """Find the 'Seat type' dropdown trigger. Figma renders it as a plain
    <button> below a 'Seat type' label, with no aria-haspopup attribute — so
    we find it by proximity to that label rather than ARIA."""
    # Strategy 1 — button reachable from the "Seat type" label.
    label = dialog.locator('text=/^Seat\\s*type$/i').first
    if label.count():
        # Closest following button/combobox in DOM order.
        trigger = label.locator(
            'xpath=following::button[1] | following::*[@role="combobox"][1]'
        ).first
        if trigger.count():
            return trigger
    # Strategy 2 — common ARIA shapes.
    trigger = dialog.locator(
        'button[aria-haspopup], [role="combobox"]'
    ).first
    if trigger.count():
        return trigger
    # Strategy 3 — any button inside the dialog whose visible text is one of
    # the known seat labels (View / Full / Dev). Excludes Cancel/Send/Invite.
    for label_text in ("View", "Full", "Dev", "Editor", "Viewer"):
        btn = dialog.locator(f'button:has-text("{label_text}")').first
        if btn.count():
            try:
                txt = btn.inner_text().strip()
            except Exception:
                txt = ""
            if txt and txt not in ("Cancel", "Send invite", "Send", "Invite"):
                return btn
    return None


def _select_invite_role(dialog, role: str) -> str | None:
    """Pick the seat/role in the invite dialog. Returns None on success or a
    'failed: ...' status. If no role picker is visible (org defaults to one
    seat type), returns None — the default is accepted."""
    resolved = _resolve_role(role)
    if resolved is None:
        # Refuse rather than escalate to Full — see ROLE_ALIASES.
        return (
            f"needs-confirmation: unknown Figma seat {role!r} "
            f"(known: {', '.join(sorted(set(ROLE_ALIASES.values())))})"
        )
    pattern = ROLE_LABELS[resolved]

    trigger = _find_seat_trigger(dialog)
    if trigger is None:
        return None  # single-seat org, default accepted

    try:
        current = trigger.inner_text()
        if pattern.search(current or ""):
            return None
    except Exception:
        pass

    try:
        trigger.click(timeout=5_000)
    except Exception as e:
        return f"failed: role-trigger-click ({type(e).__name__})"

    # Figma's seat menu opens in a portal outside the dialog. The items are
    # NOT semantic menuitems — they're divs/buttons with the seat label as
    # visible text. Search the page broadly.
    page = dialog.page
    time.sleep(0.4)  # let the menu render

    # Strategy A — proper ARIA menuitem/option.
    option = page.get_by_role("menuitem", name=pattern).first
    if option.count() == 0:
        option = page.get_by_role("option", name=pattern).first
    # Strategy B — any visible element whose text is exactly the seat name.
    if option.count() == 0:
        # Prefer rows that explicitly look like menu/list options.
        option = page.locator(
            '[role="menuitem"]:visible, [role="option"]:visible, '
            'li:visible, [data-testid*="option" i]:visible'
        ).filter(has_text=pattern).first
    # Strategy C — broadest: any visible clickable with matching text, but
    # exclude the dialog body to avoid re-clicking the trigger itself.
    if option.count() == 0:
        option = page.locator(
            'div:visible, button:visible, span:visible'
        ).filter(has_text=pattern).first

    if option.count() == 0:
        return f"failed: role-option-not-found ({role})"

    try:
        option.click(timeout=5_000)
    except Exception as e:
        return f"failed: role-option-click ({type(e).__name__})"

    # Verify the trigger label updated.
    try:
        time.sleep(0.4)
        new_text = trigger.inner_text()
        if pattern.search(new_text or ""):
            return None
    except Exception:
        pass
    # Couldn't confirm, but the click went through — let Send button enablement
    # be the real signal.
    return None


def _click_send_invite(dialog) -> str | None:
    """Click the dialog's primary send button. Returns None on success or a
    'failed: ...' status."""
    send = dialog.get_by_role(
        "button", name=re.compile(r"^(Send\s+invite|Send|Invite)\b", re.I)
    ).first
    if send.count() == 0:
        send = dialog.locator(
            'button:has-text("Send invite"), button:has-text("Send"), '
            'button:has-text("Invite")'
        ).last
    if send.count() == 0:
        return "failed: send-button-not-found"
    # Wait for the button to be enabled — Figma disables it until the email
    # input has a committed, valid chip.
    try:
        dialog.page.wait_for_function(
            "() => Array.from(document.querySelectorAll('[role=dialog]'))"
            "  .filter(d => d.offsetWidth || d.offsetHeight || d.getClientRects().length)"
            "  .flatMap(d => Array.from(d.querySelectorAll('button')))"
            "  .some(b => /^(send(\\s+invite)?|invite)\\b/i.test(b.textContent.trim())"
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


def _remap_partner_email(email: str) -> str:
    """Figma-specific email rewrite. The Playrix Figma org's allowed-domain
    list excludes partner studios, so fluytstudio.net invites must target the
    user's mirror Playrix account: <local>@fluytstudio.net → <local>.ff@playrix.com.
    Other domains pass through unchanged."""
    if "@" not in email:
        return email
    local, _, domain = email.partition("@")
    if domain.lower() == "fluytstudio.net":
        return f"{local}.ff@playrix.com"
    return email


def invite(context, creds: dict, target_user: str, role: str = "full") -> str:
    """Invite `target_user` (email) to the Figma org via Admin → People.

    Applies the partner-domain remap (fluytstudio.net → ff@playrix.com) since
    Figma's org allow-list doesn't include fluytstudio.

    `role` is a seat label hint: "full", "editor", "viewer", or "dev".
    If the org has a single seat type, the role argument is ignored
    (whatever's the default is what gets used).

    Returns one of:
      "invited"                 — Send clicked, dialog closed cleanly.
      "needs-confirmation: ..." — Action partly done but final state unclear.
      "failed: <reason>"        — Stopped before any destructive click.
    """
    target_user = _remap_partner_email(target_user)

    # Validate the seat BEFORE touching the UI: an unknown role used to fall
    # back to "full", silently granting more than the task asked for.
    if _resolve_role(role) is None:
        return (
            f"needs-confirmation: unknown Figma seat {role!r} "
            f"(known: {', '.join(sorted(set(ROLE_ALIASES.values())))})"
        )

    page = get_page(context, "figma")
    page.set_default_timeout(DEFAULT_TIMEOUT)

    nav_err = _ensure_on_people(page, creds)
    if nav_err:
        return nav_err

    # Quick sanity: if the user is already on the People list, there's no
    # reason to invite them again. This re-uses deactivate's search input
    # patterns to look the user up without clicking anything.
    search = page.locator(SEARCH_SEL).first
    if search.count():
        try:
            search.fill("")
            search.fill(target_user)
            time.sleep(1.5)
        except Exception:
            pass
        body = page.locator("body").inner_text().lower()
        already = (
            "no members match" not in body
            and "no results" not in body
            and target_user.lower() in body
        )
        if already:
            return "already-invited"
        # Clear the search so the invite button is unobstructed.
        try:
            search.fill("")
            time.sleep(0.4)
        except Exception:
            pass

    open_err = _open_invite_dialog(page)
    if open_err:
        return open_err

    dialog = page.locator('[role="dialog"]:visible').last

    if not _fill_invite_email(dialog, target_user):
        return "failed: email-field-not-found"

    role_err = _select_invite_role(dialog, role)
    if role_err:
        return role_err

    send_err = _click_send_invite(dialog)
    if send_err:
        return send_err

    # Soft success: dialog dismisses, or a toast confirms.
    try:
        dialog.wait_for(state="detached", timeout=8_000)
        return "invited"
    except Exception:
        try:
            body = page.locator("body").inner_text().lower()
            if "invitation sent" in body or "invite sent" in body or "invited" in body:
                return "invited"
        except Exception:
            pass
        return "needs-confirmation: dialog-still-open"


_REMOVE_BTN_ENABLED_JS = (
    "() => Array.from(document.querySelectorAll('[role=dialog]'))"
    "  .filter(d => d.offsetWidth || d.offsetHeight || d.getClientRects().length)"
    "  .flatMap(d => Array.from(d.querySelectorAll('button')))"
    "  .some(b => /^remove(\\s+user)?\\b/i.test(b.textContent.trim())"
    "    && !b.disabled && b.getAttribute('aria-disabled') !== 'true')"
)


def _wait_remove_enabled(page, timeout: int) -> bool:
    """True once a visible dialog has an enabled 'Remove'/'Remove user' button."""
    try:
        page.wait_for_function(_REMOVE_BTN_ENABLED_JS, timeout=timeout)
        return True
    except Exception:
        return False


def deactivate(context, creds: dict, target_user: str) -> str:
    # Use a Figma-dedicated page on the shared persistent context so
    # screenshots and state aren't disturbed by other services' plugins.
    page = get_page(context, "figma")
    page.set_default_timeout(DEFAULT_TIMEOUT)

    if not getattr(context, SESSION_KEY, False):
        if not _login(page, creds):
            return "failed: login-no-redirect"
        setattr(context, SESSION_KEY, True)
    elif not _on_people_page(page):
        # Session is cached but we're not on People (e.g. Figma session
        # expired, or first call this run). Probe and re-login if needed.
        page.goto("https://www.figma.com/files/recent")
        if is_on_auth_page(page):
            if not _login(page, creds):
                return "failed: login-no-redirect"

    # Defensive cleanup: dismiss any leftover menu / confirmation dialog from
    # the previous task. Without this, Figma's modal backdrop intercepts the
    # next pointer events.
    for _ in range(3):
        try:
            page.keyboard.press("Escape")
        except Exception:
            break
        time.sleep(0.2)

    # If we're already on People (subsequent users in the same group), skip
    # the Admin → People nav and just refill the search box.
    if not _on_people_page(page):
        nav_err = _navigate_to_people(page)
        if nav_err:
            return nav_err

        # First-time-on-People only: wait for the list to populate.
        try:
            page.wait_for_selector(SEARCH_SEL, timeout=8_000)
        except Exception:
            return "failed: people-list-not-rendered"
        try:
            page.wait_for_function(
                "() => /[\\w.+-]+@[\\w-]+\\.[\\w.-]+/.test(document.body.innerText)",
                timeout=8_000,
            )
        except Exception:
            pass

    # Clear any leftover search from the previous user, then type the new one.
    search = page.locator(SEARCH_SEL).first
    if search.count():
        search.fill("")
        search.fill(target_user)
        time.sleep(1.5)  # debounce + filter

    # ---- 6. Locate the row ----
    # First: detect the explicit empty-state message Figma renders.
    body_text = page.locator("body").inner_text().lower()
    if "no members match" in body_text or "no results" in body_text:
        return "user-not-found"

    # Restrict the row search to list-row roles only (NOT generic <div>, which
    # would match the search input's container — a bug we've already hit).
    row = page.locator(
        f'[role="row"]:has-text("{target_user}"), '
        f'tr:has-text("{target_user}"), '
        f'[role="listitem"]:has-text("{target_user}"), '
        f'li:has-text("{target_user}")'
    ).first
    if row.count() == 0:
        return "user-not-found"

    # ---- 7. SAFETY: re-confirm the row contains the target email ----
    row_text = row.inner_text()
    if target_user.lower() not in row_text.lower():
        return "failed: row-mismatch"

    # Highlight the row so the screenshot makes the match obvious.
    try:
        row.scroll_into_view_if_needed()
        row.evaluate("el => el.style.outline = '3px solid #ff3b30'")
    except Exception:
        pass

    # If the operator just wants confirmation that we can find the user,
    # stop here — DO NOT touch the 3-dots menu or anything destructive.
    if os.environ.get("DEACTIVATE_FIND_ONLY"):
        return "found"

    # Dismiss any pop-up Figma survey overlay that might intercept clicks.
    _dismiss_in_app_overlay(page)

    # ---- 8. Open the row's 3-dots menu ----
    kebab = row.locator(
        'button[aria-haspopup], button[aria-label*="More" i], button[aria-label*="options" i], '
        'button[aria-label*="actions" i], button:has-text("…"), button:has-text("...")'
    ).first
    if kebab.count() == 0:
        # Last-resort: hover the row to reveal a hidden button, then take the last button.
        row.hover()
        time.sleep(0.3)
        kebab = row.locator("button").last
    kebab.click()

    # ---- 9. Click "Remove" ----
    remove = page.get_by_role("menuitem", name=re.compile(r"^Remove\b", re.I)).first
    if remove.count() == 0:
        remove = page.locator(
            '[role="menuitem"]:has-text("Remove"), button:has-text("Remove")'
        ).first
    if remove.count() == 0:
        return "failed: remove-not-found"
    # One more overlay-dismiss attempt before the destructive click — surveys
    # can pop up between opening the menu and selecting the action.
    _dismiss_in_app_overlay(page)
    try:
        remove.click(timeout=8_000)
    except Exception:
        # Survey portal likely intercepting; one more dismiss + retry.
        _dismiss_in_app_overlay(page)
        try:
            remove.click(timeout=5_000)
        except Exception as e:
            return f"failed: remove-click ({type(e).__name__})"

    # ---- 10. Confirm dialog ----
    # Figma's current dialog: "Remove <Name> from <Org>?" with a text input
    # asking the operator to type the user's display name to enable the
    # "Remove user" button.
    # Older dialog form (with a checkbox "I understand…") is still handled
    # below as a fallback.
    try:
        dialog = page.locator('[role="dialog"]:visible').last
        dialog.wait_for(timeout=5_000)
    except Exception:
        return "needs-confirmation: no-dialog"

    # The dialog renders its title immediately but loads the body (warning text
    # + 'I understand' checkbox / name input) behind a spinner. Wait for that
    # before reading the form, otherwise we interact with an empty dialog.
    _wait_remove_dialog_ready(dialog)

    # Read the dialog text/title to extract the display name Figma wants typed
    # into the confirmation field. Current dialog text starts with:
    # "Remove <Name> from <Org>?"
    dialog_text = ""
    try:
        dialog_text = dialog.inner_text()
    except Exception:
        pass
    title_text = ""
    for sel in ('h1', 'h2', 'h3', '[role="heading"]'):
        t = dialog.locator(sel).first
        if t.count():
            try:
                title_text = t.inner_text()
                if title_text:
                    break
            except Exception:
                continue
    if not title_text:
        try:
            title_text = next(
                (line.strip() for line in dialog_text.splitlines() if line.strip()),
                "",
            )
        except Exception:
            title_text = ""

    name_match = (
        re.search(r"\bRemove\s+(.+?)\s+from\s+.+?\?", dialog_text, re.I | re.S)
        or re.search(r"\bRemove\s+(.+?)\s+from\s+", title_text, re.I)
    )

    # Two confirmation forms exist. Newer: a text input asking the operator to
    # type the user's name. Current (as of 2026-06): a checkbox "I understand
    # that this action can't be undone". Both dialogs carry a "Remove <Name>
    # from <Org>?" title, so a matching `name_match` is NOT enough to pick the
    # text-input form — gate on the input actually being present.
    name_input = dialog.locator(
        'input[type="text"]:visible, input:not([type]):visible, '
        'input[type="search"]:visible, textarea:visible'
    )
    is_name_form = bool(name_match and name_input.count())

    if is_name_form:
        display_name = re.sub(r"\s+", " ", name_match.group(1)).strip()
        if not _fill_dialog_name_input(dialog, display_name):
            return "needs-confirmation: name-input-fill-failed"
    else:
        # Checkbox form: tick "I understand that this action can't be undone".
        if not _check_understand_box(dialog):
            return "needs-confirmation: understand-checkbox-failed"

    # Wait for the destructive button to become enabled ("Remove user" /
    # "Remove"). On timeout, retry the same confirmation gesture against the
    # currently-visible dialog — guards against Figma leaving a stale hidden
    # dialog in the DOM while the visible one is the real active confirmation.
    if not _wait_remove_enabled(page, 5_000):
        if is_name_form:
            detail = re.sub(r"\s+", " ", name_match.group(1)).strip()
            retried = _fill_dialog_name_input(dialog, detail)
        else:
            detail = "checkbox"
            retried = _check_understand_box(dialog)
        if not (retried and _wait_remove_enabled(page, 3_000)):
            return f"needs-confirmation: remove-button-not-enabled ({detail})"

    confirm = dialog.get_by_role(
        "button", name=re.compile(r"^Remove(\s+user)?\b", re.I)
    ).first
    if confirm.count() == 0:
        confirm = dialog.locator('button:has-text("Remove")').last
    confirm.click()

    # ---- 11. Wait for the row to disappear as a soft success check ----
    try:
        row.wait_for(state="detached", timeout=8_000)
        return "deactivated"
    except Exception:
        return "needs-confirmation: row-still-present"
