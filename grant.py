#!/usr/bin/env python3
"""Process Asana "Доступ к ..." tasks and invite users via browser.

Counterpart to deactivate.py — same .env, same persistent browser profile, same
service-plugin convention. Plugins implementing access grants export an
`invite(context, creds, target_user, role) -> str` function.

Scope: only the onboarding template, titled "Доступ к <Service> - <ФИ>".
The "Заявка на лицензию/доступ ..." purchase-request form is intentionally NOT
handled — those tasks are procurement/partner/investigation requests that rarely
map onto an `invite()` call, so they are left for manual processing.

Usage:
    ./grant.py --date 2026-08-24 --service figma --dry-run
    ./grant.py --date 2026-08-24 --service adobe --yes
    ./grant.py --task 1217366391048351
"""
from __future__ import annotations

import argparse
import importlib
import os
import re
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from asana_client import (
    AsanaError,
    _get,
    complete_task,
    create_task_comment,
    fetch_task_comments,
    fetch_tasks_due_on,
    upload_task_attachment,
)
from deactivate import load_creds, _capture_shots
from services import _slug, env_key
from services._common import make_stdout_safe, service_profile_dir

# Onboarding template: "Доступ к <Service> - <ФИ>". The service name lives
# ONLY in the title (this form has no `Имя сервиса:` field).
TITLE_PREFIX = "Доступ к "
# The person is separated from the service by a dash, a slash or "для". Service
# names can themselves contain a dash or slash ("Microsoft Partner Center /
# Windows Store"), so we split on the LAST separator, not the first.
PERSON_SEP_RE = re.compile(r"\s+(?:[-–—/]|для)\s+")
# Must end on an alphanumeric: the notes are prose, so a trailing sentence dot
# ("...на maximt@playrix.com. Посмотрите") would otherwise be captured as part
# of the address and read as a different account.
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]*\w")

# Lines we lift out of the Russian task body. The onboarding form uses
# `Корпоративная почта:`, but some "Доступ к" tasks come from a support-request
# variant that uses `email сотрудника:` instead, so both are read.
NOTES_FIELDS = {
    # `Сервис:` / `Роль:` sit at the very END of the onboarding body, after the
    # LDAP and template blocks. They are the pair the request was built from,
    # so they beat anything inferred from the title.
    "service": re.compile(r"^\s*Сервис:\s*(.+)$", re.M),
    "role": re.compile(r"^\s*Роль:\s*(.+)$", re.M),
    "path": re.compile(r"^\s*Путь к объекту:\s*(.+)$", re.M),
    "email": re.compile(r"^\s*email сотрудника:\s*([\w.+-]+@[\w-]+\.[\w.-]*\w)", re.M),
    "corp_email": re.compile(
        r"^\s*Корпоративная почта:\s*([\w.+-]+@[\w-]+\.[\w.-]*\w)", re.M
    ),
    "ad_login": re.compile(r"^\s*логин ad:\s*(\S+)", re.M),
    "name": re.compile(r"^\s*ФИ:\s*(.+)$", re.M),
}

# Asana's forms offer an "Другое" ("Other") option, which reaches us as a
# literal service name and would yield an unusable env key like `ДРУГОЕ`
# (Cyrillic). Treat it as "no service" rather than guessing.
PLACEHOLDER_RE = re.compile(
    r"^(?:другое|прочее|other|misc|n/?a|нет|none|-{1,2}|—|–)$", re.I
)
# Asana auto-linkifies bare domains, so a title mentioning "Coda.io" can arrive
# as "http://Coda.io" and yielded the env key HTTP_CODA_IO.
URL_SCHEME_RE = re.compile(r"^(?:https?|ftp)://", re.I)


def _clean_service(name: str) -> str:
    """Normalize a service name: drop URL scheme, quotes, decorations.

    Onboarding titles prefix some services with a status emoji
    ("🔄Adobe Creative Cloud (RedBark)"), which would otherwise end up in the
    slug and env key. Only the LEADING junk is stripped — a trailing ")" is
    part of the name ("Adobe Creative Cloud (RedBark)").
    """
    s = name.strip().strip("«»\"'`").strip()
    s = URL_SCHEME_RE.sub("", s)
    s = re.sub(r"^[^\w(]+", "", s)  # emoji / bullets / stray punctuation
    s = s.rstrip("/").strip()
    return s


def _is_placeholder(value: str) -> bool:
    return bool(PLACEHOLDER_RE.match(value.strip()))


def _service_from_title(title: str) -> str:
    """Extract the service name from the task title, or "" if absent."""
    body = title[len(TITLE_PREFIX):] if title.startswith(TITLE_PREFIX) else title
    body = body.strip()

    # Trim the trailing "- <ФИ>" / "/ <ФИ>" / "для <ФИ>". Split on the LAST
    # separator so a service name containing one survives intact
    # ("Microsoft Partner Center / Windows Store - Иван Иванов").
    if seps := list(PERSON_SEP_RE.finditer(body)):
        body = body[: seps[-1].start()]

    return _clean_service(body)

# Note on domain remapping: previously a global rule rewrote
# <localpart>@fluytstudio.net → <localpart>.ff@playrix.com. That's now handled
# inside individual plugins (svc_figma.invite remaps; svc_chatgpt.invite does
# NOT — it invites contractors into a fluytstudio-scoped workspace under their
# original address). Keep the parsed target email unchanged here.


def parse_task(task: dict) -> dict[str, Any]:
    """Pull service / target email / role hint out of a "Доступ к ..." task.

    Returns the keys `service`, `target`, `role` plus `service_title` (what the
    title yielded) and `warnings` (human-readable strings for the plan printer).

    The service comes from the body's `Сервис:` line, falling back to the title
    when that line is absent or holds the form's "Другое" placeholder. A
    disagreement between the two is reported rather than silently resolved.
    """
    title = task["name"].strip()
    notes = task.get("notes", "") or ""
    warnings: list[str] = []

    m = NOTES_FIELDS["service"].search(notes)
    service_notes = _clean_service(m.group(1)) if m else ""
    service_title = _service_from_title(title)

    if service_notes and not _is_placeholder(service_notes):
        service = service_notes
        if service_title and service_title.lower() != service_notes.lower():
            warnings.append(
                f"service mismatch: body={service_notes!r} vs title={service_title!r}"
                " — using the body field; verify manually"
            )
    elif service_title and not _is_placeholder(service_title):
        service = service_title
        if service_notes:
            warnings.append(
                f"`Сервис:` is a placeholder ({service_notes!r}) — "
                f"using title {service_title!r}"
            )
    else:
        service = ""
        warnings.append(
            "no usable service name — body "
            f"({service_notes or 'none'!r}) and title ({service_title or 'none'!r})"
            " give nothing; read the task manually"
        )

    # Target email: prefer the structured fields — `Корпоративная почта:` on the
    # onboarding form, `email сотрудника:` on the support-request variant — and
    # only then fall back to the first address anywhere in the body.
    target = ""
    for field in ("email", "corp_email"):
        m = NOTES_FIELDS[field].search(notes)
        if m:
            target = m.group(1)
            break
    if not target:
        m = EMAIL_RE.search(notes)
        target = m.group(0) if m else ""

    # If the body mentions other addresses, the requested access may be for a
    # different account than the requester's (seen on a real task), so warn.
    if target:
        others = {e for e in EMAIL_RE.findall(notes) if e.lower() != target.lower()}
        if others:
            warnings.append(
                "other email(s) in body — access may be for a different account: "
                + ", ".join(sorted(others)[:3])
            )

    # Role hint. The onboarding template carries it in a dedicated line
    # ("Роль: Photoshop"); the support-request variant encodes it as the leaf
    # of an object path ("Путь к объекту: Adobe -> After Effects"). The value
    # is kept whole so multi-word products survive ("after effects", not
    # "after"); chained paths resolve to the leaf segment.
    role = "full"
    candidate = ""
    m = NOTES_FIELDS["role"].search(notes)
    if m:
        candidate = m.group(1).strip().lower()
    else:
        m = NOTES_FIELDS["path"].search(notes)
        if m and "->" in m.group(1):
            candidate = m.group(1).rsplit("->", 1)[1].strip().lower()
    if not candidate:
        pass  # no usable hint — keep the default
    elif _is_placeholder(candidate):
        # "Другое" is the form's placeholder, not a role a plugin knows.
        warnings.append(
            f"role hint is a placeholder ({candidate!r}) — defaulting to 'full'"
        )
    else:
        role = candidate

    return {
        "service": service,
        "target": target,
        "role": role,
        "service_title": service_title,
        "warnings": warnings,
    }


def fetch_one_task(task_gid: str) -> dict:
    return _get(
        f"/tasks/{task_gid}",
        params={"opt_fields": "name,notes,due_on,gid,permalink_url,completed"},
    )


def _missing_invite_warning(service: str) -> str | None:
    """If the resolved plugin module has no invite(), return a warning string."""
    module_name = f"services.{_slug(service)}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        return f"no plugin for {service!r} (would fall back to generic — invite not implemented)"
    if not hasattr(module, "invite"):
        return f"plugin {module_name} has no invite() — only deactivate() is implemented"
    return None


def print_plan(plan: list[dict], header: str) -> None:
    by_service: dict[str, list[dict]] = {}
    for row in plan:
        by_service.setdefault(row["service"], []).append(row)

    total = len(plan)
    print(f"\n=== {header} — {total} task(s) across {len(by_service)} service(s) ===")
    for service in sorted(by_service):
        rows = by_service[service]
        statuses = {r["status"] for r in rows}
        if statuses == {"ready"}:
            tag = "[OK]   ready"
        elif statuses == {"already-processed"}:
            tag = "[DONE] already has an automation comment (use --force to re-run)"
        elif statuses <= {"ready", "already-processed"}:
            # Common on a re-run: part of the day is done, the rest is new.
            n_ready = sum(1 for r in rows if r["status"] == "ready")
            tag = f"[OK]   {n_ready} to run, {len(rows) - n_ready} already done"
        elif "missing-credentials" in statuses:
            tag = f"[SKIP] missing creds for {env_key(service)}"
        elif "missing-invite" in statuses:
            warn = _missing_invite_warning(service) or "no invite() in plugin"
            tag = f"[SKIP] {warn}"
        elif "missing-target" in statuses:
            tag = "[SKIP] could not parse target email from task"
        elif "missing-service" in statuses:
            tag = "[SKIP] no service name in task notes or title"
        else:
            tag = f"[?]    {', '.join(sorted(statuses))}"

        label = service or "<no service>"
        print(f"\n  {label}  —  {len(rows)} task(s)  —  {tag}")
        for row in rows:
            print(
                f"    • {row['target']:<40}  role={row['role']:<8}  "
                f"({row['task']['gid']})"
            )
            if row.get("prior_comment"):
                # ASCII only: this runs under the Windows scheduler, whose
                # console codepage can't encode arrows and would crash the run.
                print(f"        <- prior comment: {row['prior_comment']}")
            # Parsing ambiguities are easy to miss and have caused wrong-service
            # / wrong-account picks, so print them per task.
            for w in row.get("warnings", []):
                print(f"        ! {w}")


# Outcomes that mean access is in place → the Asana task can be completed.
# Adobe appends detail ("invited (added groups: Photoshop Configuration)"), so
# these are matched as PREFIXES, not exact strings.
COMPLETABLE_PREFIXES = ("invited", "already-invited", "already-member")
# Outcomes worth a comment at all: the completable ones plus the terminal
# "nothing to do" verdicts. Transient failures (needs-login, failed: …) are
# left uncommented — they mean "re-run", not "here is the result".
COMMENTABLE_PREFIXES = COMPLETABLE_PREFIXES + ("skipped:",)

# Stamped into every comment this runner writes, and looked for on re-runs so a
# task that already has a proof comment is not acted on twice. Kept on its own
# line and deliberately unglamorous — it is a machine marker, not prose.
AUTOMATION_MARKER = "[grant-bot]"
# Comments written before the marker existed are still recognised by their
# headline, so the very first re-run after this change doesn't re-invite.
LEGACY_HEADLINES = (
    "Access granted by automation.",
    "User already had access in the service.",
)


def _outcome_matches(outcome: str, prefixes: tuple[str, ...]) -> bool:
    return any(outcome.startswith(p) for p in prefixes)


def _existing_automation_comment(task_gid: str) -> str | None:
    """Return a short description of a prior automation comment, or None.

    Asana is the source of truth here: the services themselves have their own
    idempotency guards (Figma's "already-invited", Adobe's "already-member"),
    but those still cost a login and a page load. Checking the task first makes
    a re-run cheap and, for plugins without such a guard, safe.

    Raises AsanaError if the comments can't be read — the caller must decide,
    loudly, whether to proceed without the guard. Swallowing that made the
    whole feature a silent no-op on a token lacking `stories:read`.
    """
    comments = fetch_task_comments(task_gid)
    for c in comments:
        text = c.get("text") or ""
        if AUTOMATION_MARKER in text or any(h in text for h in LEGACY_HEADLINES):
            when = (c.get("created_at") or "")[:10]
            first = text.strip().splitlines()[0][:60] if text.strip() else "(empty)"
            return f"{first} ({when})" if when else first
    return None


def _comment_after_outcome(
    row: dict, outcome: str, shots: list[str], complete: bool = True
) -> str | None:
    """Upload proof screenshots, comment, and (optionally) complete the task.

    Mirrors deactivate.py's write-back so a grant leaves the same audit trail.
    Returns None on success, or a compact error string if any Asana call failed.
    """
    if not _outcome_matches(outcome, COMMENTABLE_PREFIXES):
        return None
    gid = row["task"]["gid"]

    try:
        for shot in shots:
            upload_task_attachment(gid, shot)

        if outcome.startswith("invited"):
            headline = "Access granted by automation."
        elif outcome.startswith(("already-invited", "already-member")):
            headline = "User already had access in the service."
        else:
            headline = "No action taken."
        lines = [
            headline,
            f"Service: {row['service']}",
            f"User: {row['target']}",
            f"Role: {row['role']}",
            f"Result: {outcome}",
        ]
        shot_names = [Path(s).name for s in shots]
        if shot_names:
            lines.append("Screenshot(s) attached:")
            lines.extend(f"- {name}" for name in shot_names)
        else:
            # API-only plugins (e.g. Adobe UMAPI) never open a page.
            lines.append("Screenshot: not available for this service.")

        will_complete = complete and _outcome_matches(outcome, COMPLETABLE_PREFIXES)
        if will_complete:
            lines.append("Task marked complete by automation.")
        # Marker last, so a re-run can recognise this comment even if the
        # wording above changes.
        lines.append(AUTOMATION_MARKER)

        create_task_comment(gid, "\n".join(lines))

        if will_complete:
            complete_task(gid)
        return None
    except AsanaError as e:
        return str(e)


def main() -> int:
    make_stdout_safe()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", help="Due date in YYYY-MM-DD (mutually exclusive with --task)")
    ap.add_argument("--task", help="Single Asana task GID to process")
    ap.add_argument("--service", action="append", default=[], metavar="NAME",
                    help="Process only tasks for service(s) matching NAME (case-insensitive substring). Repeatable.")
    ap.add_argument("--dry-run", action="store_true", help="Print plan and exit")
    ap.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    ap.add_argument("--headless", action="store_true", help="Run browser headless")
    ap.add_argument("--screenshot-dir", default="screenshots",
                    help="Where to save per-task screenshots (default: ./screenshots)")
    ap.add_argument(
        "--no-comment",
        action="store_true",
        help="Do NOT write the proof comment/attachment back to Asana",
    )
    ap.add_argument(
        "--no-complete",
        action="store_true",
        help="Do NOT mark Asana tasks complete on success (default: mark complete)",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Re-run tasks that already carry an automation comment "
             "(default: skip them)",
    )
    args = ap.parse_args()

    if not args.date and not args.task:
        print("error: pass --date YYYY-MM-DD or --task <gid>", file=sys.stderr)
        return 2
    if args.date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
        print(f"error: --date must be YYYY-MM-DD, got {args.date!r}", file=sys.stderr)
        return 2

    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

    try:
        if args.task:
            tasks = [fetch_one_task(args.task)]
        else:
            tasks = fetch_tasks_due_on(args.date, text_filter="Доступ к")
    except AsanaError as e:
        print(f"asana error: {e}", file=sys.stderr)
        return 1

    matching = [t for t in tasks if t["name"].strip().startswith(TITLE_PREFIX)]
    if not matching:
        print(f"no matching access-request tasks")
        return 0

    # Normalize punctuation so a filter like "slack workspace redbark2" matches
    # the Asana service name "Slack (Workspace Redbark2)".
    def _norm(s: str) -> str:
        return re.sub(r"\W+", " ", s.lower(), flags=re.UNICODE).strip()

    service_filters = [_norm(s) for s in args.service]

    plan: list[dict[str, Any]] = []
    guard_errors: list[str] = []
    for t in matching:
        parsed = parse_task(t)
        service = parsed["service"]
        target = parsed["target"]
        role = parsed["role"]
        if service_filters and not any(f in _norm(service) for f in service_filters):
            continue
        creds = load_creds(service) if service else None
        if not service:
            status = "missing-service"
        elif not creds:
            status = "missing-credentials"
        elif not target:
            status = "missing-target"
        elif _missing_invite_warning(service) is not None:
            status = "missing-invite"
        else:
            status = "ready"
        # Only worth an API call for tasks we would otherwise act on.
        prior = None
        if status == "ready" and not args.force:
            try:
                prior = _existing_automation_comment(t["gid"])
            except AsanaError as e:
                guard_errors.append(str(e))
            else:
                if prior:
                    status = "already-processed"
        plan.append({
            "task": t,
            "service": service,
            "target": target,
            "role": role,
            "creds": creds,
            "status": status,
            "warnings": parsed.get("warnings", []),
            "prior_comment": prior,
        })

    if not plan:
        print("no tasks matched the filters")
        return 0

    header = f"Plan for {args.date}" if args.date else f"Plan for task {args.task}"
    print_plan(plan, header)
    if service_filters:
        print(f"  (filtered to services matching: {', '.join(args.service)})")

    if guard_errors:
        # The re-run guard is inoperative — say so instead of letting the plan
        # imply these tasks were checked and found clean.
        print(
            f"\n!! WARNING: could not read Asana comments for {len(guard_errors)} "
            f"task(s) — the already-processed guard is NOT active for them."
        )
        print(f"   {guard_errors[0]}")
        if "stories:read" in guard_errors[0]:
            print(
                "   The token lacks `stories:read`. Re-mint it with that scope "
                "(or full permissions) to enable the guard."
            )
        print("   Re-running may repeat an action that was already performed.")

    if args.dry_run:
        return 0

    ready = [r for r in plan if r["status"] == "ready"]
    if not ready:
        print("\nnothing ready to run")
        return 0

    if not args.yes:
        prompt = "\nproceed with invites? [y/N] "
        if guard_errors:
            prompt = "\nproceed WITHOUT the already-processed guard? [y/N] "
        ans = input(prompt).strip().lower()
        if ans != "y":
            print("aborted")
            return 0

    from playwright.sync_api import sync_playwright
    # Stealth patches the launched Chromium to hide common automation tells
    # (navigator.webdriver, missing plugins, Chrome runtime, etc.) that
    # Cloudflare Turnstile uses to flag Playwright as a bot. Wrapping the
    # whole sync_playwright session means every context/page that comes out
    # of `p` is patched, including the existing persistent profile.
    from playwright_stealth import Stealth

    date_label = args.date or "manual"
    shot_root = Path(args.screenshot_dir) / date_label / "grant"
    shot_root.mkdir(parents=True, exist_ok=True)

    outcomes: dict[str, str] = {}
    shots_by_gid: dict[str, list[str]] = {}
    comment_errors: dict[str, str] = {}

    by_service: dict[str, list[dict]] = {}
    for row in ready:
        by_service.setdefault(row["service"], []).append(row)

    with Stealth().use_sync(sync_playwright()) as p:
        # One persistent context per service (separate window + cookie jar
        # under .browser_profiles/<slug>), opened/closed sequentially.
        for service, rows in by_service.items():
            slug = _slug(service).removeprefix("svc_") or "unknown"
            context = p.chromium.launch_persistent_context(
                user_data_dir=service_profile_dir(slug),
                headless=args.headless,
            )
            module = importlib.import_module(f"services.{_slug(service)}")
            handler = module.invite
            try:
                for row in rows:
                    gid = row["task"]["gid"]
                    target = row["target"]
                    role = row["role"]
                    print(f"\n== {service}: inviting {target} (role={role}) ==")
                    outcome = "failed: unknown"
                    try:
                        outcome = handler(context, row["creds"], target, role)
                    except TypeError:
                        # Plugin signature may not yet accept role — retry without.
                        try:
                            outcome = handler(context, row["creds"], target)
                        except Exception as e:  # noqa: BLE001
                            outcome = f"failed: {type(e).__name__}: {e}"
                    except Exception as e:  # noqa: BLE001
                        outcome = f"failed: {type(e).__name__}: {e}"
                    outcomes[gid] = outcome
                    shots_by_gid[gid] = _capture_shots(context, shot_root, row, outcome)
                    if not args.no_comment:
                        comment_error = _comment_after_outcome(
                            row,
                            outcome,
                            shots_by_gid[gid],
                            complete=not args.no_complete,
                        )
                        if comment_error:
                            comment_errors[gid] = comment_error
            finally:
                context.close()

    machine_log = bool(os.environ.get("ACTION_LOG_STREAM"))
    print("\n=== summary ===")
    for row in plan:
        gid = row["task"]["gid"]
        status = outcomes.get(gid, row["status"])
        print(
            f"  {gid:<18} "
            f"{row['service']:<18} "
            f"{row['target']:<40} "
            f"{status}"
        )
        for shot in shots_by_gid.get(gid, []):
            print(f"    screenshot: {shot}")
        if comment_errors.get(gid):
            print(f"    asana-comment-error: {comment_errors[gid]}")
        if machine_log:
            print(f"RESULT|{gid}|{row['service']}|{row['target']}|{status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
