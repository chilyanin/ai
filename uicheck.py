#!/usr/bin/env python3
"""Nightly UI-drift check for the browser-driven service plugins.

Walks every browser-based service the way deactivate.py / grant.py would -
sign in, reach the member list, find a canary user (the admin itself), open
the row menu, confirm the Remove/Deactivate item is listed - and verifies each
CSS selector the plugin relies on. When a service has changed its UI, the
failing selector is repaired on the spot: candidates are read off the live
page, a replacement is chosen (browser-use LLM, or a text heuristic), verified
with Playwright and written to uicheck/overrides.json, which the plugins read
at start-up. Nothing is removed or invited; confirmation dialogs are never
opened.

Usage:
    ./uicheck.py --init-overrides          # write overrides.json from code defaults
    ./uicheck.py --dry-run                 # which services would run, with which creds
    ./uicheck.py --service figma           # one service, headed
    ./uicheck.py --yes --headless          # the nightly run (uicheck-run.bat)
    ./uicheck.py --bench-llm               # time OpenAI vs Google, store the winner
    ./uicheck.py --bench-llm --env-file .env_test   # same, with keys from a dotenv

Exit codes: 0 all green (incl. repaired/warn), 1 unrepaired failures, 2 config.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

from services._common import make_stdout_safe, service_profile_dir

HERE = Path(__file__).resolve().parent
REPORT_DIR = HERE / "uicheck" / "reports"
STATE_DIR = HERE / "uicheck" / "state"


def _norm(s: str) -> str:
    return re.sub(r"\W+", " ", s.lower(), flags=re.UNICODE).strip()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _canary_for(service: str, creds: dict, override: str | None) -> str:
    from deactivate import _env_key_candidates, _first_env

    if override:
        return override
    keys = _env_key_candidates(service)
    env = _first_env(*(n for key in keys for n in (f"SERVICE_{key}_CANARY", f"{key}_CANARY")))
    return env or creds.get("login") or ""


def build_plan(args) -> list[dict]:
    from deactivate import _has_api_creds, _missing_creds_fields, load_creds
    from uicheck.probes import SERVICES

    service_filters = [_norm(s) for s in args.service]
    skip_filters = [_norm(s) for s in args.skip]
    plan = []
    for spec in SERVICES:
        name, slug = spec["name"], spec["slug"]
        if service_filters and not any(f in _norm(name) or f in _norm(slug) for f in service_filters):
            continue
        if skip_filters and any(f in _norm(name) or f in _norm(slug) for f in skip_filters):
            continue
        creds = load_creds(name)
        missing = _missing_creds_fields(name)
        if not creds or missing:
            status = "missing-credentials"
            note = ", ".join(missing) if missing else "no credentials"
        elif _has_api_creds(creds) and not creds.get("url"):
            status = "api-only"
            note = "not a browser plugin"
        elif not creds.get("url"):
            status = "missing-url"
            note = "URL"
        else:
            status = "ready"
            note = ""
        canary = _canary_for(name, creds or {}, args.canary) if creds else ""
        if status == "ready" and not canary:
            status = "missing-canary"
            note = "no LOGIN and no <KEY>_CANARY to search for"
        plan.append({**spec, "creds": creds, "status": status, "note": note, "canary": canary})
    return plan


def print_plan(plan: list[dict]) -> None:
    print(f"\n=== uicheck plan - {len(plan)} service(s) ===")
    for row in plan:
        tag = {"ready": "[OK]  ", "missing-credentials": "[SKIP]", "missing-url": "[SKIP]", "api-only": "[SKIP]", "missing-canary": "[SKIP]"}.get(row["status"], "[?]   ")
        extra = f"  ({row['note']})" if row["note"] else ""
        canary = f"  canary={row['canary']}" if row["status"] == "ready" else ""
        print(f"  {tag} {row['name']:<28} profile={row['slug']}{canary}{extra}")


def run_service(row: dict, *, headless: bool, repairer, fp_store, shot_dir: Path, log) -> list:
    from playwright.sync_api import sync_playwright

    from services._common import get_page
    from uicheck.model import ProbeAbort, ProbeContext, StepResult

    name, slug = row["name"], row["slug"]
    port = _free_port()
    log(f"\n== {name} (profile {slug}) ==")
    results: list = []
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=service_profile_dir(slug),
            headless=headless,
            args=[f"--remote-debugging-port={port}"],
        )

        # Native confirm()/alert() must never be accepted by the probe.
        def _dismiss(dialog):
            try:
                dialog.dismiss()
            except Exception:
                pass

        def _wire(page):
            page.on("dialog", _dismiss)

        for pg in context.pages:
            _wire(pg)
        context.on("page", _wire)

        page = get_page(context, slug)
        _wire(page)
        ctx = ProbeContext(
            slug=slug,
            service=name,
            page=page,
            context=context,
            creds=row["creds"],
            canary=row["canary"],
            sel_slug=row.get("sel_slug"),
            repairer=repairer,
            fp_store=fp_store,
            shot_dir=shot_dir,
            cdp_url=f"http://127.0.0.1:{port}",
            log=log,
        )
        t0 = time.time()
        try:
            row["probe"](ctx)
        except ProbeAbort as e:
            log(f"    -- aborted: {e}")
        except Exception as e:  # noqa: BLE001
            shot = ctx._shot("error")
            ctx.results.append(StepResult(name, "probe", "error", f"{type(e).__name__}: {e}", screenshot=shot))
            log(f"    ERROR     probe crashed: {type(e).__name__}: {e}")
        finally:
            results = list(ctx.results)
            try:
                context.close()
            except Exception:
                pass
        log(f"    ({time.time() - t0:.0f}s)")
    return results


def write_report(all_results: list, plan: list[dict], *, started: datetime, report_dir: Path, llm: str) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    day = started.strftime("%Y-%m-%d")
    stamp = started.strftime("%Y-%m-%d_%H%M")
    data = {
        "started": started.isoformat(timespec="seconds"),
        "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "llm": llm,
        "plan": [{k: v for k, v in r.items() if k in ("name", "slug", "status", "note", "canary")} for r in plan],
        "results": [r.as_dict() for r in all_results],
    }
    json_path = report_dir / f"{stamp}.json"
    json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    by_service: dict[str, list] = {}
    for r in all_results:
        by_service.setdefault(r.service, []).append(r)
    counts = {s: 0 for s in ("ok", "warn", "repaired", "failed", "error", "skipped")}
    for r in all_results:
        counts[r.status] = counts.get(r.status, 0) + 1

    lines = [f"# UI check {day}", ""]
    lines.append(f"Started {started.strftime('%Y-%m-%d %H:%M')} UTC · repairer: `{llm}`")
    lines.append("")
    lines.append("| status | count |")
    lines.append("|---|---|")
    for s in ("failed", "error", "repaired", "warn", "ok", "skipped"):
        lines.append(f"| {s} | {counts.get(s, 0)} |")
    lines.append("")
    not_run = [p for p in plan if p["status"] != "ready"]
    if not_run:
        lines.append("Not run: " + ", ".join(f"{p['name']} ({p['status']}{': ' + p['note'] if p['note'] else ''})" for p in not_run))
        lines.append("")
    for service, rows in by_service.items():
        worst = min(rows, key=lambda r: ("error", "failed", "repaired", "warn", "ok", "skipped").index(r.status) if r.status in ("error", "failed", "repaired", "warn", "ok", "skipped") else 9)
        lines.append(f"## {service} — {worst.status.upper()}")
        lines.append("")
        lines.append("| step | status | key | detail |")
        lines.append("|---|---|---|---|")
        for r in rows:
            detail = r.detail.replace("|", "\\|")
            if r.screenshot:
                detail += f" ([shot]({Path(r.screenshot).name}))"
            lines.append(f"| {r.step} | {r.status} | {r.key or ''} | {detail} |")
        diffs = [r for r in rows if r.diff]
        if diffs:
            lines.append("")
            lines.append("<details><summary>UI diff (structural, informational)</summary>")
            lines.append("")
            for r in diffs:
                for sign, items in (("+", r.diff["added"]), ("-", r.diff["removed"])):
                    for it in items[:15]:
                        lines.append(f"- `{r.step}` {sign} `{it}`")
            lines.append("")
            lines.append("</details>")
        lines.append("")
    repaired = [r for r in all_results if r.status == "repaired"]
    if repaired:
        lines.append("## Overrides written tonight")
        lines.append("")
        for r in repaired:
            lines.append(f"- **{r.service}** `{r.key}` → `{r.selector}` — {r.detail}")
        lines.append("")
    md_path = report_dir / f"{stamp}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    (report_dir / "latest.md").write_text("\n".join(lines), encoding="utf-8")
    return md_path, json_path


def main() -> int:
    make_stdout_safe()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--service", action="append", default=[], metavar="NAME", help="Only services matching NAME (substring, repeatable)")
    ap.add_argument("--skip", action="append", default=[], metavar="NAME", help="Skip services matching NAME (repeatable)")
    ap.add_argument("--headless", action="store_true", help="Run Chromium headless (nightly job)")
    ap.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    ap.add_argument("--dry-run", action="store_true", help="Print the plan and exit")
    ap.add_argument("--list", action="store_true", help="List services and registered selector keys, then exit")
    ap.add_argument("--no-repair", action="store_true", help="Report only - never write overrides")
    ap.add_argument("--canary", metavar="EMAIL", help="Member to search for in every service (default: the admin login / <KEY>_CANARY)")
    ap.add_argument("--llm", metavar="SPEC", help="auto | openai[:model] | google[:model] | anthropic[:model] | heuristic (default: $UICHECK_LLM or auto)")
    ap.add_argument("--bench-llm", action="store_true", help="Benchmark OpenAI vs Google on the chooser prompt and store the faster one")
    ap.add_argument("--init-overrides", action="store_true", help="(Re)write uicheck/overrides.json from the code defaults, keeping live overrides")
    ap.add_argument("--env-file", metavar="PATH", help="Load keys from this dotenv instead of Vault (benchmarks / local tests only)")
    ap.add_argument("--report-dir", default=str(REPORT_DIR), help=f"Where to write reports (default: {REPORT_DIR})")
    args = ap.parse_args()

    if args.init_overrides:
        from services._selectors import OVERRIDES_PATH, dump_defaults

        data = dump_defaults()
        n = sum(len(v) for v in data.values())
        print(f"wrote {n} selector(s) for {len(data)} service(s) -> {OVERRIDES_PATH}")
        return 0

    if args.list:
        from services._selectors import REGISTRY, import_all_plugins, is_overridden
        from uicheck.probes import SERVICES

        import_all_plugins()
        for spec in SERVICES:
            reg = spec.get("sel_slug") or spec["slug"]
            keys = sorted(REGISTRY.get(reg, {}))
            marks = [f"{k}*" if is_overridden(reg, k) else k for k in keys]
            print(f"{spec['name']:<28} [{reg}] {', '.join(marks)}")
        print("(* = override in force)")
        return 0

    # Secrets: Vault as everywhere else; --env-file is for the LLM benchmark on
    # a machine without Vault and never touches the Vault setup.
    if args.env_file:
        from dotenv import load_dotenv

        if not Path(args.env_file).is_file():
            print(f"error: --env-file {args.env_file} not found", file=sys.stderr)
            return 2
        load_dotenv(args.env_file, override=False)
    else:
        from secrets_provider import SecretsError, load_secrets

        try:
            load_secrets()
        except SecretsError as e:
            print(f"secrets error: {e}", file=sys.stderr)
            return 2

    from uicheck import repair as R

    if args.bench_llm:
        avail = R.available_providers()
        if not avail:
            print("error: no OPENAI_API_KEY / GOOGLE_API_KEY / ANTHROPIC_API_KEY in env", file=sys.stderr)
            return 2
        print(f"benchmarking {', '.join(p for p in ('openai', 'google', 'anthropic') if p in avail)} ...")
        R.bench([p for p in ("openai", "google", "anthropic") if p in avail])
        return 0

    plan = build_plan(args)
    if not plan:
        print("no services match the filters")
        return 0
    print_plan(plan)
    ready = [r for r in plan if r["status"] == "ready"]
    if args.dry_run:
        return 0
    if not ready:
        print("nothing to run")
        return 0
    if not args.yes:
        ans = input("\nproceed? [y/N] ").strip().lower()
        if ans != "y":
            print("aborted")
            return 0

    repairer = None if args.no_repair else R.build_repairer(args.llm)
    llm_name = "disabled" if repairer is None else repairer.name
    print(f"repairer: {llm_name}")

    from uicheck.fingerprint import Store

    started = datetime.now(timezone.utc)
    report_dir = Path(args.report_dir)
    shot_dir = report_dir / started.strftime("%Y-%m-%d_%H%M")
    fp_store = Store(STATE_DIR / "fingerprints")

    log_path = HERE / "uicheck.log"

    def log(line: str) -> None:
        print(line)
        try:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"{datetime.now().strftime('%H:%M:%S')} {line}\n")
        except OSError:
            pass

    log(f"===== uicheck {started.isoformat(timespec='seconds')} repairer={llm_name} =====")
    all_results = []
    for row in ready:
        try:
            all_results += run_service(row, headless=args.headless, repairer=repairer, fp_store=fp_store, shot_dir=shot_dir, log=log)
        except Exception as e:  # noqa: BLE001
            from uicheck.model import StepResult

            all_results.append(StepResult(row["name"], "browser", "error", f"{type(e).__name__}: {e}"))
            log(f"    ERROR     browser failed: {type(e).__name__}: {e}")

    md_path, json_path = write_report(all_results, plan, started=started, report_dir=report_dir, llm=llm_name)

    print("\n=== summary ===")
    worst = 0
    for row in ready:
        rows = [r for r in all_results if r.service == row["name"]]
        statuses = [r.status for r in rows]
        if "failed" in statuses or "error" in statuses:
            tag, worst = "FAILED", 1
        elif "repaired" in statuses:
            tag = "REPAIRED"
        elif "warn" in statuses:
            tag = "WARN"
        else:
            tag = "OK"
        bad = [f"{r.step}({r.status})" for r in rows if r.status in ("failed", "error", "repaired", "warn")]
        print(f"  {row['name']:<28} {tag:<9} {', '.join(bad)}")
    print(f"  report: {md_path}")
    log(f"===== done rc={worst} report={md_path.name}")
    return worst


if __name__ == "__main__":
    sys.exit(main())
