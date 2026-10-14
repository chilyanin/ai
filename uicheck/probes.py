"""Per-service probes: walk the admin UI the way the plugin does, check every
selector, stop before anything destructive.

Each probe gets a ProbeContext (uicheck/model.py) whose page lives in the
service's own persistent profile - the same one deactivate.py / grant.py use,
so a nightly pass also keeps those sessions warm. Probes reuse the plugin's
own helpers (`_login`, `_navigate_to_people`, `find_user_row`, ...) so a green
run means the plugin itself would get that far.

Depth (operator decision, 2026-10-13): open the row's actions menu, confirm
the Remove/Deactivate item is there, press Escape. Confirmation dialogs are
never opened; invite dialogs are opened, inspected and cancelled with nothing
typed.

Canary (operator decision, 2026-10-14): the person named in today's Asana
task for that service. A "Удалить из" task gives a member -> full row/menu
path (`ctx.has_member`). A "Доступ к" task gives someone not in the service
yet -> only login / list / search / invite steps; the row steps are recorded
as skipped via `ctx.require_member(...)`. No task today -> the service is not
probed at all (decided in uicheck.py, the probe never runs).
"""
from __future__ import annotations

import re
import time

from services._common import find_user_row, is_on_auth_page
from uicheck.model import ProbeAbort, ProbeContext

CANARY_GOAL = "a members-list row container (one element per person) that contains the member's email"


def _login_form_checks(ctx: ProbeContext, *, email_key="login_email", password_key="login_password", submit_key: str | None = None) -> bool:
    """If a sign-in form is on screen, verify its selectors. Returns True if a
    form was found (so the login helper will exercise it)."""
    page = ctx.page
    if not is_on_auth_page(page):
        ctx.skip("login_form", "session still valid - sign-in form not shown, login selectors not exercised", key=email_key)
        return False
    ctx.check(email_key, "the email / username input of the sign-in form", hints=("email", "username", "login"), abort=False)
    pw = ctx.check(password_key, "the password input of the sign-in form", hints=("password",), abort=False, timeout_s=3.0)
    if pw is None:
        ctx.skip("login_password_note", "password field not on this screen yet (staged login) - checked by the plugin's login helper", key=password_key)
    if submit_key:
        ctx.check(submit_key, "the sign-in / submit button of the form", hints=("sign in", "log in", "continue", "submit"), abort=False)
    return True


def _open_menu_and_check(ctx: ProbeContext, kebab, *, item_key: str, item_goal: str, hints, alt=None) -> None:
    """Click the row menu button, check the destructive item is listed, Escape."""
    try:
        kebab.scroll_into_view_if_needed()
        kebab.click(timeout=5_000)
    except Exception as e:  # noqa: BLE001
        ctx.state("menu_open", False, "row actions menu opens on click", detail=f"click failed: {type(e).__name__}", abort=False)
        return
    time.sleep(0.6)
    try:
        ctx.check(item_key, item_goal, hints=hints, alt=alt, abort=False, timeout_s=4.0)
    finally:
        ctx.escape(2)


# ---------------------------------------------------------------------------
# Figma
# ---------------------------------------------------------------------------
def probe_figma(ctx: ProbeContext) -> None:
    import services.svc_figma as F

    ctx.plugin_modules.append(F)
    page = ctx.page
    page.set_default_timeout(F.DEFAULT_TIMEOUT)

    page.goto(ctx.creds["url"])
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5_000)
    except Exception:
        pass
    _login_form_checks(ctx, submit_key="login_submit")
    ctx.state("login", F._login(page, ctx.creds), "Figma sign-in lands on files/admin")
    ctx.escape(2)

    if not F._on_people_page(page):
        nav_err = F._navigate_to_people(page)
        if nav_err:
            ctx.nav(
                "people_page",
                "the Figma Admin → People page: the organisation's member list with a search box",
                reached=lambda: F._on_people_page(page),
                key="people_url",
            )
    ctx.state("people_page", F._on_people_page(page), "Admin → People list is open (URL has /people or /members and a search box)")

    search = ctx.check("search", "the search/filter input above the People member list", hints=("search", "filter"))
    try:
        search.fill("")
        search.fill(ctx.canary)
        time.sleep(1.5)
    except Exception:
        pass
    if ctx.require_member("row", "kebab", "remove_item"):
        row = ctx.check("row", CANARY_GOAL, must_contain=ctx.canary, hints=("row",))
        kebab = ctx.check("kebab", "the row's '⋯' / more-actions menu button", scope=row, hints=("more", "options", "actions", "…"))
        F._dismiss_in_app_overlay(page)
        _open_menu_and_check(
            ctx, kebab,
            item_key="remove_item",
            item_goal="the 'Remove' item in the opened row actions menu (presence only)",
            hints=("remove",),
            alt=lambda: page.get_by_role("menuitem", name=re.compile(r"^Remove\b", re.I)),
        )

    # Invite flow (grant.py): open the dialog, inspect, cancel.
    try:
        search.fill("")
        time.sleep(0.4)
    except Exception:
        pass
    F._dismiss_in_app_overlay(page)
    invite_btn = ctx.check(
        "invite_button", "the 'Invite' button on the People page that opens the invite dialog",
        hints=("invite",), abort=False,
        alt=lambda: page.get_by_role("button", name=re.compile(r"^Invite\b", re.I)),
    )
    if invite_btn is None:
        ctx.skip("invite_dialog", "invite button missing - dialog checks skipped")
        return
    try:
        invite_btn.click(timeout=8_000)
    except Exception as e:  # noqa: BLE001
        ctx.state("invite_dialog", False, "invite dialog opens", detail=f"click failed: {type(e).__name__}", abort=False)
        return
    dialog = ctx.check("dialog", "the open invite dialog (modal)", hints=("dialog", "invite"), abort=False, timeout_s=8.0)
    if dialog is not None:
        dlg = dialog.locator("xpath=.").last
        ctx.check("invite_email", "the email input inside the invite dialog", scope=dlg, hints=("email",), abort=False)
        ctx.check("send_button", "the 'Send invite' button inside the invite dialog (presence only, may be disabled)", scope=dlg, hints=("send", "invite"), abort=False)
    ctx.escape(3)


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------
def probe_slack(ctx: ProbeContext) -> None:
    import services.svc_slack_workspace_redbark2 as S

    ctx.plugin_modules.append(S)
    page = ctx.page
    page.set_default_timeout(S.DEFAULT_TIMEOUT)
    url = ctx.creds.get("url") or S.DEFAULT_URL

    ctx.escape(2)
    ok = S._ensure_members_table(page, url)
    if not ok:
        if ctx.creds.get("login") and ctx.creds.get("password"):
            _login_form_checks(ctx, submit_key="login_submit")
            ctx.state("login", S._login(page, ctx.creds), "Slack no-SSO sign-in (email+password+TOTP) reaches the admin members table")
            ok = S._ensure_members_table(page, url)
        else:
            ctx.skip("login_form", "no LOGIN/PASSWORD for Slack - session-only", key="login_email")
    ctx.state("members_table", ok, "admin → People members table rendered")
    ctx.check("people_tab", "the 'People' tab/link of the admin area", hints=("people",), abort=False, timeout_s=2.0)

    search = ctx.check("search", "the search input of the members table header", hints=("search", "filter"))
    try:
        search.fill("")
        search.fill(ctx.canary)
        time.sleep(1.5)
    except Exception:
        pass
    if ctx.require_member("row", "actions_button", "deactivate_item"):
        ctx.check("row", CANARY_GOAL, must_contain=ctx.canary, hints=("row", "member"))
        actions = ctx.check("actions_button", "the per-row 'Actions' (⋯) button of the filtered member row", hints=("actions", "more"))
        _open_menu_and_check(
            ctx, actions,
            item_key="deactivate_item",
            item_goal="the 'Deactivate account' item in the opened actions menu (presence only)",
            hints=("deactivate",),
        )
    ctx.skip("activate_item", "only shown for already-deactivated members - not probed", key="activate_item")
    ctx.skip("confirm_button", "confirmation dialog is never opened by the nightly check", key="confirm_button")


# ---------------------------------------------------------------------------
# Unity (one probe covers every Unity alias - same selectors)
# ---------------------------------------------------------------------------
def probe_unity(ctx: ProbeContext) -> None:
    import services.svc_unity as U
    from services._selectors import split_top_level

    ctx.plugin_modules.append(U)
    page = ctx.page
    page.set_default_timeout(U.DEFAULT_TIMEOUT)
    ctx.escape(2)

    page.goto(ctx.creds["url"])
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5_000)
    except Exception:
        pass
    _login_form_checks(ctx)
    ctx.state("login", U._login(page, ctx.creds), "Unity ID sign-in (+TOTP) lands on cloud.unity.com")

    page.goto(ctx.creds["url"])
    try:
        page.wait_for_load_state("networkidle", timeout=U.SETTLE_TIMEOUT)
    except Exception:
        pass
    ctx.state("members_page", U._wait_for_members_page(page), "organisation members page rendered")
    U._dismiss_cookie_banner(page)

    ctx.check("search", "the members list filter/search input (not the global top-bar search)", hints=("search", "filter"), abort=False)
    if ctx.require_member("row", "kebab", "remove_item"):
        status = find_user_row(
            page, ctx.canary,
            extra_row_selectors=tuple(split_top_level(ctx.current("row_extra"))),
            search_sel=ctx.current("search"),
            row_sel=ctx.current("row"),
        )
        ctx.state("find_user_row", status == "found", "plugin's find_user_row locates the canary", detail=f"find_user_row -> {status}", abort=False)
        row = ctx.check("row", CANARY_GOAL, must_contain=ctx.canary, extra=ctx.current("row_extra"), hints=("row", "member"))
        kebab = ctx.check("kebab", "the row's '⋯' / more-actions menu button", scope=row, hints=("more", "options", "actions"))
        _open_menu_and_check(
            ctx, kebab,
            item_key="remove_item",
            item_goal="the 'Remove from organization' item in the opened row menu (presence only)",
            hints=("remove from organization", "remove"),
            alt=lambda: page.get_by_role("menuitem", name=re.compile(r"remove\s+from\s+organization", re.I)),
        )
    ctx.skip("dialog", "confirmation dialog is never opened by the nightly check", key="dialog")
    ctx.skip("confirm_button", "confirmation dialog is never opened by the nightly check", key="confirm_button")


# ---------------------------------------------------------------------------
# Plastic SCM
# ---------------------------------------------------------------------------
def probe_plastic_scm(ctx: ProbeContext) -> None:
    import services.svc_plastic_scm as P

    ctx.plugin_modules.append(P)
    page = ctx.page
    page.set_default_timeout(P.DEFAULT_TIMEOUT)

    page.goto(ctx.creds["url"])
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5_000)
    except Exception:
        pass
    if "plasticscm.com" in page.url.lower() and is_on_auth_page(page):
        ctx.check("sso_button", "the 'Sign in with Unity' button on the Plastic SCM login page", hints=("unity", "sign in"), abort=False)
    else:
        ctx.skip("sso_button", "session still valid - Plastic login page not shown", key="sso_button")
    _login_form_checks(ctx)
    ctx.state("login", P._login(page, ctx.creds), "Unity ID OAuth returns to the Plastic SCM dashboard")

    page.goto(ctx.creds["url"])
    try:
        page.wait_for_load_state("networkidle", timeout=P.SETTLE_TIMEOUT)
    except Exception:
        pass
    ctx.state("on_app", P._on_app(page), "users-and-groups page is on plasticscm.com")

    ctx.check("search", "the users list search/filter input", hints=("search", "filter"), abort=False)
    if ctx.require_member("row", "delete_button"):
        status = find_user_row(page, ctx.canary, extra_row_selectors=(ctx.current("row"),), search_sel=ctx.current("search"))
        ctx.state("find_user_row", status == "found", "plugin's find_user_row locates the canary", detail=f"find_user_row -> {status}", abort=False)
        ctx.check("row", CANARY_GOAL, must_contain=ctx.canary, hints=("user-row", "row"))
        ctx.check(
            "delete_button",
            "the row's red trash/delete button for this user (presence only - it fires a native confirm)",
            hints=("delete", "remove", "trash"),
        )


# ---------------------------------------------------------------------------
# Skills Base (both orgs share markup; sel_slug = "skills_base")
# ---------------------------------------------------------------------------
def probe_skills_base(ctx: ProbeContext) -> None:
    import services.svc_skills_base_programmers as K

    ctx.plugin_modules.append(K)
    page = ctx.page
    page.set_default_timeout(K.DEFAULT_TIMEOUT)

    page.goto(ctx.creds["url"])
    try:
        page.wait_for_load_state("domcontentloaded", timeout=K.SETTLE_TIMEOUT)
    except Exception:
        pass
    K._maybe_solve_captcha(page)
    if K._is_skills_base_login_landing(page):
        K._click_button_named(page, "Log in with a Skills Base account")
        time.sleep(1.0)
    _login_form_checks(ctx)
    ctx.state("login", K._login(page, ctx.creds), "Skills Base sign-in (Turnstile via solvecaptcha, MFA-setup skipped)")

    users_status = K._open_users_area(page, ctx.creds, ctx.canary)
    if users_status != "ok" and K._handle_mfa_setup(page):
        users_status = K._open_users_area(page, ctx.creds, ctx.canary)
    ctx.state("people_list", users_status == "ok", "Directories → People grid is open", detail=users_status)
    time.sleep(0.5)

    ctx.check("people_search", "the 'Search people' box above the People grid", hints=("search", "people"))
    ctx.check("dt_info", "the DataTables info line ('Showing 1 to N of M entries')", hints=("showing", "entries"), abort=False, timeout_s=3.0)
    if ctx.require_member("row", "delete_button"):
        status = K._find_person(page, ctx.canary)
        ctx.state("find_person", status == "found", "plugin's _find_person locates the canary row", detail=f"_find_person -> {status}", abort=False)
        row = ctx.check("row", CANARY_GOAL, must_contain=ctx.canary, extra=", ".join(ctx.current("row_extra")), hints=("row", "person"))
        try:
            row.hover()
            time.sleep(0.3)
        except Exception:
            pass
        ctx.check("delete_button", "the row's trash/delete control (presence only)", scope=row, hints=("delete", "trash", "remove"))
    ctx.skip("confirm_surface", "confirmation modal is never opened by the nightly check", key="confirm_surface")


# ---------------------------------------------------------------------------
# Maxon
# ---------------------------------------------------------------------------
def probe_maxon(ctx: ProbeContext) -> None:
    import services.svc_maxon as M

    ctx.plugin_modules.append(M)
    page = ctx.page
    page.set_default_timeout(M.DEFAULT_TIMEOUT)
    ctx.escape(2)

    page.goto(ctx.creds["url"])
    M._dismiss_cookie_banner(page)
    _login_form_checks(ctx)
    ctx.state("login", M._login(page, ctx.creds), "Maxon sign-in lands on my.maxon.net")
    page.goto(ctx.creds["url"])
    try:
        page.wait_for_load_state("networkidle", timeout=M.SETTLE_TIMEOUT)
    except Exception:
        pass

    tab = ctx.check("users_tab", "the 'Users' tab of the team dashboard", hints=("users",))
    try:
        tab.click()
    except Exception:
        pass
    pane = ctx.check("users_pane", "the Users tab pane (the panel holding the users table)", hints=("users",), timeout_s=8.0)
    ctx.check("search", "the users table search/filter input", hints=("search", "filter"), abort=False)
    if ctx.require_member("row", "kebab", "remove_item"):
        status = find_user_row(page, ctx.canary, search_sel=ctx.current("search"))
        ctx.state("find_user_row", status == "found", "plugin's find_user_row locates the canary", detail=f"find_user_row -> {status}", abort=False)
        row = ctx.check("row", CANARY_GOAL, scope=pane, must_contain=ctx.canary, hints=("row",))
        kebab = ctx.check("kebab", "the row's '⋯' dropdown toggle (more actions)", scope=row, hints=("more", "dropdown", "toggle"))
        _open_menu_and_check(
            ctx, kebab,
            item_key="remove_item",
            item_goal="the 'Remove from team' item in the opened dropdown menu (presence only)",
            hints=("remove from team", "remove"),
        )
    ctx.skip("dialog", "confirmation dialog is never opened by the nightly check", key="dialog")
    ctx.skip("confirm_button", "confirmation dialog is never opened by the nightly check", key="confirm_button")


# ---------------------------------------------------------------------------
# Autodesk (login + member list only; removal is not implemented in the plugin)
# ---------------------------------------------------------------------------
def probe_autodesk(ctx: ProbeContext) -> None:
    import services.svc_autodesk as A

    ctx.plugin_modules.append(A)
    page = ctx.page
    page.set_default_timeout(A.DEFAULT_TIMEOUT)

    ctx.state("login", A._login(page, ctx.creds), "Autodesk sign-in (email → password → TOTP) lands on manage.autodesk.com")
    page.goto(ctx.creds["url"])
    try:
        page.wait_for_load_state("networkidle", timeout=A.SETTLE_TIMEOUT)
    except Exception:
        pass
    ctx.state("admin_app", A._on_admin_app(page) and not A._on_marketing_page(page), "admin app (not the marketing page) is open")
    ctx.check("search", "the users list search/filter input", hints=("search", "filter"), abort=False)
    if ctx.require_member("find_user_row"):
        status = find_user_row(page, ctx.canary, search_sel=ctx.current("search"))
        ctx.state("find_user_row", status == "found", "plugin's find_user_row locates the canary", detail=f"find_user_row -> {status}", abort=False)


# ---------------------------------------------------------------------------
# ChatGPT Team (invite only; session auth)
# ---------------------------------------------------------------------------
def probe_chatgpt(ctx: ProbeContext) -> None:
    import services.svc_chatgpt as C

    ctx.plugin_modules.append(C)
    page = ctx.page
    page.set_default_timeout(C.DEFAULT_TIMEOUT)
    url = ctx.creds.get("url") or "https://chatgpt.com/admin/members"

    page.goto(url)
    try:
        page.wait_for_load_state("networkidle", timeout=C.SETTLE_TIMEOUT)
    except Exception:
        pass
    if not C._on_admin_members(page) or is_on_auth_page(page):
        if ctx.creds.get("login") and ctx.creds.get("password"):
            _login_form_checks(ctx)
            ctx.state("login", C._login(page, ctx.creds), "scripted ChatGPT sign-in reaches /admin", abort=False)
            if not C._on_admin_members(page):
                page.goto(url)
        else:
            ctx.skip("login_form", "session-auth service without LOGIN/PASSWORD - prime with prime_session.py", key="login_email")
    ctx.state("admin_members", C._on_admin_members(page) and not is_on_auth_page(page), "chatgpt.com/admin/members is open and authenticated")
    ctx.state("admin_shell", C._wait_for_admin_shell(page, quick_timeout_s=8.0, slow_timeout_s=20.0), "members admin shell rendered (Invite button / search / heading)")
    ctx.check("admin_shell", "any element of the members admin shell (Invite button, search box, Members heading, member row)", hints=("invite", "members", "search"), abort=False)

    invite_btn = ctx.check(
        "invite_button", "the 'Invite members' button that opens the invite dialog",
        hints=("invite",), abort=False,
        alt=lambda: page.get_by_role("button", name=re.compile(r"^(Invite\s+members?|\+\s*Invite|Add\s+members?|Invite)\b", re.I)),
    )
    if invite_btn is None:
        return
    try:
        invite_btn.click(timeout=8_000)
    except Exception as e:  # noqa: BLE001
        ctx.state("invite_dialog", False, "invite dialog opens", detail=f"click failed: {type(e).__name__}", abort=False)
        return
    dialog = ctx.check("dialog", "the open invite dialog (modal)", hints=("dialog", "invite"), abort=False, timeout_s=8.0)
    if dialog is not None:
        ctx.check("invite_email", "the email input inside the invite dialog", scope=dialog, hints=("email",), abort=False)
        ctx.check("send_button", "the 'Send invite' button inside the invite dialog (presence only, may be disabled)", scope=dialog, hints=("send", "invite", "add"), abort=False)
    ctx.escape(3)


# ---------------------------------------------------------------------------
# Registry: display name (as in Asana / env key) -> probe + profile/registry slugs
# ---------------------------------------------------------------------------
# name: Asana service name used by load_creds(); slug: persistent profile +
# get_page slug (same derivation as deactivate.py); sel_slug: selector registry
# slug when it differs; page_slug: get_page attr the plugin uses.
SERVICES: list[dict] = [
    {"name": "Figma", "slug": "figma", "probe": probe_figma},
    {"name": "Unity", "slug": "unity", "probe": probe_unity},
    {"name": "Plastic SCM", "slug": "plastic_scm", "probe": probe_plastic_scm},
    {"name": "Slack (Workspace Redbark2)", "slug": "slack_workspace_redbark2", "probe": probe_slack},
    {"name": "Skills Base (Programmers)", "slug": "skills_base_programmers", "sel_slug": "skills_base", "probe": probe_skills_base},
    {"name": "Skills Base", "slug": "skills_base", "sel_slug": "skills_base", "probe": probe_skills_base},
    {"name": "Maxon", "slug": "maxon", "probe": probe_maxon},
    {"name": "Autodesk", "slug": "autodesk", "probe": probe_autodesk},
    {"name": "ChatGPT", "slug": "chatgpt", "probe": probe_chatgpt},
]
