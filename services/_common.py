"""Shared helpers for service-deactivation plugins."""
from __future__ import annotations

import os
import re
import time

from totp import generate as totp_generate, seconds_remaining

OTP_INPUT_SELECTORS = (
    'input[autocomplete="one-time-code"], '
    'input[name="code"], '
    'input[name="totp"], '
    'input[name="otp"], '
    'input[name="verificationCode"], '
    'input[id*="otp" i], '
    'input[id*="totp" i], '
    'input[id*="code" i]'
)

OTP_SUBMIT_NAME_RE = re.compile(
    r"^(Verify|Submit|Continue|Next|Sign\s*in|Log\s*in)$", re.I
)


def submit_totp(page, secret_env: str, *, retries: int = 1) -> bool:
    """Locate a one-time-code input on `page`, fill it from $secret_env, submit.

    Handles two layouts:
      * Single input that takes the whole 6-digit code (`fill()`)
      * Six separate single-character boxes (focus first, then keyboard.type
        — the browser auto-advances between them).

    Waits for ≥8s remaining in the TOTP window before filling so the code
    doesn't expire mid-flight. On a "wrong code" / "invalid" response, waits
    for the next window and retries up to `retries` times.

    Returns True if any submission was performed (success or not). False if
    no OTP input was visible (e.g. service skipped 2FA for this session).
    """
    secret = os.environ.get(secret_env)
    if not secret:
        return False

    inputs = page.locator(OTP_INPUT_SELECTORS)
    try:
        inputs.first.wait_for(timeout=10_000)
    except Exception:
        return False

    box_count = inputs.count()
    multi_box = box_count > 1

    attempts = retries + 1
    for attempt in range(attempts):
        while seconds_remaining() < 8:
            time.sleep(1)
        code = totp_generate(secret)

        if multi_box:
            # Six-boxes: focus the first input, clear via select-all, then type.
            first = inputs.first
            try:
                first.click()
                first.press("ControlOrMeta+a")
                first.press("Delete")
            except Exception:
                pass
            page.keyboard.type(code, delay=30)
        else:
            single = inputs.first
            single.fill("")
            single.fill(code)

        btn = page.get_by_role("button", name=OTP_SUBMIT_NAME_RE).first
        if btn.count():
            btn.click()
        else:
            inputs.first.press("Enter")

        # Brief wait, then check for an error message.
        time.sleep(2.5)
        try:
            body = page.locator("body").inner_text().lower()
        except Exception:
            body = ""
        if not any(s in body for s in ("wrong code", "invalid code", "incorrect", "try again")):
            return True
        if attempt < attempts - 1:
            time.sleep(seconds_remaining() + 2)
    return True  # last attempt was made, even if we still see an error


def session_attr(context, key: str, default=None):
    """Persistent per-context state for plugins (used to skip re-login)."""
    return getattr(context, key, default)


def set_session_attr(context, key: str, value) -> None:
    setattr(context, key, value)


def is_find_only() -> bool:
    return bool(os.environ.get("DEACTIVATE_FIND_ONLY"))


PAGE_ATTR_PREFIX = "_page_"


def get_page(context, service_slug: str):
    """Return a long-lived page bound to this plugin on the shared context.

    Each plugin stores its dedicated page on the context, so that running a
    different service afterwards doesn't disturb the state we need to
    screenshot.
    """
    attr = f"{PAGE_ATTR_PREFIX}{service_slug}"
    page = getattr(context, attr, None)
    if page is None or page.is_closed():
        page = context.new_page()
        setattr(context, attr, page)
    return page


# URL substrings that indicate we're on an auth/sign-in page rather than
# a logged-in app surface. Hyphen and underscore variants matter — we hit
# a Maxon bug where "/sign-in" wasn't detected because we only looked for
# "signin".
AUTH_URL_MARKERS = (
    "login",
    "log-in",
    "log_in",
    "signin",
    "sign-in",
    "sign_in",
    "/auth/",
    "/oauth",
    "/sso/",
    "authorize",
)


NO_RESULTS_PHRASES = (
    # English
    "no members match",
    "no users match",
    "no results",
    "no matches",
    "no users found",
    "nothing found",
    # Russian
    "ничего не найдено",
    "нет результатов",
    "не найдено",
    "пользователи не найдены",
)

SEARCH_INPUT_SEL = (
    'input[type="search"], '
    'input[placeholder*="search" i], '
    'input[placeholder*="Search" i], '
    'input[placeholder*="Поиск" i], '
    'input[placeholder*="поиск" i], '
    'input[aria-label*="search" i], '
    'input[aria-label*="filter" i]'
)


def find_user_row(
    page,
    target_user: str,
    *,
    settle_timeout: float = 20.0,
    extra_row_selectors: tuple[str, ...] = (),
) -> str:
    """After landing on a user-list page, search for `target_user`.

    `extra_row_selectors` adds service-specific CSS selectors for the row
    container (e.g. `("div.user-row",)` for Plastic SCM). They are appended
    to the default set ([role="row"], tr, [role="listitem"], li).

    Returns:
      "found"           — a list row containing the user is visible (and outlined
                          in red so the screenshot is unambiguous)
      "user-not-found"  — page rendered an explicit empty state, or no row
                          matched after searching
      "failed: list-not-rendered" — gave up waiting for a search input or any
                          email-shaped text to appear
    """
    import time as _time

    # Wait for the list page to actually render.
    try:
        page.wait_for_function(
            "(sel) => document.querySelector(sel) || "
            "/[\\w.+-]+@[\\w-]+\\.[\\w.-]+/.test(document.body.innerText)",
            arg=SEARCH_INPUT_SEL,
            timeout=int(settle_timeout * 1000),
        )
    except Exception:
        return "failed: list-not-rendered"

    search = page.locator(SEARCH_INPUT_SEL).first
    if search.count():
        try:
            search.fill("")
            search.fill(target_user)
            _time.sleep(1.5)  # debounce + filter
        except Exception:
            pass

    # Empty-state messages take priority — they're authoritative.
    try:
        body_text = page.locator("body").inner_text().lower()
    except Exception:
        body_text = ""
    if any(p in body_text for p in NO_RESULTS_PHRASES):
        return "user-not-found"

    # Restrict matches to actual list-row roles plus any extra service-specific
    # selectors. We deliberately don't match bare <div> — that bites the
    # search-box container.
    selectors = [
        f'[role="row"]:has-text("{target_user}")',
        f'tr:has-text("{target_user}")',
        f'[role="listitem"]:has-text("{target_user}")',
        f'li:has-text("{target_user}")',
    ]
    for extra in extra_row_selectors:
        selectors.append(f'{extra}:has-text("{target_user}")')
    row = page.locator(", ".join(selectors)).first
    if row.count() == 0:
        return "user-not-found"

    # Verify the row really contains the target (defends against the search-box
    # container leaking into the match).
    try:
        if target_user.lower() not in row.inner_text().lower():
            return "user-not-found"
        row.scroll_into_view_if_needed()
        row.evaluate("el => el.style.outline = '3px solid #ff3b30'")
    except Exception:
        pass
    return "found"


def is_on_auth_page(page) -> bool:
    """True if the current page looks like a login/sign-in/oauth page.

    Combines a URL check with a visibility check for password inputs — a
    password field on screen is a strong signal we're not yet authenticated.
    """
    url = page.url.lower()
    if any(m in url for m in AUTH_URL_MARKERS):
        return True
    try:
        if page.locator('input[type="password"]:visible').count() > 0:
            return True
    except Exception:
        pass
    return False
