#!/usr/bin/env python3
"""Process Asana "Заявка на лицензию/доступ ..." tasks and invite users via browser.

Counterpart to deactivate.py — same .env, same persistent browser profile, same
service-plugin convention. Plugins implementing access grants export an
`invite(context, creds, target_user, role) -> str` function.

Usage:
    ./grant.py --date 2026-05-19 --service figma --dry-run
    ./grant.py --date 2026-05-19 --service figma --yes
    ./grant.py --task 1214935218171397
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

from asana_client import AsanaError, _get, fetch_tasks_due_on
from deactivate import load_creds, _capture_shots
from services import _slug, env_key
from services._common import service_profile_dir

TITLE_PREFIX = "Заявка на лицензию/доступ "
TITLE_SUFFIX_RE = re.compile(r"\s*/\s*Purchase Request\s*$", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# Lines we lift out of the Russian access-request task body.
NOTES_FIELDS = {
    "service": re.compile(r"^\s*Имя сервиса:\s*(.+)$", re.M),
    "path": re.compile(r"^\s*Путь к объекту:\s*(.+)$", re.M),
    "email": re.compile(r"^\s*email сотрудника:\s*([\w.+-]+@[\w-]+\.[\w.-]+)", re.M),
    "ad_login": re.compile(r"^\s*логин ad:\s*(\S+)", re.M),
    "name": re.compile(r"^\s*ФИ:\s*(.+)$", re.M),
}

ROLE_HINT_RE = re.compile(r"->\s*([A-Za-zА-Яа-я]+)\b")

# Note on domain remapping: previously a global rule rewrote
# <localpart>@fluytstudio.net → <localpart>.ff@playrix.com. That's now handled
# inside individual plugins (svc_figma.invite remaps; svc_chatgpt.invite does
# NOT — it invites contractors into a fluytstudio-scoped workspace under their
# original address). Keep the parsed target email unchanged here.


def parse_task(task: dict) -> dict[str, str]:
    """Pull service / target email / role hint out of an access-request task."""
    title = task["name"].strip()
    notes = task.get("notes", "") or ""

    # Service name: prefer the structured `Имя сервиса:` line; fall back to the
    # title between the prefix and " / Purchase Request".
    m = NOTES_FIELDS["service"].search(notes)
    if m:
        service = m.group(1).strip()
    else:
        body = title[len(TITLE_PREFIX):] if title.startswith(TITLE_PREFIX) else title
        body = TITLE_SUFFIX_RE.sub("", body)
        # Title shape: "<Service> для <Name>"
        service = re.split(r"\s+для\s+", body, maxsplit=1)[0].strip()

    # Target email: prefer the structured `email сотрудника:` field.
    m = NOTES_FIELDS["email"].search(notes)
    target = m.group(1) if m else ""
    if not target:
        m = EMAIL_RE.search(notes)
        target = m.group(0) if m else ""

    # Role hint: "Путь к объекту: Figma -> Full" → "full".
    role = "full"
    m = NOTES_FIELDS["path"].search(notes)
    if m:
        rh = ROLE_HINT_RE.search(m.group(1))
        if rh:
            role = rh.group(1).strip().lower()

    return {"service": service, "target": target, "role": role}


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
        elif "missing-credentials" in statuses:
            tag = f"[SKIP] missing creds for {env_key(service)}"
        elif "missing-invite" in statuses:
            warn = _missing_invite_warning(service) or "no invite() in plugin"
            tag = f"[SKIP] {warn}"
        elif "missing-target" in statuses:
            tag = "[SKIP] could not parse target email from task"
        else:
            tag = f"[?]    {', '.join(sorted(statuses))}"

        print(f"\n  {service}  —  {len(rows)} task(s)  —  {tag}")
        for row in rows:
            print(
                f"    • {row['target']:<40}  role={row['role']:<8}  "
                f"({row['task']['gid']})"
            )


def main() -> int:
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
            tasks = fetch_tasks_due_on(args.date, text_filter="Заявка на лицензию")
    except AsanaError as e:
        print(f"asana error: {e}", file=sys.stderr)
        return 1

    matching = [t for t in tasks if t["name"].startswith(TITLE_PREFIX)]
    if not matching:
        print(f"no matching access-request tasks")
        return 0

    # Normalize punctuation so a filter like "slack workspace redbark2" matches
    # the Asana service name "Slack (Workspace Redbark2)".
    def _norm(s: str) -> str:
        return re.sub(r"\W+", " ", s.lower(), flags=re.UNICODE).strip()

    service_filters = [_norm(s) for s in args.service]

    plan: list[dict[str, Any]] = []
    for t in matching:
        parsed = parse_task(t)
        service = parsed["service"]
        target = parsed["target"]
        role = parsed["role"]
        if service_filters and not any(f in _norm(service) for f in service_filters):
            continue
        creds = load_creds(service)
        if not creds:
            status = "missing-credentials"
        elif not target:
            status = "missing-target"
        elif _missing_invite_warning(service) is not None:
            status = "missing-invite"
        else:
            status = "ready"
        plan.append({
            "task": t,
            "service": service,
            "target": target,
            "role": role,
            "creds": creds,
            "status": status,
        })

    if not plan:
        print("no tasks matched the filters")
        return 0

    header = f"Plan for {args.date}" if args.date else f"Plan for task {args.task}"
    print_plan(plan, header)
    if service_filters:
        print(f"  (filtered to services matching: {', '.join(args.service)})")

    if args.dry_run:
        return 0

    ready = [r for r in plan if r["status"] == "ready"]
    if not ready:
        print("\nnothing ready to run")
        return 0

    if not args.yes:
        ans = input("\nproceed with invites? [y/N] ").strip().lower()
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
        if machine_log:
            print(f"RESULT|{gid}|{row['service']}|{row['target']}|{status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
