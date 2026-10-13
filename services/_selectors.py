"""Selector registry with file-backed overrides (uicheck/overrides.json).

Plugins declare every CSS selector they depend on through `sel()`:

    SEARCH = sel("slack_workspace_redbark2", "search",
                 '[data-qa="workspace-members__table-header-search_input"]')

The call registers the code default under (slug, key) and returns the value
from `uicheck/overrides.json` when the file has one, otherwise the default.
That file is the operational source of truth: the nightly `uicheck.py` run
verifies every key against the live service and, when a service ships a new
UI, writes a verified replacement there - no code edit, no redeploy. Removing
an entry (or running `uicheck.py --init-overrides`) falls back to the code
default.

Only CSS selectors go through here. Role/regex locators
(`page.get_by_role("button", name=re.compile(...))`) are text-based and
robust enough to stay hard-coded.

Row selectors are *container* selectors ("tr, [role=row]"); `with_text()`
turns them into the per-target form the plugins use ("tr:has-text(x),
[role=row]:has-text(x)"). Keys whose default contains `{target}` are
templates filled by `fill_target()`.

File format (one entry per key):

    {"figma": {"search": {"selector": "...", "default": "...",
                          "source": "code|llm:openai/gpt-4.1-mini|heuristic|agent",
                          "since": "2026-10-13", "verified": "2026-10-14",
                          "replaced": "<previous selector>"}}}
"""
from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
OVERRIDES_PATH = Path(os.environ.get("UICHECK_OVERRIDES") or ROOT / "uicheck" / "overrides.json")

# slug -> key -> code default (str or list[str]). Filled as plugins import.
REGISTRY: dict[str, dict[str, Any]] = {}

_cache: dict[str, Any] | None = None
_cache_mtime: float | None = None


def _load() -> dict[str, Any]:
    """Overrides file content, re-read when its mtime changes."""
    global _cache, _cache_mtime
    try:
        mtime = OVERRIDES_PATH.stat().st_mtime
    except OSError:
        _cache, _cache_mtime = {}, None
        return _cache
    if _cache is not None and _cache_mtime == mtime:
        return _cache
    try:
        data = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
        _cache = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        _cache = {}
    _cache_mtime = mtime
    return _cache


def _save(data: dict[str, Any]) -> None:
    global _cache, _cache_mtime
    OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OVERRIDES_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, OVERRIDES_PATH)
    _cache = None
    _cache_mtime = None


def sel(slug: str, key: str, default):
    """Register `default` for (slug, key) and return the effective selector."""
    REGISTRY.setdefault(slug, {})[key] = default
    entry = _load().get(slug, {}).get(key)
    if not isinstance(entry, dict):
        return default
    value = entry.get("selector")
    if value in (None, ""):
        return default
    if isinstance(default, list):
        if isinstance(value, str):
            return [value]
        return list(value)
    if isinstance(value, list):
        return ", ".join(value)
    return value


def entry(slug: str, key: str) -> dict[str, Any] | None:
    e = _load().get(slug, {}).get(key)
    return dict(e) if isinstance(e, dict) else None


def default_for(slug: str, key: str):
    return REGISTRY.get(slug, {}).get(key)


def is_overridden(slug: str, key: str) -> bool:
    e = entry(slug, key)
    return bool(e) and e.get("selector") not in (None, "") and e.get("selector") != default_for(slug, key)


def set_override(slug: str, key: str, selector, *, source: str, verified: str | None = None) -> None:
    """Record a verified replacement selector for (slug, key)."""
    data = _load()
    data = json.loads(json.dumps(data))  # detach from cache
    svc = data.setdefault(slug, {})
    old = svc.get(key) or {}
    today = date.today().isoformat()
    svc[key] = {
        "selector": selector,
        "default": default_for(slug, key) if default_for(slug, key) is not None else old.get("default"),
        "source": source,
        "since": today,
        "verified": verified or today,
        "replaced": old.get("selector"),
    }
    _save(data)


def mark_verified(slug: str, key: str, when: str | None = None) -> None:
    data = json.loads(json.dumps(_load()))
    e = data.get(slug, {}).get(key)
    if isinstance(e, dict):
        e["verified"] = when or date.today().isoformat()
        _save(data)


def reset_to_default(slug: str, key: str, *, reason: str = "") -> None:
    """Point (slug, key) back at the code default (keeps the history fields)."""
    data = json.loads(json.dumps(_load()))
    svc = data.setdefault(slug, {})
    old = svc.get(key) or {}
    default = default_for(slug, key)
    svc[key] = {
        "selector": default,
        "default": default,
        "source": "code",
        "since": date.today().isoformat(),
        "verified": date.today().isoformat(),
        "replaced": old.get("selector"),
        "note": reason or "reverted to code default",
    }
    _save(data)


def dump_defaults(path: Path | None = None, *, keep_overrides: bool = True) -> dict[str, Any]:
    """Write every registered key to the overrides file.

    Entries that already carry a non-code override are kept (unless
    `keep_overrides=False`); everything else is (re)written as the code
    default so the file always lists the full set of selectors in use.
    """
    import_all_plugins()
    existing = _load() if keep_overrides else {}
    out: dict[str, Any] = {}
    today = date.today().isoformat()
    for slug in sorted(REGISTRY):
        out[slug] = {}
        for key in sorted(REGISTRY[slug]):
            default = REGISTRY[slug][key]
            old = existing.get(slug, {}).get(key)
            if (
                isinstance(old, dict)
                and old.get("source") not in (None, "code")
                and old.get("selector") not in (None, "")
                and old.get("selector") != default
            ):
                old = dict(old)
                old["default"] = default
                out[slug][key] = old
                continue
            out[slug][key] = {
                "selector": default,
                "default": default,
                "source": "code",
                "since": (old or {}).get("since") or today,
                "verified": (old or {}).get("verified") or today,
            }
    target = Path(path) if path else OVERRIDES_PATH
    if target == OVERRIDES_PATH:
        _save(out)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(out, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return out


def import_all_plugins() -> list[str]:
    """Import every services/svc_*.py so REGISTRY holds all declared keys."""
    import importlib

    names = []
    for p in sorted((ROOT / "services").glob("svc_*.py")):
        mod = f"services.{p.stem}"
        try:
            importlib.import_module(mod)
            names.append(mod)
        except Exception as e:  # noqa: BLE001 - a broken plugin must not hide the rest
            print(f"[selectors] could not import {mod}: {type(e).__name__}: {e}")
    return names


# ---------------------------------------------------------------------------
# Selector string helpers
# ---------------------------------------------------------------------------

def split_top_level(selector: str) -> list[str]:
    """Split a CSS selector list on commas that are not inside () [] or quotes."""
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    buf: list[str] = []
    for ch in selector:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch in "([":
            depth += 1
            buf.append(ch)
        elif ch in ")]":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return [p for p in parts if p]


def _quote_text(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def with_text(container_selector: str | list[str], text: str) -> str:
    """'tr, li' + 'x@y' -> 'tr:has-text("x@y"), li:has-text("x@y")'."""
    parts = container_selector if isinstance(container_selector, list) else split_top_level(container_selector)
    q = _quote_text(text)
    return ", ".join(f"{p}:has-text({q})" for p in parts)


def fill_target(template: str, target: str) -> str:
    """Fill a `{target}` template selector (no str.format: CSS may hold braces)."""
    return template.replace("{target}", target)


def has_placeholder(value) -> bool:
    if isinstance(value, list):
        return any("{target}" in v for v in value)
    return isinstance(value, str) and "{target}" in value


_LOOKS_LIKE_CSS = re.compile(r"^[\w\s\[\]=\"'*^$|~.#:()>+,-]+$")


def looks_like_css(value: str) -> bool:
    return bool(value) and bool(_LOOKS_LIKE_CSS.match(value))
