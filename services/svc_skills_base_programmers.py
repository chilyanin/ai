"""Skills Base (Programmers) deactivation plugin.

Required env:
    SKILLS_BASE_PROGRAMMERS_URL        users/admin URL
    SKILLS_BASE_PROGRAMMERS_LOGIN      admin email/login
    SKILLS_BASE_PROGRAMMERS_PASSWORD   admin password

The current implementation logs in if needed, opens the configured URL,
searches for the target user, and supports --find-only screenshots. The
destructive remove flow is intentionally blocked until the exact UI path is
confirmed for this service.
"""
from __future__ import annotations

import re
import time
from urllib.parse import urljoin

from services._common import (
    find_user_row,
    get_page,
    is_find_only,
    is_on_auth_page,
    session_attr,
    set_session_attr,
    solve_cloudflare_turnstile,
)

DEFAULT_TIMEOUT = 20_000
SETTLE_TIMEOUT = 5_000
SESSION_KEY = "_skills_base_programmers_signed_in"
SERVICE_SLUG = "skills_base_programmers"
USERS_TAB_RE = re.compile(
    r"^(users?|members?|people|team|employees|пользователи|участники|команда)$",
    re.I,
)


def _click_button_named(page, *labels: str) -> bool:
    for label in labels:
        btn = page.get_by_role("button", name=re.compile(rf"^{label}$", re.I)).first
        if btn.count():
            try:
                btn.click()
                return True
            except Exception:
                continue

    # Some login pages use anchors/divs styled as buttons.
    for label in labels:
        item = page.locator(
            f'a:has-text("{label}"), [role="button"]:has-text("{label}")'
        ).first
        if item.count():
            try:
                item.click()
                return True
            except Exception:
                continue
    return False


def _fill_first_visible(page, selectors: str, value: str) -> bool:
    inputs = page.locator(selectors)
    try:
        count = inputs.count()
    except Exception:
        return False
    for i in range(count):
        field = inputs.nth(i)
        try:
            if field.is_visible():
                field.fill(value)
                return True
        except Exception:
            continue
    return False


def _is_skills_base_login_landing(page) -> bool:
    try:
        text = page.locator("body").inner_text(timeout=2_000).lower()
    except Exception:
        return False
    return (
        "log in with a skills base account" in text
        or "log in with single sign-on" in text
    )


def _is_mfa_setup(page) -> bool:
    try:
        text = page.locator("body").inner_text(timeout=2_000).lower()
    except Exception:
        return False
    return (
        "multi-factor authentication setup" in text
        or "/account/twofactor-setup" in page.url.lower()
    )


def _is_personal_summary(page) -> bool:
    if "/people/view" in page.url.lower():
        return True
    try:
        text = page.locator("body").inner_text(timeout=2_000).lower()
    except Exception:
        return False
    return "my summary" in text and "administration" in text


def _handle_mfa_setup(page) -> bool:
    if not _is_mfa_setup(page):
        return True

    checkbox = page.locator(
        'label:has-text("Don\'t ask me for 30 days") input[type="checkbox"], '
        'input[type="checkbox"]'
    ).first
    if checkbox.count():
        try:
            checkbox.check()
        except Exception:
            try:
                checkbox.click()
            except Exception:
                pass

    if not _click_button_named(page, "Skip"):
        return False

    try:
        page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT)
    except Exception:
        pass
    time.sleep(0.5)
    return not _is_mfa_setup(page)


def _maybe_solve_captcha(page) -> None:
    try:
        body = page.locator("body").inner_text(timeout=2_000).lower()
    except Exception:
        body = ""
    try:
        has_challenge = (
            page.locator(
                'iframe[src*="challenges.cloudflare.com"], '
                'iframe[src*="turnstile"], '
                '[data-sitekey]'
            ).count()
            > 0
        )
    except Exception:
        has_challenge = False

    if not has_challenge and not any(
        marker in body for marker in ("captcha", "turnstile", "verify you are human")
    ):
        return

    ok, detail = solve_cloudflare_turnstile(page)
    print(f"    captcha: {detail}")
    if ok:
        try:
            page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT)
        except Exception:
            pass


def _has_user_list_surface(page, target_user: str) -> bool:
    if _is_skills_base_login_landing(page) or _is_mfa_setup(page) or _is_personal_summary(page):
        return False
    try:
        found = page.evaluate(
            """(target) => {
                const text = document.body.innerText.toLowerCase();
                if (text.includes(target.toLowerCase())) return true;
                const url = location.href.toLowerCase();
                if (/(admin|administration|settings|account).*(user|member|people)/.test(url)) {
                    return true;
                }
                const hasUserWords = /(users|members|people)/.test(text);
                const hasAdminWords = /(administration|admin|settings)/.test(text);
                const hasListShape = Boolean(document.querySelector('[role="row"], table'));
                return hasUserWords && hasAdminWords && hasListShape;
            }""",
            target_user,
        )
        return bool(found)
    except Exception:
        return False


def _click_menu_item(page, label_re: re.Pattern) -> bool:
    for role in ("button", "link", "menuitem", "tab"):
        item = page.get_by_role(role, name=label_re).first
        try:
            if item.count():
                item.click()
                return True
        except Exception:
            continue

    item = page.locator(
        'a, button, [role="button"], [role="menuitem"], [role="treeitem"]'
    ).filter(has_text=label_re).first
    try:
        if item.count():
            item.click()
            return True
    except Exception:
        pass
    return False


def _try_administration_menu(page, target_user: str) -> bool:
    if _click_menu_item(page, re.compile(r"^administration$", re.I)):
        try:
            page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT)
        except Exception:
            pass
        time.sleep(0.5)

    if _click_menu_item(page, USERS_TAB_RE):
        try:
            page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT)
        except Exception:
            pass
        time.sleep(0.8)
        return _has_user_list_surface(page, target_user)
    return False


def _try_direct_user_urls(page, target_user: str) -> bool:
    candidates = (
        "/administration/users",
        "/administration/people",
        "/administration/members",
        "/admin/users",
        "/admin/people",
        "/settings/users",
        "/account/users",
        "/people",
        "/people/list",
    )
    for path in candidates:
        try:
            page.goto(urljoin(page.url, path))
            page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT)
        except Exception:
            pass
        time.sleep(0.5)
        if _has_user_list_surface(page, target_user):
            return True
    return False


def _instance_problem(page) -> str | None:
    """Detect a dead/invalid org shortcut. Returns a status string or None.

    Skills Base orgs are reached via /o/<slug>. A wrong/deleted slug shows a
    distinctive message instead of a login — surface that plainly rather than
    failing later with a vague 'list-not-rendered'.
    """
    try:
        text = page.locator("body").inner_text(timeout=2_000).lower()
    except Exception:
        return None
    if "this skills base instance has been deleted" in text:
        return "failed: skills-base-instance-deleted"
    if "invalid shortcut link" in text:
        return "failed: skills-base-invalid-shortcut"
    if "the page you requested was not found" in text or text.strip() == "not found":
        return "failed: skills-base-org-not-found"
    return None


def _open_users_area(page, creds: dict, target_user: str) -> str:
    page.goto(creds["url"])
    try:
        page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT)
    except Exception:
        pass
    _maybe_solve_captcha(page)
    problem = _instance_problem(page)
    if problem:
        return problem
    if _has_user_list_surface(page, target_user):
        return "ok"

    if _try_administration_menu(page, target_user):
        return "ok"

    for locator in (
        page.get_by_role("tab", name=USERS_TAB_RE).first,
        page.get_by_role("link", name=USERS_TAB_RE).first,
        page.get_by_role("button", name=USERS_TAB_RE).first,
        page.locator(
            'a[href*="user" i], a[href*="member" i], a[href*="people" i], '
            'button:has-text("Users"), button:has-text("Members"), '
            'button:has-text("People"), [role="menuitem"]:has-text("Users"), '
            '[role="menuitem"]:has-text("Members"), [role="menuitem"]:has-text("People")'
        ).first,
    ):
        try:
            if locator.count() == 0:
                continue
            locator.click()
            page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT)
            time.sleep(0.5)
            if _has_user_list_surface(page, target_user):
                return "ok"
        except Exception:
            continue

    if _try_direct_user_urls(page, target_user):
        return "ok"

    return _browser_use_users_tab_fallback(page)


def _browser_use_users_tab_fallback(page) -> str:
    """Last-resort hook requested by the operator.

    browser_use is intentionally imported lazily because this project still
    runs primarily through Playwright. In this environment the installed
    browser_use package may require an LLM/provider setup before it can drive a
    browser, so this function reports a clear status instead of breaking the
    whole deactivation run.
    """
    try:
        import browser_use  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return f"needs-confirmation: users-tab-not-found-browser-use-unavailable ({type(e).__name__})"
    return "needs-confirmation: users-tab-not-found-browser-use-needed"


def _login(page, creds: dict) -> bool:
    page.goto(creds["url"])
    try:
        page.wait_for_load_state("domcontentloaded", timeout=SETTLE_TIMEOUT)
    except Exception:
        pass
    _maybe_solve_captcha(page)

    if _is_skills_base_login_landing(page):
        if not _click_button_named(page, "Log in with a Skills Base account"):
            return False
        try:
            page.wait_for_load_state("domcontentloaded", timeout=SETTLE_TIMEOUT)
        except Exception:
            pass
        _maybe_solve_captcha(page)

    if not is_on_auth_page(page) and not _is_skills_base_login_landing(page):
        return _handle_mfa_setup(page)

    login_filled = _fill_first_visible(
        page,
        (
            'input[type="email"], input[name*="email" i], '
            'input[name*="login" i], input[name*="user" i], '
            'input[autocomplete="username"], input[type="text"]'
        ),
        creds["login"],
    )
    if not login_filled:
        return False

    password_filled = _fill_first_visible(
        page,
        'input[type="password"], input[name*="password" i], input[autocomplete="current-password"]',
        creds["password"],
    )
    if not password_filled:
        if not _click_button_named(page, "Continue", "Next", "Sign in", "Log in", "Login"):
            page.keyboard.press("Enter")
        try:
            page.locator('input[type="password"]').first.wait_for(timeout=12_000)
        except Exception:
            return False
        password_filled = _fill_first_visible(
            page,
            'input[type="password"], input[name*="password" i], input[autocomplete="current-password"]',
            creds["password"],
        )
        if not password_filled:
            return False

    if not _click_button_named(page, "Sign in", "Log in", "Login", "Continue"):
        page.keyboard.press("Enter")

    _maybe_solve_captcha(page)

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

    try:
        page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT)
    except Exception:
        pass
    _maybe_solve_captcha(page)
    return _handle_mfa_setup(page)


def run(context, creds: dict, target_user: str, *, session_key: str, slug: str) -> str:
    """Shared Skills Base flow, parameterized by per-org session key + page slug.

    Both the "playrix" and "playrixprogrammers" plugins call this so the
    login / captcha / users-tab / find-user logic lives in one place.
    """
    page = get_page(context, slug)
    page.set_default_timeout(DEFAULT_TIMEOUT)

    if not session_attr(context, session_key):
        if not _login(page, creds):
            return "failed: login"
        set_session_attr(context, session_key, True)

    users_status = _open_users_area(page, creds, target_user)
    if users_status != "ok" and _handle_mfa_setup(page):
        users_status = _open_users_area(page, creds, target_user)
    if users_status != "ok":
        return users_status

    # A few SPAs render the user list just after networkidle.
    time.sleep(0.5)

    status = find_user_row(
        page,
        target_user,
        extra_row_selectors=(
            '[class*="user" i]',
            '[class*="member" i]',
            '[class*="person" i]',
            '[data-testid*="user" i]',
            '[data-testid*="member" i]',
        ),
    )
    if is_find_only():
        return status
    if status != "found":
        return status

    return "needs-confirmation: skills-base-deactivate-flow-not-implemented"


def deactivate(context, creds: dict, target_user: str) -> str:
    return run(context, creds, target_user, session_key=SESSION_KEY, slug=SERVICE_SLUG)
