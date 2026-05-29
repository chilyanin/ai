"""Alias plugin for Asana service name "Unity (Enterprise)"."""
from __future__ import annotations

from services.svc_unity import _deactivate


def deactivate(context, creds: dict, target_user: str) -> str:
    return _deactivate(context, creds, target_user, page_slug="unity_enterprise")
