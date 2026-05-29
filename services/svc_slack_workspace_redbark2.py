"""Slack (Workspace Redbark2) deactivation plugin — API-based, no browser.

Uses the Slack Web API directly:
    1. users.lookupByEmail(email=target)         → user_id
    2. admin.users.remove(team_id, user_id)      → remove from workspace

Required env (either prefix is accepted):
    SLACK_WORKSPACE_REDBARK2_TOKEN     xoxp-* user OAuth token with the
                                       admin.users:write scope (and
                                       users:read.email for lookup).
    SLACK_WORKSPACE_REDBARK2_TEAM_ID   (optional) team GID. If not set,
                                       auto-detected via auth.test once per
                                       run and cached on the context.

Statuses:
    "deactivated"            → admin.users.remove succeeded
    "user-not-found"         → email not found in Slack
    "found"                  → --find-only, looked up user_id only
    "failed: <reason>"       → any other error (with Slack's error code if any)
"""
from __future__ import annotations

import os
from typing import Any

import requests

API_ROOT = "https://slack.com/api"
SESSION_TEAM_KEY = "_slack_workspace_redbark2_team_id"


def _post(token: str, method: str, **params: Any) -> dict:
    r = requests.post(
        f"{API_ROOT}/{method}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=params,
        timeout=30,
    )
    try:
        return r.json()
    except ValueError:
        return {"ok": False, "error": f"non-json-response (status={r.status_code})"}


def _resolve_team_id(context, token: str, configured: str | None) -> str | None:
    if configured:
        return configured
    cached = getattr(context, SESSION_TEAM_KEY, None)
    if cached:
        return cached
    res = _post(token, "auth.test")
    if not res.get("ok"):
        return None
    team_id = res.get("team_id")
    if team_id:
        setattr(context, SESSION_TEAM_KEY, team_id)
    return team_id


def deactivate(context, creds: dict, target_user: str) -> str:
    token = creds.get("token")
    if not token:
        return "failed: no-token"

    team_id = _resolve_team_id(context, token, creds.get("team_id"))
    if not team_id:
        return "failed: cannot-resolve-team-id"

    res = _post(token, "users.lookupByEmail", email=target_user)
    if not res.get("ok"):
        err = res.get("error", "unknown")
        # Slack returns "users_not_found" for missing emails.
        if err in ("users_not_found", "user_not_found"):
            return "user-not-found"
        return f"failed: lookup ({err})"

    user = res.get("user") or {}
    user_id = user.get("id")
    if not user_id:
        return "user-not-found"

    if os.environ.get("DEACTIVATE_FIND_ONLY"):
        return f"found: user_id={user_id}"

    res = _post(token, "admin.users.remove", team_id=team_id, user_id=user_id)
    if not res.get("ok"):
        err = res.get("error", "unknown")
        return f"failed: remove ({err})"
    return "deactivated"
