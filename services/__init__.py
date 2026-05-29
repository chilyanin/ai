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


def get_handler(service_name: str):
    module_name = f"services.{_slug(service_name)}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        from services import _generic
        return _generic.deactivate
    return module.deactivate


def env_key(service_name: str) -> str:
    return re.sub(r"\W+", "_", service_name, flags=re.UNICODE).strip("_").upper()
