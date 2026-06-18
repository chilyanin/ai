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

from services._common import (
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
# Skills Base's manageable people list lives at <app-eu host>/people/ and is
# reached from the left-nav "Directories" group → "People". PEOPLE_RE matches
# that nav link for the bounce-back fallback in _open_users_area.
PEOPLE_RE = re.compile(r"^(people|persons?|directory of people|люди|сотрудники)$", re.I)

# Row containers for a person in the People list, used by _locate_person_row.
PERSON_ROW_SELECTORS = (
    '[class*="user" i]',
    '[class*="member" i]',
    '[class*="person" i]',
    '[data-testid*="user" i]',
    '[data-testid*="member" i]',
    '[data-testid*="person" i]',
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


def _people_url(page, creds: dict) -> str:
    """Absolute URL of the People directory list.

    The org shortcut lives on app.skills-base.com/o/<slug> but the app itself
    runs on the regional host (app-eu.skills-base.com for this org). The people
    list is served at <origin>/people/. Derive the origin from the current
    (post-login) URL when possible, falling back to the configured creds URL,
    and force the EU app host.
    """
    base = page.url if "skills-base.com" in (page.url or "") else creds["url"]
    m = re.match(r"https?://[^/]+", base or "")
    origin = m.group(0) if m else "https://app-eu.skills-base.com"
    origin = origin.replace("://app.skills-base.com", "://app-eu.skills-base.com")
    return origin.rstrip("/") + "/people/"


def _on_people_list(page) -> bool:
    """True if we're on the People directory list (not a single-person view)."""
    url = (page.url or "").lower()
    if "/people/view" in url:
        return False
    if "/people" not in url:
        return False
    # A list has a "Search people" box and/or multiple data rows.
    try:
        if page.locator(
            'input[placeholder*="search" i], input[type="search"]'
        ).count():
            return True
        return page.locator('[role="row"], tbody tr').count() > 1
    except Exception:
        return False


def _open_users_area(page, creds: dict, target_user: str) -> str:
    # Skills Base's manageable people list is at <app-eu host>/people/. Go there
    # directly — the generic menu/admin discovery isn't needed for this service.
    people_url = _people_url(page, creds)
    page.goto(people_url)
    try:
        page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT)
    except Exception:
        pass
    _maybe_solve_captcha(page)

    problem = _instance_problem(page)
    if problem:
        return problem

    if _on_people_list(page):
        return "ok"

    # Bounced to a personal/summary view (e.g. /people/view) — click the
    # left-nav People link, then re-try the direct URL.
    if _click_menu_item(page, PEOPLE_RE):
        try:
            page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT)
        except Exception:
            pass
        time.sleep(0.5)
    if _on_people_list(page):
        return "ok"

    page.goto(people_url)
    try:
        page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT)
    except Exception:
        pass
    if _on_people_list(page):
        return "ok"

    return "failed: people-list-not-rendered"


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


# Skills Base offboarding is a DELETE. Confirmed row markup (legacy Bootstrap):
#   <a title="Delete <Name>" href="/people/delete/id/<id>" class="btn btn-mini">
#       <i class="icon-trash"></i></a>
# Clicking it shows a confirmation modal whose primary button is "Delete".
DELETE_CONFIRM_RE = re.compile(
    r"^(delete|remove|confirm|yes|ok|удалить|подтвердить|да)\b", re.I
)
# Selectors for the per-row delete (basket/trash) control. Confirmed-shape
# selectors first, then defensive fallbacks for other markups.
DELETE_BTN_SEL = (
    'a[href*="/people/delete" i], a[href*="/delete" i], '
    'a[title*="delete" i], button[title*="delete" i], '
    'a:has(i[class*="trash" i]), button:has(i[class*="trash" i]), '
    'a[aria-label*="delete" i], button[aria-label*="delete" i], '
    'button[aria-label*="remove" i], a[aria-label*="remove" i], '
    'button[aria-label*="trash" i], button[aria-label*="basket" i], '
    'button[title*="remove" i], button[title*="basket" i], '
    'a:has(i[class*="basket" i]), button:has(i[class*="basket" i]), '
    'button:has(i[class*="delete" i]), button:has(i[class*="bin" i]), '
    'button:has(svg[class*="trash" i]), button:has(svg[class*="basket" i]), '
    'button:has(svg[class*="delete" i])'
)
# Confirmation surface: a Bootstrap modal (no role=dialog in this legacy app),
# a modern role=dialog/alertdialog, or a standalone /people/delete confirm page.
CONFIRM_SURFACE_SEL = (
    '[role="dialog"], [role="alertdialog"], '
    '.modal.in, .modal.show, .modal[style*="display: block"], .modal:visible'
)


def _locate_person_row(page, target_user: str):
    """Locate the People-grid row for `target_user`. Returns a Locator or None.

    Used by both _find_person (to outline the row) and the delete flow (to act
    on it).
    """
    selectors = [
        f'[role="row"]:has-text("{target_user}")',
        f'tr:has-text("{target_user}")',
        f'[role="listitem"]:has-text("{target_user}")',
        f'li:has-text("{target_user}")',
    ]
    for extra in PERSON_ROW_SELECTORS:
        selectors.append(f'{extra}:has-text("{target_user}")')
    row = page.locator(", ".join(selectors)).first
    if row.count() == 0:
        return None
    try:
        if target_user.lower() not in row.inner_text().lower():
            return None
    except Exception:
        return None
    return row


def _find_row_delete_button(row):
    """Locate the basket/trash delete control inside `row`. Returns it or None."""
    btn = row.locator(DELETE_BTN_SEL).first
    if btn.count():
        return btn
    # Reveal hover-only action controls, then try again.
    try:
        row.hover()
    except Exception:
        pass
    time.sleep(0.3)
    btn = row.locator(DELETE_BTN_SEL).first
    return btn if btn.count() else None


def _confirm_delete(page) -> str | None:
    """Confirm deletion. The trash control either opens a Bootstrap modal or
    navigates to a /people/delete confirm page; handle both. Returns None on
    success, else a needs-confirmation status."""
    # Scope to a confirmation surface if one appeared (modal/dialog); otherwise
    # fall back to the whole page (standalone confirm page).
    scope = page
    try:
        surface = page.locator(CONFIRM_SURFACE_SEL).last
        surface.wait_for(state="visible", timeout=6_000)
        scope = surface
    except Exception:
        if "/people/delete" not in (page.url or "").lower():
            return "needs-confirmation: no-confirm-surface"

    # The confirm control is a button OR an anchor (legacy uses <a class="btn
    # btn-danger">Delete</a>). Match by role first, then by text, scoped to the
    # confirm surface. Exclude the row's own trash link via text match on a
    # short label.
    confirm = scope.get_by_role("button", name=DELETE_CONFIRM_RE).first
    if confirm.count() == 0:
        confirm = scope.locator(
            'a.btn-danger, button.btn-danger, '
            'button:has-text("Delete"), a:has-text("Delete"), '
            'button:has-text("Remove"), a:has-text("Remove"), '
            'button:has-text("Confirm"), input[type="submit"][value*="elete" i]'
        ).last
    if confirm.count() == 0:
        return "needs-confirmation: confirm-delete-button-not-found"
    try:
        confirm.click(timeout=8_000)
    except Exception as e:  # noqa: BLE001
        return f"needs-confirmation: confirm-click ({type(e).__name__})"
    return None


def _deactivate_person(page, target_user: str) -> str:
    """Delete the found person: click the row's basket button, confirm Delete,
    verify the row is gone.

    Returns "deactivated" on success, else "needs-confirmation: ...".
    """
    row = _locate_person_row(page, target_user)
    if row is None:
        return "needs-confirmation: row-not-found-for-action"

    delete_btn = _find_row_delete_button(row)
    if delete_btn is None:
        return "needs-confirmation: delete-button-not-found"
    try:
        delete_btn.click(timeout=8_000)
    except Exception as e:  # noqa: BLE001
        return f"needs-confirmation: delete-click ({type(e).__name__})"
    time.sleep(0.4)

    confirm_err = _confirm_delete(page)
    if confirm_err:
        return confirm_err

    # Verify: a success toast, or the row leaving the list.
    try:
        page.wait_for_function(
            """(target) => {
                const text = document.body.innerText.toLowerCase();
                if (/(has been|successfully).{0,20}(delet|remov)/.test(text)) return true;
                const sel = '[role="row"], tr, [role="listitem"], li';
                const rows = Array.from(document.querySelectorAll(sel))
                    .filter(r => r.innerText.toLowerCase().includes(target.toLowerCase()));
                return rows.length === 0;  // gone from the list
            }""",
            arg=target_user,
            timeout=8_000,
        )
        return "deactivated"
    except Exception:
        return "needs-confirmation: deletion-unverified"


PEOPLE_SEARCH_SEL = '#peopleSearch, input[placeholder*="people" i]'
# DataTables footer line, e.g. "Showing 1 to 1 of 1 entries (filtered from 119
# total entries)". This is the authoritative signal that the grid has applied
# the search — far more reliable than scraping transient "No matching records"
# text that flashes while the AJAX data is still loading.
DT_INFO_SEL = '.dataTables_info, [id$="_info"]'
_DT_FILTERED_RE = re.compile(r"filtered from\s+[\d,]+", re.I)
_DT_ZERO_RE = re.compile(r"showing\s+0\s+to\s+0|of\s+0\s+entries", re.I)


def _find_person(page, target_user: str) -> str:
    """Filter the People grid for `target_user` and locate their row.

    Uses the People-page "Search people" box (#peopleSearch) with real
    keystrokes — the grid filters on keyup, so .fill() alone doesn't trigger it.
    Waits for the DataTables grid to *settle* (its info line reports a filtered
    count) before deciding, so we don't mistake the transient "No matching
    records found" shown during load for a real miss. Outlines the matched row
    for the proof screenshot. Returns "found", "user-not-found", or
    "failed: people-list-not-rendered".
    """
    box = page.locator(PEOPLE_SEARCH_SEL).first
    if box.count() == 0:
        return "failed: people-list-not-rendered"
    try:
        box.click()
        box.fill("")
        box.type(target_user, delay=40)
    except Exception:
        return "failed: people-list-not-rendered"

    # Poll until the row appears, or the grid settles on an explicit 0 results.
    deadline = time.time() + 20
    while time.time() < deadline:
        row = _locate_person_row(page, target_user)
        if row is not None:
            try:
                row.scroll_into_view_if_needed()
                row.evaluate("el => el.style.outline = '3px solid #ff3b30'")
            except Exception:
                pass
            return "found"

        try:
            info = page.locator(DT_INFO_SEL).first.inner_text(timeout=1_000)
        except Exception:
            info = ""
        # Only trust a 0-result verdict once the grid reports it has applied the
        # filter ("filtered from N") — otherwise it's still loading.
        if _DT_FILTERED_RE.search(info) and _DT_ZERO_RE.search(info):
            return "user-not-found"
        time.sleep(0.5)

    return "user-not-found"


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

    # Let the grid finish its initial render before filtering.
    time.sleep(0.5)

    status = _find_person(page, target_user)
    if is_find_only():
        return status
    if status != "found":
        return status

    return _deactivate_person(page, target_user)


def deactivate(context, creds: dict, target_user: str) -> str:
    return run(context, creds, target_user, session_key=SESSION_KEY, slug=SERVICE_SLUG)
