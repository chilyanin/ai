"""Per-service deactivation handlers.

Drop a file named `svc_<slug>.py` into this folder with a top-level
`deactivate(context, creds, target_user) -> str` function. `context` is a
Playwright BrowserContext; `creds` is a dict with keys url/login/password/notes;
`target_user` is the identifier parsed from the Asana task. Return a short
status string ("deactivated", "user-not-found", "skipped", etc).

If no plugin exists for a service, the generic handler opens the URL and
pauses for manual action.
"""
from __future__ import annotations

import importlib
import re


def _slug(service_name: str) -> str:
    s = re.sub(r"\W+", "_", service_name.lower(), flags=re.UNICODE).strip("_")
    return "svc_" + s if s else "svc_unknown"


# Some Asana titles carry a tail inside the service name — e.g. the transfer
# variant "Syncsketch (RedBark) при переходе в Playrix", which slugifies to
# svc_syncsketch_redbark_при_переходе_в_playrix. These all target the same admin
# surface, so a slug starting with one of these prefixes falls back to it.
PREFIX_ALIASES = ("svc_syncsketch",)


def _load_module(service_name: str):
    """Import the plugin module for `service_name`, or None if there isn't one."""
    slug = _slug(service_name)
    candidates = [slug]
    candidates += [a for a in PREFIX_ALIASES if slug.startswith(a) and a != slug]
    for candidate in candidates:
        try:
            return importlib.import_module(f"services.{candidate}")
        except ModuleNotFoundError:
            continue
    return None


def get_handler(service_name: str):
    module = _load_module(service_name)
    if module is None:
        from services import _generic
        return _generic.deactivate
    return module.deactivate


def required_env(service_name: str) -> tuple[str, ...] | None:
    """Env field names a plugin declares as required (e.g. ("LOGIN", "TOKEN")).

    None when the plugin doesn't declare any, in which case callers fall back to
    the default browser-mode expectations (URL/LOGIN/PASSWORD).
    """
    module = _load_module(service_name)
    fields = getattr(module, "REQUIRED_ENV", None) if module else None
    return tuple(fields) if fields else None


def env_key(service_name: str) -> str:
    return re.sub(r"\W+", "_", service_name, flags=re.UNICODE).strip("_").upper()
