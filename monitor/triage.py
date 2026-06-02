"""LLM-assisted triage for "Other" bucket tasks (display-only).

Given a task title + notes, ask OpenAI to propose:
  * subcategory       — a short refined category
  * target_service    — the system/service the task likely concerns
  * suggested_action  — the concrete next step a human should take
  * confidence        — low | medium | high
  * rationale         — one sentence explaining the guess

Pure assistance: nothing here executes anything or writes to Asana.
"""
from __future__ import annotations

import json
import os
import re

import requests

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = os.environ.get("OPENAI_TRIAGE_MODEL", "gpt-5.5")

_SYSTEM_PROMPT = (
    "You are an IT operations assistant for a game studio. You triage internal "
    "Asana tasks that an automated rule-classifier could not categorise into "
    "the known buckets (service offboarding, access/license requests, "
    "procurement/renewals). Task titles are often in Russian. "
    "Given a task, infer what it actually asks for and the single most useful "
    "next action an IT specialist should take. Be concrete and brief. "
    "If the task is ambiguous, say so and set confidence to 'low'. "
    "Respond ONLY with a JSON object with keys: subcategory (short string), "
    "target_service (string, '' if none/unknown), suggested_action (one or two "
    "sentences), confidence ('low'|'medium'|'high'), rationale (one sentence). "
    "Keep subcategory and target_service in English; suggested_action may use "
    "the task's language."
)


class TriageError(RuntimeError):
    pass


def _api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise TriageError("OPENAI_API_KEY is not set")
    return key


def _coerce(obj: dict) -> dict:
    """Normalise the model's JSON into our fixed shape."""
    conf = str(obj.get("confidence", "")).strip().lower()
    if conf not in ("low", "medium", "high"):
        conf = "low"
    return {
        "subcategory": str(obj.get("subcategory", "")).strip()[:120] or "uncategorised",
        "target_service": str(obj.get("target_service", "")).strip()[:120],
        "suggested_action": str(obj.get("suggested_action", "")).strip()[:1000]
        or "No suggestion produced.",
        "confidence": conf,
        "rationale": str(obj.get("rationale", "")).strip()[:500],
    }


def triage_task(name: str, notes: str = "", *, model: str | None = None) -> dict:
    """Call OpenAI and return the normalised triage dict. Raises TriageError."""
    model = model or DEFAULT_MODEL
    user_content = f"Task title:\n{name}\n\nTask notes:\n{(notes or '').strip() or '(none)'}"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "response_format": {"type": "json_object"},
    }
    # gpt-5 family only supports the default temperature (1); sending a custom
    # value is rejected. Older models benefit from a low temperature.
    if not model.startswith("gpt-5"):
        payload["temperature"] = 0.2
    try:
        r = requests.post(
            OPENAI_URL,
            headers={
                "Authorization": f"Bearer {_api_key()}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=45,
        )
    except requests.RequestException as e:
        raise TriageError(f"request failed: {e}") from e

    if not r.ok:
        raise TriageError(f"openai {r.status_code}: {r.text[:300]}")

    try:
        content = r.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as e:
        raise TriageError(f"unexpected response shape: {e}") from e

    try:
        obj = json.loads(content)
    except ValueError:
        # Model wrapped JSON in prose/code fences — extract the first {...}.
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            raise TriageError("model did not return JSON")
        obj = json.loads(m.group(0))

    out = _coerce(obj)
    out["model"] = model
    return out
