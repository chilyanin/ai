"""Minimal Asana REST client for the deactivation workflow."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import requests

API_ROOT = "https://app.asana.com/api/1.0"
OAUTH_TOKEN_URL = "https://app.asana.com/-/oauth_token"


class AsanaError(RuntimeError):
    pass


_cached_token: str | None = None


def _refresh_oauth_token() -> str | None:
    client_id = os.environ.get("ASANA_CLIENT_ID")
    client_secret = os.environ.get("ASANA_CLIENT_SECRET")
    refresh_token = os.environ.get("ASANA_REFRESH_TOKEN")
    if not (client_id and client_secret and refresh_token):
        return None
    r = requests.post(
        OAUTH_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    if not r.ok:
        raise AsanaError(
            f"OAuth refresh failed: {r.status_code} {r.reason}: {r.text[:300]}"
        )
    return r.json()["access_token"]


def _token() -> str:
    global _cached_token
    if _cached_token:
        return _cached_token
    direct = os.environ.get("ASANA_ACCESS_TOKEN")
    if direct:
        _cached_token = direct
        return direct
    refreshed = _refresh_oauth_token()
    if refreshed:
        _cached_token = refreshed
        return refreshed
    raise AsanaError(
        "no Asana auth: set ASANA_ACCESS_TOKEN, or ASANA_CLIENT_ID/"
        "ASANA_CLIENT_SECRET/ASANA_REFRESH_TOKEN in .env"
    )


def _get(path: str, params: dict | None = None) -> Any:
    r = requests.get(
        f"{API_ROOT}{path}",
        params=params,
        headers={"Authorization": f"Bearer {_token()}"},
        timeout=30,
    )
    if not r.ok:
        raise AsanaError(f"{r.status_code} {r.reason} on {path}: {r.text[:300]}")
    return r.json()["data"]


def _post_json(path: str, payload: dict, params: dict | None = None) -> Any:
    r = requests.post(
        f"{API_ROOT}{path}",
        params=params,
        json=payload,
        headers={
            "Authorization": f"Bearer {_token()}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    if not r.ok:
        raise AsanaError(f"{r.status_code} {r.reason} on {path}: {r.text[:300]}")
    return r.json()["data"]


def create_task_comment(task_gid: str, text: str) -> dict:
    """Create a comment story on a task."""
    return _post_json(f"/tasks/{task_gid}/stories", {"data": {"text": text}})


def upload_task_attachment(task_gid: str, path: str | Path) -> dict:
    """Upload a local file as an Asana attachment on a task."""
    p = Path(path)
    with p.open("rb") as f:
        r = requests.post(
            f"{API_ROOT}/attachments",
            headers={"Authorization": f"Bearer {_token()}"},
            data={"parent": task_gid},
            files={"file": (p.name, f, "image/png")},
            timeout=60,
        )
    if not r.ok:
        raise AsanaError(
            f"{r.status_code} {r.reason} uploading {p.name}: {r.text[:300]}"
        )
    return r.json()["data"]


def me() -> dict:
    return _get("/users/me")


def _resolve_workspace_gid() -> str:
    override = (
        os.environ.get("ASANA_WORKSPACE_GID")
        or os.environ.get("ASANA_WORKSPACE_ID")
    )
    if override:
        return override
    workspaces = me().get("workspaces") or []
    if not workspaces:
        raise AsanaError("current Asana user has no workspaces")
    if len(workspaces) > 1:
        names = ", ".join(f"{w['name']}({w['gid']})" for w in workspaces)
        raise AsanaError(
            f"multiple Asana workspaces found ({names}); set ASANA_WORKSPACE_GID in .env"
        )
    return workspaces[0]["gid"]


def fetch_tasks_due_on(
    due_on: str,
    text_filter: str = "Удалить из",
    include_completed: bool = False,
) -> list[dict]:
    """Return tasks assigned to the authed user, due on `due_on`.

    By default excludes completed tasks; pass `include_completed=True` to keep them.
    """
    current = me()
    workspace_gid = _resolve_workspace_gid()
    params: dict[str, Any] = {
        "assignee.any": current["gid"],
        "due_on": due_on,
        "opt_fields": "name,notes,due_on,gid,permalink_url,completed",
        "limit": 100,
    }
    if not include_completed:
        params["completed"] = "false"
    if text_filter:
        params["text"] = text_filter
    return _get(f"/workspaces/{workspace_gid}/tasks/search", params=params)
