"""Rule-based bucket classifier for Asana task titles.

Buckets (stable IDs used by the DB + dashboard):
  * offboarding  — removal/deactivation requests
  * access       — license / access / transfer requests
  * procurement  — renewals, purchases, audits
  * other        — anything that needs a human eye

The rules are intentionally conservative — when in doubt, we drop to "other"
so the dashboard surfaces unfamiliar task patterns rather than silently
mis-bucketing them.
"""
from __future__ import annotations

import re

BUCKETS = ("offboarding", "access", "procurement", "other")

BUCKET_LABELS = {
    "offboarding": "Offboarding",
    "access": "Access requests",
    "procurement": "Procurement / Renewals",
    "other": "Other (needs human)",
}

# Compiled once at import time. All patterns are case-insensitive.
_OFFBOARDING_PATTERNS = [
    re.compile(r"^Удалить из\b", re.I),
    re.compile(r"^Отключени[ея]\b", re.I),                # "Отключения художников"
    re.compile(r"\bПрекращение сотрудничества\b", re.I),
    re.compile(r"^Ротация пароля\b", re.I),               # offboarding password rotation
    re.compile(r"^Удалить помеченное на удаление\b", re.I),
]

_ACCESS_PATTERNS = [
    re.compile(r"^Заявка на лицензию/доступ\b", re.I),
    re.compile(r"^Доступ\s+(к|в)\b", re.I),               # "Доступ к Figma", "Доступ в Slack Connect"
    re.compile(r"^Перенос доступов\b", re.I),
    re.compile(r"^Выдача доступов\b", re.I),
]

_PROCUREMENT_PATTERNS = [
    re.compile(r"^Продление\b", re.I),
    re.compile(r"^Закупка\b", re.I),
    re.compile(r"^Заявка на закупку\b", re.I),
    re.compile(r"^Заявка на покупку\b", re.I),
    re.compile(r"-аудит\b", re.I),                        # "Adobe-аудит использования"
    re.compile(r"^Инвойс за\b", re.I),
]


def classify(task: dict) -> str:
    """Return the bucket id for a single Asana task dict.

    Only the `name` field is consulted. Title-based rules are simpler to
    reason about than parsing notes / custom fields, and the four buckets
    map cleanly to the title conventions Playrix IT uses in Asana.
    """
    name = (task.get("name") or "").strip()
    if not name:
        return "other"
    for p in _OFFBOARDING_PATTERNS:
        if p.search(name):
            return "offboarding"
    for p in _ACCESS_PATTERNS:
        if p.search(name):
            return "access"
    for p in _PROCUREMENT_PATTERNS:
        if p.search(name):
            return "procurement"
    return "other"


def classify_many(tasks: list[dict]) -> tuple[dict[str, int], list[dict]]:
    """Run `classify` over `tasks` and return (counts_by_bucket, enriched_tasks).

    `enriched_tasks` is a shallow copy of the input with a "bucket" field added.
    `counts_by_bucket` has keys for every bucket in BUCKETS (zero if unused).
    """
    counts = {b: 0 for b in BUCKETS}
    enriched: list[dict] = []
    for t in tasks:
        b = classify(t)
        counts[b] += 1
        enriched.append({**t, "bucket": b})
    return counts, enriched
