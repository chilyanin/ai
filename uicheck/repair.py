"""Repair engines: pick a replacement element, or navigate to a lost page.

Two engines share one interface (`choose` / `navigate` / `name`):

* `HeuristicRepairer` - no LLM. Scores candidates against the step's hints
  (text / aria-label / data-qa / placeholder) and the goal words. Always
  available; also the fallback when the LLM call fails.
* `LLMRepairer` - browser-use chat model (OpenAI / Google / Anthropic) asked
  to pick the candidate index as JSON; `navigate` runs a browser-use `Agent`
  attached to the already-open Chromium over CDP with a read-only mandate.

Provider selection (`UICHECK_LLM`): `auto` (default) reads
uicheck/state/llm_choice.json or runs `bench()` once when both OPENAI_API_KEY
and GOOGLE_API_KEY are present and keeps the faster provider; otherwise the
only configured key wins. `openai[:model]`, `google[:model]`,
`anthropic[:model]` force one; `heuristic` disables the LLM.

Every proposal is verified by Playwright in ProbeContext before it is saved,
so a wrong pick costs one more failed check, never a wrong override.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import statistics
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from uicheck.candidates import describe

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "uicheck" / "state"
CHOICE_FILE = STATE_DIR / "llm_choice.json"

DEFAULT_MODELS = {
    "openai": "gpt-4.1-mini",
    "google": "gemini-2.5-flash",
    "anthropic": "claude-sonnet-4-5",
}
KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}

# browser-use phones home unless told not to, and is chatty on INFO.
os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
os.environ.setdefault("BROWSER_USE_LOGGING_LEVEL", "warning")

_WORD = re.compile(r"[a-zа-я0-9]+", re.I)


def _words(s: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(s or "") if len(w) > 2}


# ---------------------------------------------------------------------------
# Heuristic
# ---------------------------------------------------------------------------
class HeuristicRepairer:
    name = "heuristic"

    def choose(self, *, service, step, goal, hints, old_selector, candidates, url="", title="", kind="element"):
        """Return (index, confidence, reason, source) or None."""
        hint_words = {h.lower() for h in hints}
        goal_words = _words(goal) - {"the", "and", "for", "with", "that", "this", "inside", "button", "input"}
        want_input = any(w in goal.lower() for w in ("input", "search", "filter", "field", "email"))
        want_click = any(w in goal.lower() for w in ("button", "menu", "item", "link", "tab"))
        best, best_score, best_why = -1, 0.0, ""
        for c in candidates:
            # Class names and the generated selector often carry the only
            # stable vocabulary ("member-row", "person-menu"), so they count.
            fields = " ".join(
                str(c.get(k) or "") for k in ("text", "aria", "dataqa", "placeholder", "name", "title", "role", "cls", "generic")
            ).lower()
            score = 0.0
            why = []
            for h in hint_words:
                if h and h in fields:
                    score += 3
                    why.append(f"hint '{h}'")
            overlap = goal_words & _words(fields)
            if overlap:
                score += 0.5 * len(overlap)
                why.append("goal words " + ",".join(sorted(overlap))[:40])
            tag = c.get("tag")
            if want_input and tag in ("input", "textarea"):
                score += 1.5
            if want_click and tag in ("button", "a") or (c.get("role") in ("button", "menuitem", "tab", "link")):
                score += 1.0 if want_click else 0.3
            if c.get("disabled"):
                score -= 1
            if c.get("dataqa"):
                score += 0.5  # stable hooks are better overrides
            # Among equals prefer the smallest element (a row, not its table).
            score -= min(0.4, (c.get("textlen") or 0) / 5000)
            if score > best_score:
                best, best_score, best_why = c["i"], score, "; ".join(why)
        if best < 0 or best_score < 2.0:
            return None
        confidence = min(0.9, 0.3 + best_score / 10)
        return best, confidence, best_why or "best heuristic score", self.name

    def navigate(self, *, cdp_url, goal, start_url):
        return None


# ---------------------------------------------------------------------------
# LLM (browser-use)
# ---------------------------------------------------------------------------
_SYSTEM = (
    "You repair a browser automation that administers SaaS user lists "
    "(find a member row, open its actions menu, locate the Remove/Deactivate "
    "item, open the Invite dialog). One of its CSS selectors stopped matching "
    "after the service changed its UI. You get the step's goal, the old "
    "selector and the interactive elements currently on the page. Pick the "
    "single candidate that fulfils the goal. Answer with JSON only: "
    '{"index": <int, -1 if none fits>, "confidence": <0..1>, "reason": "<short>"}.'
)


_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()


def _get_loop() -> asyncio.AbstractEventLoop:
    """One long-lived event loop on a daemon thread for every async call.

    The caller sits inside Playwright's sync API, which refuses to run while
    an asyncio loop is active in its own thread - so async work goes to a
    separate thread. A single persistent loop (rather than asyncio.run per
    call) keeps the provider SDKs' HTTP clients alive between calls; closing
    a loop under them made google-genai log "Event loop is closed".
    """
    global _loop
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever, name="uicheck-async", daemon=True).start()
            _loop = loop
        return _loop


def _run_async(coro, timeout: float):
    fut = asyncio.run_coroutine_threadsafe(coro, _get_loop())
    try:
        return fut.result(timeout)
    except TimeoutError:
        fut.cancel()
        raise TimeoutError(f"async call exceeded {timeout:.0f}s") from None


def _parse_choice(text: str) -> tuple[int, float, str] | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    try:
        idx = int(data.get("index", -1))
    except (TypeError, ValueError):
        return None
    try:
        conf = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    return idx, max(0.0, min(1.0, conf)), str(data.get("reason", ""))[:200]


def make_chat_model(spec: str):
    """`openai:gpt-4.1-mini` -> browser-use chat model instance."""
    provider, _, model = spec.partition(":")
    provider = provider.strip().lower()
    model = model.strip() or DEFAULT_MODELS.get(provider, "")
    key = os.environ.get(KEY_ENV.get(provider, ""), "")
    if not key:
        raise RuntimeError(f"{KEY_ENV.get(provider, provider)} is not set")
    if provider == "openai":
        from browser_use.llm.openai.chat import ChatOpenAI

        return ChatOpenAI(model=model, api_key=key, temperature=0, reasoning_effort="low")
    if provider == "google":
        from browser_use.llm.google.chat import ChatGoogle

        return ChatGoogle(model=model, api_key=key, temperature=0)
    if provider == "anthropic":
        from browser_use.llm.anthropic.chat import ChatAnthropic

        return ChatAnthropic(model=model, api_key=key, temperature=0)
    raise RuntimeError(f"unknown LLM provider {provider!r} (use openai|google|anthropic|heuristic)")


class LLMRepairer:
    def __init__(self, spec: str, *, llm_timeout: float = 60.0, agent_max_steps: int = 15, log=print):
        self.spec = spec
        self.llm = make_chat_model(spec)
        self.name = f"llm:{spec}"
        self.llm_timeout = llm_timeout
        self.agent_max_steps = agent_max_steps
        self.log = log
        self.fallback = HeuristicRepairer()

    # ------------------------------------------------------------- choose
    def _prompt(self, *, service, step, goal, hints, old_selector, candidates, url, title) -> str:
        lines = [
            f"Service: {service}",
            f"Page: {title!r} {url}",
            f"Step: {step}",
            f"Goal: {goal}",
            f"Hints (words likely on/near the element): {', '.join(hints) or '-'}",
            f"Old selector (no longer matches): {old_selector}",
            "",
            "Candidates:",
        ]
        lines += [describe(c) for c in candidates]
        lines.append("")
        lines.append("Reply with the JSON object only.")
        return "\n".join(lines)

    async def _ask(self, prompt: str) -> str:
        from browser_use.llm.messages import SystemMessage, UserMessage

        res = await self.llm.ainvoke([SystemMessage(content=_SYSTEM), UserMessage(content=prompt)])
        return res.completion if isinstance(res.completion, str) else json.dumps(res.completion)

    def choose(self, *, service, step, goal, hints, old_selector, candidates, url="", title="", kind="element"):
        prompt = self._prompt(service=service, step=step, goal=goal, hints=hints, old_selector=old_selector, candidates=candidates, url=url, title=title)
        try:
            text = _run_async(self._ask(prompt), self.llm_timeout)
        except Exception as e:  # noqa: BLE001
            self.log(f"    (LLM {self.spec} failed: {type(e).__name__}: {e}; falling back to heuristic)")
            return self.fallback.choose(service=service, step=step, goal=goal, hints=hints, old_selector=old_selector, candidates=candidates, url=url, title=title, kind=kind)
        parsed = _parse_choice(text)
        if not parsed:
            self.log(f"    (LLM answer not parseable: {text[:120]!r}; falling back to heuristic)")
            return self.fallback.choose(service=service, step=step, goal=goal, hints=hints, old_selector=old_selector, candidates=candidates, url=url, title=title, kind=kind)
        idx, conf, reason = parsed
        if idx < 0:
            return None
        return idx, conf, reason, self.name

    # ----------------------------------------------------------- navigate
    async def _agent_run(self, cdp_url: str, goal: str, start_url: str) -> str | None:
        from browser_use import Agent, BrowserSession

        task = (
            "You are attached to an admin console that is already signed in. "
            f"Goal: {goal}. Starting page: {start_url}. "
            "Use only navigation: click sidebar/menu/tab links and read pages. "
            "NEVER click Remove, Delete, Deactivate, Invite, Send, Submit, Save, "
            "Confirm or Log out; never fill a form; never open a confirmation dialog. "
            "As soon as the goal page is on screen, finish and report its URL."
        )
        session = BrowserSession(cdp_url=cdp_url)
        agent = Agent(
            task=task,
            llm=self.llm,
            browser_session=session,
            use_vision=True,
            use_judge=False,
            enable_planning=False,
            max_failures=2,
            max_actions_per_step=2,
            enable_signal_handler=False,
        )
        try:
            history = await agent.run(max_steps=self.agent_max_steps)
            urls = [u for u in (history.urls() or []) if u]
            return urls[-1] if urls else None
        finally:
            try:
                await session.stop()
            except Exception:  # noqa: BLE001
                pass

    def navigate(self, *, cdp_url, goal, start_url):
        self.log(f"    nav-repair via browser-use agent ({self.spec}) ...")
        return _run_async(self._agent_run(cdp_url, goal, start_url), timeout=self.agent_max_steps * 60)


# ---------------------------------------------------------------------------
# Provider selection + benchmark
# ---------------------------------------------------------------------------
_BENCH_CANDIDATES = [
    {"i": 0, "tag": "input", "role": "", "type": "search", "text": "", "aria": "", "placeholder": "Filter members", "name": "q", "dataqa": "members-filter", "title": "", "href": "", "disabled": False, "uniq": '[data-qa="members-filter"]', "generic": '[data-qa="members-filter"]'},
    {"i": 1, "tag": "button", "role": "", "type": "", "text": "Invite people", "aria": "", "placeholder": "", "name": "", "dataqa": "invite-btn", "title": "", "href": "", "disabled": False, "uniq": '[data-qa="invite-btn"]', "generic": '[data-qa="invite-btn"]'},
    {"i": 2, "tag": "button", "role": "", "type": "", "text": "", "aria": "More actions", "placeholder": "", "name": "", "dataqa": "row-menu", "title": "", "href": "", "disabled": False, "uniq": '[data-qa="row-menu"]', "generic": '[data-qa="row-menu"]'},
    {"i": 3, "tag": "a", "role": "", "type": "", "text": "Billing", "aria": "", "placeholder": "", "name": "", "dataqa": "", "title": "", "href": "/billing", "disabled": False, "uniq": 'a[href="/billing"]', "generic": 'a[href="/billing"]'},
    {"i": 4, "tag": "input", "role": "", "type": "checkbox", "text": "", "aria": "Select all", "placeholder": "", "name": "", "dataqa": "", "title": "", "href": "", "disabled": False, "uniq": 'input[aria-label="Select all"]', "generic": 'input[type="checkbox"]'},
]
_BENCH_TASKS = [
    ("search", "search/filter input of the members list", ("search", "filter"), 0),
    ("kebab", "the row's more-actions (⋯) menu button", ("more", "actions"), 2),
    ("invite_button", "the button that opens the invite-members dialog", ("invite",), 1),
]


def available_providers() -> list[str]:
    return [p for p, env in KEY_ENV.items() if os.environ.get(env)]


def bench(providers: list[str] | None = None, *, rounds: int = 3, log=print) -> dict[str, Any]:
    """Time each provider on the chooser prompt; persist and return the result."""
    providers = providers or [p for p in ("openai", "google") if p in available_providers()]
    results: dict[str, Any] = {}
    for provider in providers:
        spec = f"{provider}:{DEFAULT_MODELS[provider]}"
        try:
            rep = LLMRepairer(spec, log=log)
        except Exception as e:  # noqa: BLE001
            results[provider] = {"spec": spec, "error": f"{type(e).__name__}: {e}"}
            log(f"  {spec:<28} unavailable: {e}")
            continue
        times: list[float] = []
        correct = 0
        errors: list[str] = []
        for r in range(rounds):
            for step, goal, hints, expected in _BENCH_TASKS:
                prompt = rep._prompt(service="bench", step=step, goal=goal, hints=hints, old_selector='[data-qa="old"]', candidates=_BENCH_CANDIDATES, url="https://example.test/admin", title="Members")
                t0 = time.time()
                try:
                    text = _run_async(rep._ask(prompt), rep.llm_timeout)
                except Exception as e:  # noqa: BLE001
                    msg = f"{type(e).__name__}: {e}"
                    errors.append(msg[:160])
                    continue
                times.append(time.time() - t0)
                parsed = _parse_choice(text)
                if parsed and parsed[0] == expected:
                    correct += 1
        total = rounds * len(_BENCH_TASKS)
        median = statistics.median(times) if times else None
        results[provider] = {
            "spec": spec,
            "answered": f"{len(times)}/{total}",
            "median_s": round(median, 2) if median is not None else None,
            "mean_s": round(statistics.fmean(times), 2) if times else None,
            "correct": f"{correct}/{total}",
            "accuracy": correct / total if total else 0,
            "errors": errors[:3],
        }
        log(
            f"  {spec:<28} answered {len(times)}/{total}  median {results[provider]['median_s']}s  "
            f"mean {results[provider]['mean_s']}s  correct {correct}/{total}"
            + (f"  first error: {errors[0][:90]}" if errors else "")
        )

    ranked = [
        (p, r) for p, r in results.items()
        if r.get("median_s") is not None and r.get("accuracy", 0) >= 0.5
    ]
    ranked.sort(key=lambda pr: pr[1]["median_s"])
    choice = ranked[0][1]["spec"] if ranked else None
    out = {
        "when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "choice": choice,
        "results": results,
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CHOICE_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"  winner: {choice or 'none (no provider answered correctly)'}  -> {CHOICE_FILE}")
    return out


def resolve_spec(setting: str | None, *, log=print) -> str:
    """Turn UICHECK_LLM into a concrete `provider:model` or 'heuristic'."""
    setting = (setting or os.environ.get("UICHECK_LLM") or "auto").strip()
    if setting in ("heuristic", "stub", "none", "off"):
        return "heuristic"
    if setting != "auto":
        provider, _, model = setting.partition(":")
        return f"{provider}:{model or DEFAULT_MODELS.get(provider, '')}"
    avail = available_providers()
    if not avail:
        log("[uicheck] no LLM API key in env - repairs use the heuristic chooser only")
        return "heuristic"
    if CHOICE_FILE.exists():
        try:
            data = json.loads(CHOICE_FILE.read_text(encoding="utf-8"))
            choice = data.get("choice")
            if choice and choice.split(":")[0] in avail:
                return choice
        except (OSError, ValueError):
            pass
    if "openai" in avail and "google" in avail:
        log("[uicheck] both OpenAI and Google keys present - benchmarking once to pick the faster one")
        data = bench(["openai", "google"], log=log)
        if data.get("choice"):
            return data["choice"]
    provider = avail[0]
    return f"{provider}:{DEFAULT_MODELS[provider]}"


def build_repairer(setting: str | None, *, log=print):
    spec = resolve_spec(setting, log=log)
    if spec == "heuristic":
        return HeuristicRepairer()
    try:
        return LLMRepairer(spec, log=log)
    except Exception as e:  # noqa: BLE001
        log(f"[uicheck] LLM {spec} unavailable ({e}); using heuristic repairer")
        return HeuristicRepairer()
