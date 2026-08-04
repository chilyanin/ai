"""SyncSketch deactivation — REST API (api/v1 + api/v2), no browser.

SyncSketch exposes a documented REST API for workspace membership, so this
plugin never touches the Playwright `context` (it only uses it to cache the
authenticated client between tasks). The removal call is the API equivalent of
the Workspace → People → "x" control in the UI: it clears the user's connections
inside that one workspace. The user's SyncSketch account itself is not deleted
(that is a self-service action on their side), and no other workspace is touched.

Note on membership: in practice ordinary users hold only `project` connections
to the workspace — a `type: "account"` connection belongs to admins/managers. So
access is judged by whether the user holds *any* connection here, not by the
presence of an account entry (see `_has_access`).

Removal is therefore two-stage: `which=account` for the workspace, then
`which=project` for every project connection still standing afterwards. It is
undocumented whether the account-level call cascades to project connections, so
the second stage is driven by what the API actually reports rather than by an
assumption either way. A user with no account connection at all (the normal
case here) may well need stage 2 to do the real work.

Auth (one pair, taken from the admin account's Settings page):
    SYNCSKETCH_LOGIN     admin email / username
    SYNCSKETCH_TOKEN     API key
    SYNCSKETCH_TEAM_ID   workspace (account) id to remove users from
Optional:
    SYNCSKETCH_HOST      override base host (default https://www.syncsketch.com)

The API accepts credentials either as an `Authorization: apikey <login>:<key>`
header or as `?api_key=&username=` query params. We try the header first so the
key stays out of URLs, and fall back to query params on 401/403 — SyncSketch's
own client still defaults to query params, so live behaviour varies.

To discover the workspace id for SYNCSKETCH_TEAM_ID, with LOGIN/TOKEN set:

    python3 -m services.svc_syncsketch --accounts

Statuses:
    found / deactivated / already-deactivated / user-not-found /
    needs-confirmation: … / failed: …
"""
from __future__ import annotations

import json
import os

import requests

from services._common import is_find_only, session_attr, set_session_attr

DEFAULT_HOST = "https://www.syncsketch.com"
TIMEOUT = 30
CLIENT_ATTR = "_syncsketch_client"
WORKSPACE_OK_ATTR = "_syncsketch_workspace_ok"

# Reported by deactivate.py's plan when credentials are missing, so the operator
# sees the API-mode keys instead of the browser-mode URL/LOGIN/PASSWORD triple.
REQUIRED_ENV = ("LOGIN", "TOKEN", "TEAM_ID")


class _Client:
    """Minimal SyncSketch REST client with header-auth → query-auth fallback."""

    def __init__(self, login: str, api_key: str, host: str = DEFAULT_HOST):
        self.login = login
        self.api_key = api_key
        self.host = host.rstrip("/")
        self.header_auth = True  # flipped to False after a 401/403 retry

    def _send(self, method: str, path: str, *, params=None, body=None, header_auth=True):
        url = f"{self.host}/{path.lstrip('/')}"
        params = dict(params or {})
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if header_auth:
            headers["Authorization"] = f"apikey {self.login}:{self.api_key}"
        else:
            params.update({"api_key": self.api_key, "username": self.login})
        return requests.request(
            method,
            url,
            params=params,
            data=json.dumps(body) if body is not None else None,
            headers=headers,
            timeout=TIMEOUT,
        )

    def request(self, method: str, path: str, *, params=None, body=None):
        resp = self._send(method, path, params=params, body=body, header_auth=self.header_auth)
        if self.header_auth and resp.status_code in (401, 403):
            # Header auth isn't honoured on this deployment — fall back to the
            # query-param scheme SyncSketch's own client uses by default.
            retry = self._send(method, path, params=params, body=body, header_auth=False)
            if retry.status_code not in (401, 403):
                self.header_auth = False
            return retry
        return resp

    def get(self, path: str, **kw):
        return self.request("get", path, **kw)

    def post(self, path: str, **kw):
        return self.request("post", path, **kw)


def _json(resp):
    try:
        return resp.json()
    except ValueError:
        return None


def _check_auth(client: _Client) -> str | None:
    """Return None if credentials work, else a 'failed: …' string."""
    resp = client.get("/api/v1/person/connected/")
    if resp.status_code == 200:
        return None
    if resp.status_code in (401, 403):
        return (
            f"failed: auth rejected ({resp.status_code}) — check "
            f"SYNCSKETCH_LOGIN / SYNCSKETCH_TOKEN"
        )
    return f"failed: connectivity check {resp.status_code}: {resp.text[:160]}"


def _accounts(client: _Client) -> list[dict]:
    resp = client.get("/api/v1/account/", params={"active": 1})
    if resp.status_code != 200:
        raise RuntimeError(f"account list {resp.status_code}: {resp.text[:160]}")
    body = _json(resp) or {}
    # The endpoint can repeat a workspace (one row per connection) — dedupe by id.
    seen: dict[str, dict] = {}
    for acc in body.get("objects") or []:
        seen.setdefault(str(acc.get("id")), acc)
    return list(seen.values())


def _verify_workspace(context, client: _Client, workspace_id: str) -> str | None:
    """Confirm SYNCSKETCH_TEAM_ID is a workspace this admin actually has.

    Load-bearing: `_connections` reads a 404 as "no access here", so a wrong
    workspace id would 404 for every user and silently report everyone as
    already-deactivated — closing Asana tasks having removed nothing. Checked
    once per run and cached on the context.
    """
    if context is not None and session_attr(context, WORKSPACE_OK_ATTR) == workspace_id:
        return None
    try:
        accounts = _accounts(client)
    except RuntimeError as e:
        return f"failed: {e}"
    ids = [str(a.get("id")) for a in accounts]
    if str(workspace_id) not in ids:
        return (
            f"failed: SYNCSKETCH_TEAM_ID={workspace_id} is not a workspace this "
            f"account administers (has: {', '.join(ids) or 'none'})"
        )
    if context is not None:
        set_session_attr(context, WORKSPACE_OK_ATTR, workspace_id)
    return None


def _lookup_user_id(client: _Client, email: str) -> tuple[int | None, str | None]:
    """Return (user_id, error). user_id is None when the email is unknown."""
    resp = client.get("/api/v1/simpleperson/", params={"email__iexact": email})
    if resp.status_code != 200:
        return None, f"failed: user lookup {resp.status_code}: {resp.text[:160]}"
    body = _json(resp)
    if body is None:
        return None, f"failed: user lookup non-json ({resp.text[:120]})"
    objects = body.get("objects") or []
    if not objects:
        return None, None
    uid = objects[0].get("id")
    if not uid:
        return None, f"failed: user record has no id ({json.dumps(objects[0])[:160]})"
    return int(uid), None


def _connections(client: _Client, user_id: int, workspace_id: str) -> list | None:
    """The user's connections *within* this workspace, or None if unreadable.

    Live payload is a flat list, one entry per connection:
        {"type": "project", "permission": "member", "project_id": 23135, …}
        {"type": "account", "permission": "admin",  "account_id": "14550", …}

    A 404 `{"detail": "No Person matches the given query."}` is how the endpoint
    reports "this person holds nothing in this workspace" — observed live right
    after a successful removal — so it maps to an empty list, not to unknown.
    That reading is only safe because `_verify_workspace` has already confirmed
    the configured workspace id exists; otherwise a bad SYNCSKETCH_TEAM_ID would
    404 for everyone and every task would look already-deactivated.

    Other unrecognised shapes/statuses return None so the caller stays
    conservative instead of reporting an access state we can't actually see.
    """
    resp = client.get(f"/api/v2/user/{user_id}/connections/account/{workspace_id}/")
    if resp.status_code == 404:
        return []
    if resp.status_code != 200:
        return None
    body = _json(resp)
    if isinstance(body, list):
        return body
    # Tolerate an envelope in case the endpoint grows one.
    if isinstance(body, dict):
        for key in ("connections", "objects"):
            if isinstance(body.get(key), list):
                return body[key]
    return None


def _has_access(connections: list | None) -> bool | None:
    """True/False if the user holds any access in the workspace, None if unknown.

    Deliberately counts *any* connection, not just `type == "account"`. In this
    workspace regular users are attached purely through project connections —
    an `account` entry shows up for admins/managers. Requiring one would report
    every ordinary member as already-deactivated, and the Asana task would be
    closed having removed nothing. The workspace People list shows anyone with a
    connection, and the removal call is what clears them all.
    """
    if connections is None:
        return None
    return bool(connections)


def _describe(connections: list) -> str:
    counts: dict[str, int] = {}
    for entry in connections:
        kind = str(entry.get("type") or "unknown")
        counts[kind] = counts.get(kind, 0) + 1
    return ", ".join(f"{n} {kind}" for kind, n in sorted(counts.items())) or "none"


def _project_ids(connections: list) -> list[str]:
    """Distinct project ids from a connections list, order preserved."""
    ids: list[str] = []
    for entry in connections:
        if str(entry.get("type")) != "project":
            continue
        pid = entry.get("project_id") or entry.get("id")
        if pid is not None and str(pid) not in ids:
            ids.append(str(pid))
    return ids


def _remove_users(client: _Client, which: str, entity_id: str, email: str) -> str | None:
    """POST one removal. Returns None on an accepted call, else 'failed: …'.

    `which` is "account" (workspace-level) or "project". `users` is a JSON
    *string* nested inside the JSON body — that double encoding is what the API
    expects (see SyncSketch's own python client).
    """
    resp = client.post(
        "/api/v2/remove-users/",
        body={
            "which": which,
            "entity_id": entity_id,
            "users": json.dumps([{"email": email}]),
        },
    )
    if resp.status_code in (200, 201, 202, 204):
        body = _json(resp)
        text = resp.text[:200]
        if isinstance(body, dict) and body.get("error"):
            return f"failed: remove-users({which}) error: {str(body['error'])[:160]}"
        if "error" in text.lower() and "false" not in text.lower():
            return f"failed: remove-users({which}) response: {text}"
        return None
    return f"failed: remove-users({which}) {resp.status_code}: {resp.text[:160]}"


def _get_client(context, creds: dict) -> _Client:
    cached = session_attr(context, CLIENT_ATTR) if context is not None else None
    if cached:
        return cached
    client = _Client(
        creds["login"],
        creds["token"],
        os.environ.get("SYNCSKETCH_HOST") or DEFAULT_HOST,
    )
    if context is not None:
        set_session_attr(context, CLIENT_ATTR, client)
    return client


def deactivate(context, creds: dict, target_user: str) -> str:
    login = creds.get("login")
    token = creds.get("token")
    workspace_id = creds.get("team_id")

    missing = [
        name
        for name, val in (
            ("SYNCSKETCH_LOGIN", login),
            ("SYNCSKETCH_TOKEN", token),
            ("SYNCSKETCH_TEAM_ID", workspace_id),
        )
        if not val
    ]
    if missing:
        return f"failed: missing {' / '.join(missing)}"

    if "@" not in target_user:
        return f"needs-confirmation: target is not an email ({target_user!r})"

    client = _get_client(context, creds)

    auth_error = _check_auth(client)
    if auth_error:
        return auth_error

    workspace_error = _verify_workspace(context, client, workspace_id)
    if workspace_error:
        return workspace_error

    user_id, error = _lookup_user_id(client, target_user)
    if error:
        return error
    if user_id is None:
        return "user-not-found"

    before = _connections(client, user_id, workspace_id)
    access = _has_access(before)
    if access is False:
        # The SyncSketch account exists but holds nothing in this workspace.
        return "already-deactivated"

    if is_find_only():
        return "found" if access else "needs-confirmation: access-unreadable"

    # Stage 1: workspace-level removal. Whether this alone clears a
    # project-only user is undocumented — it may only drop the `account`
    # connection — so stage 2 mops up whatever survives instead of assuming.
    removal_error = _remove_users(client, "account", workspace_id, target_user)
    if removal_error:
        return removal_error

    after = _connections(client, user_id, workspace_id)
    if _has_access(after) is False:
        return "deactivated"
    if after is None:
        return "needs-confirmation: removal-accepted-but-unverified"

    # Stage 2: per-project removal for the connections the account-level call
    # left behind.
    project_ids = _project_ids(after)
    if not project_ids:
        return f"needs-confirmation: still-has-access ({_describe(after)})"

    errors: list[str] = []
    for pid in project_ids:
        err = _remove_users(client, "project", pid, target_user)
        if err:
            errors.append(f"project {pid}: {err}")

    final = _connections(client, user_id, workspace_id)
    if _has_access(final) is False:
        return "deactivated"
    if final is None:
        return "needs-confirmation: removal-accepted-but-unverified"
    detail = _describe(final)
    if errors:
        return f"needs-confirmation: still-has-access ({detail}); {errors[0]}"
    return f"needs-confirmation: still-has-access ({detail})"


def _main() -> int:
    """`python3 -m services.svc_syncsketch --accounts` — list workspace ids."""
    import sys

    from dotenv import load_dotenv

    load_dotenv(
        dotenv_path=os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    )
    login = os.environ.get("SYNCSKETCH_LOGIN")
    token = os.environ.get("SYNCSKETCH_TOKEN")
    if not (login and token):
        print("set SYNCSKETCH_LOGIN and SYNCSKETCH_TOKEN in .env first", file=sys.stderr)
        return 2

    client = _Client(login, token, os.environ.get("SYNCSKETCH_HOST") or DEFAULT_HOST)
    auth_error = _check_auth(client)
    if auth_error:
        print(auth_error, file=sys.stderr)
        return 1
    print(f"auth ok (mode={'header' if client.header_auth else 'query-param'})")

    try:
        accounts = _accounts(client)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    if not accounts:
        print("no workspaces visible to this account")
        return 1
    print("\nworkspaces (put the right id in SYNCSKETCH_TEAM_ID):")
    for acc in accounts:
        print(f"  {str(acc.get('id')):<12} {acc.get('name', '<unnamed>')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
