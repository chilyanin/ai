"""Probe primitives: step results and the ProbeContext every probe drives.

A probe (uicheck/probes.py) is a plain function that walks one service the
way the deactivate/grant plugin would, calling `ctx.check(key, goal, ...)` for
every CSS selector the plugin depends on. `check` evaluates the selector that
is currently in force for (slug, key) - code default or uicheck/overrides.json
entry - on the live page and, if nothing matches, hands the page to the
repairer (uicheck/repair.py), verifies its proposal with Playwright, and only
then records it as the new override.

Nothing in here clicks a confirm/submit control. Probes may open menus and
dialogs (and close them with Escape); the destructive step is checked for
presence only.
"""
from __future__ import annotations

import importlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from services import _selectors as S

# Buttons a probe must never click, whatever a repairer proposes.
DESTRUCTIVE_RE = re.compile(
    r"^\s*(remove|delete|deactivate|confirm|yes|send|invite|submit|save|ok)\b", re.I
)

STATUS_ORDER = ("error", "failed", "repaired", "warn", "skipped", "ok")


@dataclass
class StepResult:
    service: str
    step: str
    status: str  # ok | warn | repaired | failed | skipped | error
    detail: str = ""
    key: str | None = None
    selector: str | None = None
    screenshot: str | None = None
    duration: float = 0.0
    diff: dict[str, list[str]] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "step": self.step,
            "status": self.status,
            "detail": self.detail,
            "key": self.key,
            "selector": self.selector,
            "screenshot": self.screenshot,
            "duration": round(self.duration, 2),
            "diff": self.diff,
        }


class ProbeAbort(Exception):
    """Raised when a step fails in a way that makes the following steps moot.

    `soft=True` means nothing is broken - the canary just is not in the
    service (already removed, or the Asana task names a different address),
    so the member-row steps were skipped rather than failed.
    """

    def __init__(self, msg: str, *, soft: bool = False):
        super().__init__(msg)
        self.soft = soft


@dataclass
class ProbeContext:
    slug: str                    # profile / page slug (e.g. "skills_base_programmers")
    service: str                 # display name for the report
    page: Any                    # Playwright Page
    context: Any                 # Playwright BrowserContext
    creds: dict
    canary: str
    # "deactivate": the canary is a member being offboarded today -> the full
    #   row / menu / remove-item path is probed.
    # "grant": the canary is being invited today and is NOT in the service
    #   yet -> only login / list / search / invite steps run.
    canary_kind: str = "deactivate"
    date: str = ""
    sel_slug: str | None = None  # registry slug when it differs from `slug`
    repairer: Any = None         # uicheck.repair.Repairer or None
    fp_store: Any = None         # uicheck.fingerprint.Store or None
    shot_dir: Path | None = None
    cdp_url: str | None = None
    plugin_modules: list = field(default_factory=list)
    results: list[StepResult] = field(default_factory=list)
    log: Callable[[str], None] = print

    # ------------------------------------------------------------------ utils
    @property
    def reg(self) -> str:
        return self.sel_slug or self.slug

    def current(self, key: str):
        """Selector currently in force for (reg, key), re-read from disk."""
        default = S.default_for(self.reg, key)
        if default is None:
            raise KeyError(f"{self.reg}.{key} is not a registered selector")
        return S.sel(self.reg, key, default)

    def _css(self, value, *, must_contain: str | None, extra: str | None) -> str:
        if isinstance(value, list):
            value = ", ".join(value)
        if extra:
            value = f"{value}, {extra}"
        if S.has_placeholder(value):
            value = S.fill_target(value, self.canary)
        if must_contain:
            value = S.with_text(value, must_contain)
        return value

    def _shot(self, name: str) -> str | None:
        if not self.shot_dir:
            return None
        try:
            self.shot_dir.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"\W+", "_", f"{self.slug}_{name}").strip("_")
            path = self.shot_dir / f"{safe}.png"
            self.page.screenshot(path=str(path), full_page=False)
            return str(path)
        except Exception:  # noqa: BLE001
            return None

    def _record(self, r: StepResult) -> StepResult:
        self.results.append(r)
        tag = r.status.upper()
        extra = f"  [{r.key}]" if r.key else ""
        self.log(f"    {tag:<9} {r.step}{extra}  {r.detail}".rstrip())
        return r

    def _reload_plugins(self) -> None:
        """Re-evaluate plugin module constants after an override was written."""
        for mod in self.plugin_modules:
            try:
                importlib.reload(mod)
            except Exception as e:  # noqa: BLE001
                self.log(f"    (reload {mod.__name__} failed: {type(e).__name__}: {e})")

    def _fingerprint(self, step: str, result: StepResult) -> None:
        if not self.fp_store:
            return
        try:
            diff = self.fp_store.record(self.slug, step, self.page)
        except Exception:  # noqa: BLE001
            return
        if diff and (diff["added"] or diff["removed"]):
            result.diff = diff
            n_add, n_rem = len(diff["added"]), len(diff["removed"])
            result.detail = (result.detail + f"  ui-diff +{n_add}/-{n_rem}").strip()

    # ---------------------------------------------------------------- evaluate
    @staticmethod
    def _visible_matches(loc, limit: int = 12) -> tuple[int, Any]:
        """(number of visible matches, first visible locator or None)."""
        try:
            n = loc.count()
        except Exception:
            return 0, None
        first = None
        visible = 0
        for i in range(min(n, limit)):
            cand = loc.nth(i)
            try:
                if cand.is_visible():
                    visible += 1
                    if first is None:
                        first = cand
            except Exception:
                continue
        return visible, first

    def _evaluate(self, css: str, *, scope, visible: bool, min_count: int, timeout_s: float):
        """Poll until `css` has >= min_count (visible) matches. Returns (ok, count, first)."""
        root = scope if scope is not None else self.page
        deadline = time.time() + timeout_s
        while True:
            loc = root.locator(css)
            if visible:
                n, first = self._visible_matches(loc)
            else:
                try:
                    n = loc.count()
                except Exception:
                    n = 0
                first = loc.first if n else None
            if n >= min_count:
                return True, n, first
            if time.time() >= deadline:
                return False, n, first
            time.sleep(0.3)

    # ------------------------------------------------------------------- steps
    def check(
        self,
        key: str,
        goal: str,
        *,
        scope=None,
        hints: tuple[str, ...] = (),
        must_contain: str | None = None,
        extra: str | None = None,
        visible: bool = True,
        min_count: int = 1,
        timeout_s: float = 6.0,
        alt: Callable[[], Any] | None = None,
        abort: bool = True,
        step: str | None = None,
        kind: str = "element",
    ):
        """Verify the selector registered as `key` matches on the live page.

        Returns the first matching Locator (or the alt locator) so the probe
        can keep walking (click the kebab, scope the dialog, ...). On failure
        a repair is attempted (see module docstring); if that fails too the
        step is recorded as failed and, with `abort=True`, ProbeAbort is raised.
        """
        step = step or key
        t0 = time.time()
        value = self.current(key)
        css = self._css(value, must_contain=must_contain, extra=extra)
        ok, n, first = self._evaluate(css, scope=scope, visible=visible, min_count=min_count, timeout_s=timeout_s)
        alt_loc = None
        if not ok and alt is not None:
            try:
                alt_loc = alt()
                a_n, a_first = self._visible_matches(alt_loc) if visible else (alt_loc.count(), alt_loc.first)
                if a_n >= 1:
                    alt_loc = a_first if visible else alt_loc.first
                else:
                    alt_loc = None
            except Exception:
                alt_loc = None

        if ok:
            r = StepResult(self.service, step, "ok", f"{n} match(es)", key=key, selector=css, duration=time.time() - t0)
            if S.is_overridden(self.reg, key):
                r.detail += "  (override in force)"
                try:
                    S.mark_verified(self.reg, key)
                except Exception:
                    pass
            self._fingerprint(step, r)
            self._record(r)
            return first

        # ---- failure path ------------------------------------------------
        # A row check whose text is nowhere on the page is not UI drift: the
        # canary simply is not listed (removed earlier, different address).
        if must_contain and not self._body_has(must_contain):
            r = StepResult(self.service, step, "skipped", f"canary {must_contain} not present in the list (user-not-found) - not a selector problem", key=key, selector=css, screenshot=self._shot(step), duration=time.time() - t0)
            self._record(r)
            raise ProbeAbort(f"{step}: canary not listed", soft=True)

        shot = self._shot(step)
        detail = f"no match for {css!r}"

        # 1. An override that stopped matching while the code default works
        #    again: retire the override instead of inventing a new one.
        if S.is_overridden(self.reg, key):
            default = S.default_for(self.reg, key)
            d_css = self._css(default, must_contain=must_contain, extra=extra)
            d_ok, d_n, d_first = self._evaluate(d_css, scope=scope, visible=visible, min_count=min_count, timeout_s=2.0)
            if d_ok:
                S.reset_to_default(self.reg, key, reason="override stopped matching; code default works again")
                self._reload_plugins()
                r = StepResult(self.service, step, "repaired", f"override retired, code default matches ({d_n})", key=key, selector=d_css, screenshot=shot, duration=time.time() - t0)
                self._fingerprint(step, r)
                self._record(r)
                return d_first

        # 2. Ask the repairer for a replacement and verify it before saving.
        if self.repairer is not None:
            proposal = self._repair(key, goal, hints=hints, scope=scope, must_contain=must_contain, visible=visible, min_count=min_count, old_css=css, kind=kind)
            if proposal:
                new_value, new_first, src, why = proposal
                S.set_override(self.reg, key, new_value, source=src)
                self._reload_plugins()
                r = StepResult(self.service, step, "repaired", f"{why}; override saved ({src})", key=key, selector=new_value, screenshot=shot, duration=time.time() - t0)
                self._fingerprint(step, r)
                self._record(r)
                return new_first
            detail += "; repair found no verified replacement"
        else:
            detail += "; repair disabled"

        if alt_loc is not None:
            # Plugin would still work through its role/text locator - the CSS
            # fallback is stale, which is worth a warning, not a failure.
            r = StepResult(self.service, step, "warn", f"css fallback stale ({detail}); role/text locator still works", key=key, selector=css, screenshot=shot, duration=time.time() - t0)
            self._record(r)
            return alt_loc

        r = StepResult(self.service, step, "failed", detail, key=key, selector=css, screenshot=shot, duration=time.time() - t0)
        self._record(r)
        if abort:
            raise ProbeAbort(f"{step}: {detail}")
        return None

    def _repair(self, key, goal, *, hints, scope, must_contain, visible, min_count, old_css, kind):
        from uicheck import candidates as C

        root = scope if scope is not None else self.page
        container = bool(must_contain)
        try:
            cands = C.collect(root, contains_text=must_contain, container=container)
        except Exception as e:  # noqa: BLE001
            self.log(f"    (candidate extraction failed: {type(e).__name__}: {e})")
            return None
        if not cands:
            self.log("    (no candidates on page)")
            return None
        try:
            choice = self.repairer.choose(
                service=self.service,
                step=key,
                goal=goal,
                hints=hints,
                old_selector=old_css,
                candidates=cands,
                url=self.page.url,
                title=self._title(),
                kind=kind,
            )
        except Exception as e:  # noqa: BLE001
            self.log(f"    (repairer error: {type(e).__name__}: {e})")
            return None
        if not choice:
            return None
        idx, confidence, reason, src = choice
        if idx < 0 or idx >= len(cands):
            return None
        cand = cands[idx]
        proposed = cand["generic"] if container else cand["uniq"]
        if not proposed:
            return None
        if (
            kind == "element"
            and not container
            and DESTRUCTIVE_RE.match(cand.get("text") or "")
            and not re.search(r"(button|item|submit)$", key)
        ):
            # Never let a repair steer a non-control key (search, row, dialog,
            # ...) at a destructive-looking control. Keys that *name* a control
            # (remove_item, invite_button, ...) are exempt - the probe only
            # checks their presence anyway.
            self.log(f"    (rejected destructive candidate for {key}: {cand.get('text')!r})")
            return None
        # Template keys keep their {target} placeholder.
        default = S.default_for(self.reg, key)
        new_value: Any = proposed
        if S.has_placeholder(default) and must_contain is None:
            # The candidate selector is concrete (it carries the canary); turn
            # the canary back into the placeholder so the override generalises.
            if self.canary in proposed:
                new_value = proposed.replace(self.canary, "{target}")
            else:
                self.log("    (template key: candidate does not carry the canary - cannot generalise)")
                return None
        if isinstance(default, list):
            new_value = [new_value]
        css = self._css(new_value, must_contain=must_contain, extra=None)
        ok, n, first = self._evaluate(css, scope=scope, visible=visible, min_count=min_count, timeout_s=2.0)
        if not ok:
            self.log(f"    (proposal {proposed!r} did not verify: {n} match(es))")
            return None
        if not container and n > 1 and min_count == 1:
            self.log(f"    (proposal {proposed!r} is ambiguous: {n} matches)")
            return None
        return new_value, first, src, f"replacement {proposed!r} verified ({n} match(es), confidence {confidence:.2f}: {reason})"

    def _title(self) -> str:
        try:
            return self.page.title()
        except Exception:
            return ""

    def _body_has(self, text: str) -> bool:
        try:
            return text.lower() in self.page.locator("body").inner_text(timeout=3_000).lower()
        except Exception:
            return False

    @property
    def has_member(self) -> bool:
        return self.canary_kind == "deactivate"

    def require_member(self, *keys: str) -> bool:
        """True if the member-row path may run; otherwise skip `keys` and return False."""
        if self.has_member:
            return True
        reason = (
            f"no 'Удалить из' task for {self.date or 'today'} - canary {self.canary} comes from a "
            "'Доступ к' task and is not in the service yet; member-row steps skipped"
        )
        for k in keys:
            self.skip(k, reason, key=k)
        return False

    def state(self, step: str, ok: bool, goal: str, *, detail: str = "", abort: bool = True) -> bool:
        """Record a non-selector condition (login landed, list rendered, ...)."""
        if ok:
            r = StepResult(self.service, step, "ok", detail or goal)
            self._fingerprint(step, r)
            self._record(r)
            return True
        r = StepResult(self.service, step, "failed", detail or f"not satisfied: {goal}", screenshot=self._shot(step))
        self._record(r)
        if abort:
            raise ProbeAbort(f"{step}: {r.detail}")
        return False

    def skip(self, step: str, reason: str, *, key: str | None = None) -> None:
        self._record(StepResult(self.service, step, "skipped", reason, key=key))

    def nav(self, step: str, goal: str, *, reached: Callable[[], bool], key: str, abort: bool = True) -> bool:
        """Navigation step with agent-based repair.

        If `reached()` is false, ask the repairer's browser-use agent (attached
        over CDP) to reach `goal` without changing anything; when it lands on a
        page where `reached()` holds, that URL is saved as the override for
        `key` (e.g. figma.people_url) so the plugin can go there directly.
        """
        if reached():
            r = StepResult(self.service, step, "ok", goal, key=key)
            self._fingerprint(step, r)
            self._record(r)
            return True
        shot = self._shot(step)
        if self.repairer is not None and self.cdp_url:
            try:
                url = self.repairer.navigate(cdp_url=self.cdp_url, goal=goal, start_url=self.page.url)
            except Exception as e:  # noqa: BLE001
                self.log(f"    (nav agent error: {type(e).__name__}: {e})")
                url = None
            if url:
                try:
                    self.page.goto(url)
                    self.page.wait_for_load_state("networkidle", timeout=5_000)
                except Exception:
                    pass
                if reached():
                    S.set_override(self.reg, key, url, source=f"agent:{self.repairer.name}")
                    self._reload_plugins()
                    r = StepResult(self.service, step, "repaired", f"agent reached {url}; saved as {key}", key=key, selector=url, screenshot=shot)
                    self._fingerprint(step, r)
                    self._record(r)
                    return True
                self.log(f"    (agent ended on {url} but goal not reached)")
        r = StepResult(self.service, step, "failed", f"could not reach: {goal}", key=key, screenshot=shot)
        self._record(r)
        if abort:
            raise ProbeAbort(r.detail)
        return False

    def escape(self, times: int = 2) -> None:
        for _ in range(times):
            try:
                self.page.keyboard.press("Escape")
            except Exception:
                break
            time.sleep(0.2)
