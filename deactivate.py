#!/usr/bin/env python3
"""Process Asana "Удалить из ..." tasks and deactivate users via browser.

Setup (once):
    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    playwright install chromium

Usage:
    ./deactivate.py --date 2026-04-22
    ./deactivate.py --date 2026-04-22 --dry-run
    ./deactivate.py --date 2026-04-22 --yes --headless
    ./deactivate.py --date 2026-04-22 --screenshot-dir ./proof
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any

from secrets_provider import SecretsError, load_secrets

from asana_client import (
    AsanaError,
    complete_task,
    create_task_comment,
    fetch_tasks_due_on,
    upload_task_attachment,
)

# Outcomes that mean the user is no longer active in the service → the Asana
# task can be marked complete.
COMPLETABLE_OUTCOMES = ("deactivated", "already-deactivated")
from services import _slug, env_key, get_handler, required_env
from services._common import make_stdout_safe, service_profile_dir

TITLE_PREFIX = "Удалить из "
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Splits "App Store Connect - krizhanovskaya..." into [service, target].
# Recognises hyphen, en dash, em dash; requires whitespace on both sides so
# we don't split on a hyphen inside a service name like "1С-Битрикс".
DASH_SPLIT_RE = re.compile(r"\s+[-–—]\s+")


def parse_task(task: dict) -> tuple[str, str]:
    title = task["name"].strip()
    rest = title[len(TITLE_PREFIX):].strip()

    parts = DASH_SPLIT_RE.split(rest, maxsplit=1)
    if len(parts) == 2:
        service = parts[0].strip()
        remainder = parts[1].strip()
    else:
        # No dash separator — fall back to the first whitespace-separated token.
        first_split = rest.split(maxsplit=1)
        service = first_split[0] if first_split else ""
        remainder = first_split[1] if len(first_split) > 1 else ""

    # Preference order matters. Transfer-task titles carry the service account
    # inside the service portion — "Syncsketch (darya.minina.ff@playrix.com) при
    # переходе в Playrix - Дарья Минина" — so the whole title is searched before
    # the notes, whose "Корпоративная почта" is a different address than the one
    # the service actually knows.
    for haystack in (remainder, rest, task.get("notes", "")):
        email = EMAIL_RE.search(haystack)
        if email:
            return service, email.group(0)
    return service, remainder.lstrip("-—–:|, ").strip() or "<missing>"


def _first_env(*names: str) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def _env_key_candidates(service: str) -> list[str]:
    """Env-key candidates for a service, including intentional aliases."""
    key = env_key(service)
    keys = [key]
    # Asana sometimes includes product bundle details in the service name, but
    # operationally this is still the same Unity Cloud admin surface.
    if key.startswith("UNITY_"):
        keys.append("UNITY")
    # "Adobe Creative Cloud (RedBark)" et al. all share one UMAPI credential
    # stored under the canonical ADOBE_* keys.
    if key.startswith("ADOBE"):
        keys.append("ADOBE")
    # "Syncsketch (RedBark)" / "Syncsketch (…) при переходе в Playrix" are the
    # same workspace admin credential, stored under SYNCSKETCH_*.
    if key.startswith("SYNCSKETCH"):
        keys.append("SYNCSKETCH")
    return list(dict.fromkeys(keys))


def load_creds(service: str) -> dict | None:
    """Return creds dict, or None if no usable credentials are configured.

    Accepts either `SERVICE_<KEY>_<FIELD>` or bare `<KEY>_<FIELD>`.

    Four credential modes are supported:
      * Browser:  LOGIN + PASSWORD (and usually URL)
      * API:      TOKEN (used by API-based plugins like Slack)
      * OAuth:    CLIENT_ID + CLIENT_SECRET + ORG_ID (OAuth Server-to-Server,
                  used by API-based plugins like Adobe UMAPI)
      * Session:  AUTH=session + URL — plugin handles auth via Playwright's
                  persistent profile (one-time manual headed login, cookies
                  survive across runs). No credentials stored in .env.

    The plugin chooses which fields to use. `url` may still be None for
    browser plugins (the caller treats that as `missing-url`).
    """
    keys = _env_key_candidates(service)
    creds = {
        "url": _first_env(*(n for key in keys for n in (f"SERVICE_{key}_URL", f"{key}_URL"))),
        "login": _first_env(*(n for key in keys for n in (f"SERVICE_{key}_LOGIN", f"{key}_LOGIN"))),
        "password": _first_env(*(n for key in keys for n in (f"SERVICE_{key}_PASSWORD", f"{key}_PASSWORD"))),
        "token": _first_env(*(n for key in keys for n in (f"SERVICE_{key}_TOKEN", f"{key}_TOKEN"))),
        "team_id": _first_env(*(n for key in keys for n in (f"SERVICE_{key}_TEAM_ID", f"{key}_TEAM_ID"))),
        "client_id": _first_env(*(n for key in keys for n in (f"SERVICE_{key}_CLIENT_ID", f"{key}_CLIENT_ID"))),
        "client_secret": _first_env(*(n for key in keys for n in (f"SERVICE_{key}_CLIENT_SECRET", f"{key}_CLIENT_SECRET"))),
        "org_id": _first_env(*(n for key in keys for n in (f"SERVICE_{key}_ORG_ID", f"{key}_ORG_ID"))),
        "auth": _first_env(*(n for key in keys for n in (f"SERVICE_{key}_AUTH", f"{key}_AUTH"))) or "",
        "notes": _first_env(*(n for key in keys for n in (f"SERVICE_{key}_NOTES", f"{key}_NOTES"))) or "",
    }
    has_browser = bool(creds["login"] and creds["password"])
    has_api = bool(creds["token"])
    has_oauth = bool(creds["client_id"] and creds["client_secret"] and creds["org_id"])
    has_session = creds["auth"].lower() == "session" and bool(creds["url"])
    if not (has_browser or has_api or has_oauth or has_session):
        return None
    return creds


def _has_api_creds(creds: dict) -> bool:
    """True if `creds` carry credentials for an API-based (non-browser) plugin.

    Covers both a static bearer TOKEN and an OAuth Server-to-Server triple
    (CLIENT_ID + CLIENT_SECRET + ORG_ID). API plugins don't need a URL.
    """
    if creds.get("token"):
        return True
    return bool(creds.get("client_id") and creds.get("client_secret") and creds.get("org_id"))


def _capture_shots(context, shot_root: Path, row: dict, outcome: str) -> list[str]:
    svc = _slug(row["service"]).removeprefix("svc_") or "unknown"
    tag = re.sub(r"\W+", "_", outcome).strip("_")[:40] or "none"

    # Persistent context is shared across services; screenshot only the page
    # this plugin actually used. API-only plugins (e.g. Slack) never set a
    # page on the context — for those we emit no screenshot rather than
    # snapping an unrelated service's window.
    service_page = getattr(context, f"_page_{svc}", None)
    if not service_page or service_page.is_closed():
        return []
    pages = [service_page]

    saved: list[str] = []
    for i, page in enumerate(pages):
        name = f"{row['task']['gid']}_{svc}_{i}_{tag}.png"
        path = shot_root / name
        try:
            page.screenshot(path=str(path), full_page=True)
            saved.append(str(path))
        except Exception as e:  # noqa: BLE001
            print(f"    (screenshot {name} failed: {e})")
    return saved


def _missing_creds_fields(service: str) -> list[str]:
    """Which required env-var groups are missing for `service`.

    Returns the names of the missing browser-flow vars (URL/LOGIN/PASSWORD).
    If a `_TOKEN` is present, treats credentials as satisfied via API mode
    and returns an empty list.
    """
    keys = _env_key_candidates(service)
    # A plugin can declare exactly which fields it needs (API-mode plugins want
    # LOGIN/TOKEN/… rather than the browser triple).
    declared = required_env(service)
    if declared:
        return [
            f
            for f in declared
            if not _first_env(*(n for key in keys for n in (f"SERVICE_{key}_{f}", f"{key}_{f}")))
        ]
    if _first_env(*(n for key in keys for n in (f"SERVICE_{key}_TOKEN", f"{key}_TOKEN"))):
        return []
    # OAuth Server-to-Server mode: if any of the triple is set, treat this as
    # an OAuth service and report whichever of the three are missing.
    oauth = {
        f: _first_env(*(n for key in keys for n in (f"SERVICE_{key}_{f}", f"{key}_{f}")))
        for f in ("CLIENT_ID", "CLIENT_SECRET", "ORG_ID")
    }
    if any(oauth.values()):
        return [f for f, v in oauth.items() if not v]
    # Session-auth mode only requires URL — LOGIN/PASSWORD are intentionally
    # omitted (auth handled by the persistent browser profile).
    auth = _first_env(
        *(n for key in keys for n in (f"SERVICE_{key}_AUTH", f"{key}_AUTH"))
    )
    if auth and auth.lower() == "session":
        if not _first_env(*(n for key in keys for n in (f"SERVICE_{key}_URL", f"{key}_URL"))):
            return ["URL"]
        return []
    fields = ("URL", "LOGIN", "PASSWORD")
    missing = []
    for f in fields:
        if not _first_env(*(n for key in keys for n in (f"SERVICE_{key}_{f}", f"{key}_{f}"))):
            missing.append(f)
    return missing


def print_plan(plan: list[dict], date: str) -> None:
    """Group the plan by service, mark ready vs blocked, list missing env keys."""
    by_service: dict[str, list[dict]] = {}
    for row in plan:
        by_service.setdefault(row["service"], []).append(row)

    total = len(plan)
    print(f"\n=== Plan for {date} - {total} task(s) across {len(by_service)} service(s) ===")

    for service in sorted(by_service):
        rows = by_service[service]
        n = len(rows)
        statuses = {r["status"] for r in rows}
        # Aliased services (Adobe/Unity/Syncsketch variants) read their creds
        # from the canonical short key — name that one in the hint, not the
        # long slug of the Asana service string.
        key = _env_key_candidates(service)[-1]

        if statuses == {"ready"}:
            tag = "[OK]   ready"
        elif "missing-credentials" in statuses:
            missing = _missing_creds_fields(service)
            keys = ", ".join(f"{key}_{f}" for f in missing) if missing else "<unknown>"
            tag = f"[SKIP] missing in .env: {keys}"
        elif "missing-url" in statuses:
            tag = f"[SKIP] missing in .env: {key}_URL"
        else:
            tag = f"[?]    {', '.join(sorted(statuses))}"

        print(f"\n  {service}  -  {n} task(s)  -  {tag}")
        for row in rows:
            print(f"    * {row['target']:<45}  ({row['task']['gid']})")


def _comment_after_outcome(
    row: dict, outcome: str, shots: list[str], complete: bool = True
) -> str | None:
    """Upload proof screenshots, comment, and (on success) mark the Asana task
    complete.

    Returns None on success, or a compact error string if any Asana call failed.
    `complete=False` disables marking the task done (--no-complete).
    """
    if outcome not in ("deactivated", "already-deactivated", "user-not-found"):
        return None
    gid = row["task"]["gid"]

    try:
        for shot in shots:
            upload_task_attachment(gid, shot)

        shot_names = [Path(s).name for s in shots]
        if outcome == "deactivated":
            headline = "Deactivation completed by automation."
        elif outcome == "already-deactivated":
            headline = "User was already deactivated in the service."
        else:
            headline = f"User not found in {row['service']}."
        lines = [
            headline,
            f"Service: {row['service']}",
            f"User: {row['target']}",
            f"Result: {outcome}",
        ]
        if shot_names:
            lines.append("Screenshot(s) attached:")
            lines.extend(f"- {name}" for name in shot_names)
        else:
            lines.append("Screenshot: not available for this service.")

        will_complete = complete and outcome in COMPLETABLE_OUTCOMES
        if will_complete:
            lines.append("Task marked complete by automation.")

        create_task_comment(gid, "\n".join(lines))

        if will_complete:
            complete_task(gid)
        return None
    except AsanaError as e:
        return str(e)


def main() -> int:
    make_stdout_safe()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", required=True, help="Due date in YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true", help="Print plan and exit")
    ap.add_argument("--headless", action="store_true", help="Run browser headless")
    ap.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    ap.add_argument(
        "--screenshot-dir",
        default="screenshots",
        help="Where to save per-task screenshots (default: ./screenshots)",
    )
    ap.add_argument(
        "--find-only",
        action="store_true",
        help="Plugins should locate the target user and stop (no destructive actions)",
    )
    ap.add_argument(
        "--service",
        action="append",
        default=[],
        metavar="NAME",
        help="Process only tasks for service(s) matching NAME (case-insensitive substring). Repeatable.",
    )
    ap.add_argument(
        "--target",
        action="append",
        default=[],
        metavar="ID",
        help="Process only tasks whose target (email/identifier) matches ID (case-insensitive substring). Repeatable.",
    )
    ap.add_argument(
        "--include-completed",
        action="store_true",
        help="Include Asana tasks already marked complete (default: only open tasks)",
    )
    ap.add_argument(
        "--no-complete",
        action="store_true",
        help="Do NOT mark Asana tasks complete on success (default: mark complete)",
    )
    args = ap.parse_args()
    if args.find_only:
        os.environ["DEACTIVATE_FIND_ONLY"] = "1"

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
        print(f"error: --date must be YYYY-MM-DD, got {args.date!r}", file=sys.stderr)
        return 2

    try:
        load_secrets()
    except SecretsError as e:
        print(f"secrets error: {e}", file=sys.stderr)
        return 2

    try:
        tasks = fetch_tasks_due_on(args.date, include_completed=args.include_completed)
    except AsanaError as e:
        print(f"asana error: {e}", file=sys.stderr)
        return 1

    matching = [t for t in tasks if t["name"].startswith(TITLE_PREFIX)]
    if not matching:
        print(f"no matching tasks for {args.date}")
        return 0

    # Normalize punctuation so a filter like "slack workspace redbark2" matches
    # the Asana service name "Slack (Workspace Redbark2)" (parentheses, dots,
    # etc. all collapse to single spaces on both sides).
    def _norm(s: str) -> str:
        return re.sub(r"\W+", " ", s.lower(), flags=re.UNICODE).strip()

    service_filters = [_norm(s) for s in args.service]
    target_filters = [s.lower() for s in args.target]

    plan: list[dict[str, Any]] = []
    for t in matching:
        service, target = parse_task(t)
        if service_filters and not any(f in _norm(service) for f in service_filters):
            continue
        if target_filters and not any(f in target.lower() for f in target_filters):
            continue
        creds = load_creds(service)
        if not creds or _missing_creds_fields(service):
            status = "missing-credentials"
        elif not _has_api_creds(creds) and not creds.get("url"):
            # Browser-mode plugin needs a URL; API-mode (token/OAuth) doesn't.
            status = "missing-url"
        else:
            status = "ready"
        plan.append({
            "task": t,
            "service": service,
            "target": target,
            "creds": creds,
            "status": status,
        })

    if not plan:
        if service_filters:
            print(
                f"no tasks for {args.date} match service filter "
                f"{args.service!r}"
            )
        else:
            print(f"no matching tasks for {args.date}")
        return 0

    print_plan(plan, args.date)
    if service_filters:
        print(f"  (filtered to services matching: {', '.join(args.service)})")

    if args.dry_run:
        return 0

    if not args.yes:
        ans = input("\nproceed? [y/N] ").strip().lower()
        if ans != "y":
            print("aborted")
            return 0

    from playwright.sync_api import sync_playwright

    shot_root = Path(args.screenshot_dir) / args.date
    shot_root.mkdir(parents=True, exist_ok=True)

    # Bucket results: unready tasks resolve immediately; ready tasks are grouped
    # by service so we can share one logged-in browser context per service.
    outcomes: dict[str, str] = {}
    shots_by_gid: dict[str, list[str]] = {}
    comment_errors: dict[str, str] = {}
    ready_by_service: dict[str, list[dict[str, Any]]] = {}

    for row in plan:
        gid = row["task"]["gid"]
        if not row["creds"] or row["status"] == "missing-credentials":
            outcomes[gid] = "missing-credentials"
            continue
        # Browser-mode plugins need a URL; API-mode (token/OAuth) plugins don't.
        if not _has_api_creds(row["creds"]) and not row["creds"].get("url"):
            outcomes[gid] = "missing-url"
            continue
        ready_by_service.setdefault(row["service"], []).append(row)

    with sync_playwright() as p:
        # Each service runs in its OWN persistent context (separate window +
        # cookie jar under .browser_profiles/<slug>), opened and closed one at
        # a time. This isolates services from each other (no leftover modal /
        # cookie bleed) while keeping per-service sessions persistent. Profiles
        # are seeded from the legacy shared .browser_profile on first use.
        for service, rows in ready_by_service.items():
            slug = _slug(service).removeprefix("svc_") or "unknown"
            context = p.chromium.launch_persistent_context(
                user_data_dir=service_profile_dir(slug),
                headless=args.headless,
            )
            handler = get_handler(service)
            try:
                for row in rows:
                    gid = row["task"]["gid"]
                    target = row["target"]
                    print(f"\n== {service}: deactivating {target} ==")
                    outcome = "failed: unknown"
                    try:
                        outcome = handler(context, row["creds"], target)
                    except Exception as e:  # noqa: BLE001
                        outcome = f"failed: {type(e).__name__}: {e}"
                    outcomes[gid] = outcome
                    shots_by_gid[gid] = _capture_shots(context, shot_root, row, outcome)
                    comment_error = _comment_after_outcome(
                        row, outcome, shots_by_gid[gid], complete=not args.no_complete
                    )
                    if comment_error:
                        comment_errors[gid] = comment_error
            finally:
                context.close()

    # Reassemble results in original plan order.
    results: list[dict[str, Any]] = []
    for row in plan:
        gid = row["task"]["gid"]
        results.append({
            **row,
            "outcome": outcomes.get(gid, "unknown"),
            "shots": shots_by_gid.get(gid, []),
            "comment_error": comment_errors.get(gid),
        })

    machine_log = bool(os.environ.get("ACTION_LOG_STREAM"))
    print("\n=== summary ===")
    for r in results:
        print(
            f"  {r['task']['gid']:<18} "
            f"{r['service']:<18} "
            f"{r['target']:<40} "
            f"{r['outcome']}"
        )
        for shot in r.get("shots", []):
            print(f"    screenshot: {shot}")
        if r.get("comment_error"):
            print(f"    asana-comment-error: {r['comment_error']}")
        if machine_log:
            # Pipe-delimited, parsed by the monitor's action log. Kept off
            # normal terminal runs so output stays clean. 6th field = first
            # screenshot path (for the deactivation log).
            shot = (r.get("shots") or [""])[0]
            print(
                f"RESULT|{r['task']['gid']}|{r['service']}|"
                f"{r['target']}|{r['outcome']}|{shot}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
