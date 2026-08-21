"""Adobe (Admin Console) deactivation — User Management API, no browser.

Adobe exposes a first-class REST API for offboarding (UMAPI), so this plugin
never touches the Playwright `context`. It authenticates with an OAuth
Server-to-Server credential (the JWT/Service-Account flow reached end-of-life
2025-06-30) and issues a `removeFromOrg` action, which is the API equivalent of
removing a user from the Admin Console **Users** menu: it revokes all product
profiles / user-group memberships and reclaims the licenses.

The module also exports `invite()` for grant.py — the same API in reverse:
add the user to the org and to the product profiles / user groups that carry
the licenses.

Auth (OAuth Server-to-Server, from an Adobe Developer Console project that has
the *User Management API* added):
    ADOBE_CLIENT_ID       API key / client id
    ADOBE_CLIENT_SECRET   client secret
    ADOBE_ORG_ID          organization id, e.g. 1234ABCD@AdobeOrg
Optional:
    ADOBE_SCOPES          override token scopes (default below)
    ADOBE_IMS_TOKEN_URL   override IMS token endpoint (default v3)
    ADOBE_DELETE_ACCOUNT  "true" to also delete the account+assets from the
                          owning directory (destructive; default false = just
                          remove from the org, mirroring the Users-menu action)
Invite-specific (all optional):
    ADOBE_INVITE_GROUPS          comma-separated product-profile / user-group
                                 names added on invite. Org membership alone
                                 carries NO license, so a role that maps to no
                                 groups is refused (needs-confirmation) before
                                 any org write.
    ADOBE_INVITE_GROUPS_<ROLE>   per-role override; ROLE is the task's role
                                 hint uppercased, \\W+ → _, e.g. role
                                 "after effects" → ADOBE_INVITE_GROUPS_AFTER_EFFECTS
    ADOBE_IDENTITY_TYPE          adobeID (default) | enterpriseID | federatedID
                                 — which create command to use for new users.
                                 enterprise/federated require the email domain
                                 to be claimed in the org's directory.
    ADOBE_COUNTRY                ISO-2 country for created IDs (default US;
                                 required by Adobe for federatedID)

`removeFromOrg` always reports success even for a non-existent user, so we do a
GET first to distinguish `user-not-found` from a real deactivation.

Statuses:
    deactivate: found / deactivated / user-not-found / needs-confirmation: … / failed: …
    invite:     invited / already-member / needs-confirmation: … / failed: …
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse

import requests

from services._common import is_find_only, session_attr, set_session_attr

IMS_TOKEN_URL_DEFAULT = "https://ims-na1.adobelogin.com/ims/token/v3"
UMAPI_BASE = "https://usermanagement.adobe.io/v2/usermanagement"
DEFAULT_SCOPES = "openid,AdobeID,user_management_sdk"
TIMEOUT = 30
TOKEN_ATTR = "_adobe_access_token"


def _get_token(context, client_id: str, client_secret: str) -> str:
    """Fetch (and cache on the context) an IMS access token via client_credentials."""
    cached = session_attr(context, TOKEN_ATTR)
    if cached:
        return cached

    url = os.environ.get("ADOBE_IMS_TOKEN_URL") or IMS_TOKEN_URL_DEFAULT
    scopes = os.environ.get("ADOBE_SCOPES") or DEFAULT_SCOPES
    resp = requests.post(
        url,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": scopes,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"IMS token {resp.status_code}: {resp.text[:200]}")
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError(f"IMS token response had no access_token: {resp.text[:200]}")
    set_session_attr(context, TOKEN_ATTR, token)
    return token


def _headers(token: str, client_id: str, *, json_body: bool = False) -> dict:
    h = {
        "Authorization": f"Bearer {token}",
        "X-Api-Key": client_id,
        "Accept": "application/json",
    }
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def _get_user(
    token: str, client_id: str, org_id: str, email: str
) -> tuple[dict | None, str | None]:
    """GET a single org user. Returns (user, None), (None, None) when the user
    is not in the org, or (None, 'failed: …') on a real error."""
    url = f"{UMAPI_BASE}/organizations/{org_id}/users/{urllib.parse.quote(email)}"
    resp = requests.get(url, headers=_headers(token, client_id), timeout=TIMEOUT)
    if resp.status_code == 404:
        return None, None
    if resp.status_code != 200:
        return None, f"failed: lookup {resp.status_code}: {resp.text[:200]}"
    try:
        body = resp.json()
    except ValueError:
        return None, f"failed: lookup non-json ({resp.text[:120]})"
    result = str(body.get("result", "")).lower()
    if result == "success" and body.get("user"):
        return body["user"], None
    # Adobe returns a 200 with result="error.*" for some not-found / bad-id cases.
    if "not" in result or "nonexistent" in result or "invalid" in result:
        return None, None
    return None, f"failed: lookup result={body.get('result')!r}"


def _lookup_user(token: str, client_id: str, org_id: str, email: str) -> str:
    """Return 'found', 'user-not-found', or 'failed: …' for `email`."""
    user, err = _get_user(token, client_id, org_id, email)
    if err:
        return err
    return "found" if user else "user-not-found"


def _post_action(
    token: str, client_id: str, org_id: str, commands: list[dict]
) -> tuple[dict | None, str | None]:
    """POST a commands payload to the action endpoint.

    Returns (body, None) on HTTP 200 with parseable JSON, else (None, 'failed: …').
    The caller interprets result/completed/errors.
    """
    url = f"{UMAPI_BASE}/action/{org_id}"
    resp = requests.post(
        url,
        headers=_headers(token, client_id, json_body=True),
        data=json.dumps(commands),
        timeout=TIMEOUT,
    )
    if resp.status_code == 429:
        return None, "failed: rate-limited (429)"
    if resp.status_code != 200:
        return None, f"failed: action {resp.status_code}: {resp.text[:200]}"
    try:
        return resp.json(), None
    except ValueError:
        return None, f"failed: action non-json ({resp.text[:120]})"


def _action_outcome(body: dict, success_status: str) -> str:
    """Map an action response body to a plugin status string."""
    result = str(body.get("result", "")).lower()
    if result == "success" and body.get("completed", 0) >= 1:
        return success_status
    errors = body.get("errors") or []
    if errors:
        first = errors[0]
        return f"failed: {first.get('errorCode', 'error')}: {first.get('message', '')[:160]}"
    return f"needs-confirmation: unexpected-action-result {json.dumps(body)[:200]}"


def _remove_from_org(
    token: str, client_id: str, org_id: str, email: str, delete_account: bool
) -> str:
    commands = [
        {
            "user": email,
            "requestID": "deactivate_1",
            "do": [{"removeFromOrg": {"deleteAccount": delete_account}}],
        }
    ]
    body, err = _post_action(token, client_id, org_id, commands)
    if err:
        return err
    return _action_outcome(body, "deactivated")


def deactivate(context, creds: dict, target_user: str) -> str:
    client_id = creds.get("client_id")
    client_secret = creds.get("client_secret")
    org_id = creds.get("org_id")
    if not (client_id and client_secret and org_id):
        return "failed: missing ADOBE_CLIENT_ID / ADOBE_CLIENT_SECRET / ADOBE_ORG_ID"

    if "@" not in target_user:
        return f"needs-confirmation: target is not an email ({target_user!r})"

    try:
        token = _get_token(context, client_id, client_secret)
    except Exception as e:  # noqa: BLE001
        return f"failed: auth ({type(e).__name__}: {e})"

    status = _lookup_user(token, client_id, org_id, target_user)
    if status != "found":
        return status  # user-not-found or failed: …

    if is_find_only():
        return "found"

    delete_account = os.environ.get("ADOBE_DELETE_ACCOUNT", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    return _remove_from_org(token, client_id, org_id, target_user, delete_account)


# ---------------------------------------------------------------------------
# Invite (grant.py counterpart)
# ---------------------------------------------------------------------------

IDENTITY_CREATE_STEPS = {
    "adobeid": "addAdobeID",
    "enterpriseid": "createEnterpriseID",
    "federatedid": "createFederatedID",
}


def _invite_groups(role: str) -> list[str]:
    """Product-profile / user-group names for `role`, from env.

    ADOBE_INVITE_GROUPS_<ROLE> (role uppercased, \\W+ → _) wins over the
    generic ADOBE_INVITE_GROUPS. Names are comma-separated; whitespace around
    each name is stripped (Adobe profile names routinely contain spaces).
    """
    role_key = re.sub(r"\W+", "_", (role or "").strip().upper()).strip("_")
    raw = ""
    if role_key:
        raw = os.environ.get(f"ADOBE_INVITE_GROUPS_{role_key}", "")
    if not raw:
        raw = os.environ.get("ADOBE_INVITE_GROUPS", "")
    return [g.strip() for g in raw.split(",") if g.strip()]


def _names_from_email(email: str) -> tuple[str, str]:
    """Best-effort first/last name from the email localpart.

    "kristina.mylnikova@…" → ("Kristina", "Mylnikova"). Only used for
    enterprise/federated creates, where Adobe requires both fields.
    """
    localpart = email.split("@", 1)[0]
    parts = [p for p in re.split(r"[._-]+", localpart) if p]
    if not parts:
        return email, email
    first = parts[0].capitalize()
    last = parts[-1].capitalize() if len(parts) > 1 else first
    return first, last


def _create_step(email: str) -> tuple[dict, str] | tuple[None, str]:
    """Build the create step for a user not yet in the org.

    Returns (step, identity_type) or (None, 'failed: …').
    """
    identity = (os.environ.get("ADOBE_IDENTITY_TYPE") or "adobeID").strip().lower()
    step_name = IDENTITY_CREATE_STEPS.get(identity)
    if not step_name:
        return None, (
            f"failed: bad ADOBE_IDENTITY_TYPE {identity!r} "
            f"(expected adobeID | enterpriseID | federatedID)"
        )
    params: dict = {"email": email, "option": "ignoreIfAlreadyExists"}
    if step_name != "addAdobeID":
        # Adobe requires firstname/lastname for enterprise/federated creates
        # (and country for federated).
        first, last = _names_from_email(email)
        params["firstname"] = first
        params["lastname"] = last
        params["country"] = (os.environ.get("ADOBE_COUNTRY") or "US").strip().upper()
    return {step_name: params}, identity


def invite(context, creds: dict, target_user: str, role: str = "full") -> str:
    """Add `target_user` to the Adobe org and to the license-bearing groups.

    Groups come from ADOBE_INVITE_GROUPS[_<ROLE>] — Adobe grants product access
    via product-profile membership, so an unmapped role is refused BEFORE any
    org write: inviting without a group would land the user in the org with no
    license, which is never what the request meant.

    Returns: "invited" / "already-member" / "needs-confirmation: …" / "failed: …"
    """
    client_id = creds.get("client_id")
    client_secret = creds.get("client_secret")
    org_id = creds.get("org_id")
    if not (client_id and client_secret and org_id):
        return "failed: missing ADOBE_CLIENT_ID / ADOBE_CLIENT_SECRET / ADOBE_ORG_ID"

    if "@" not in target_user:
        return f"needs-confirmation: target is not an email ({target_user!r})"

    groups = _invite_groups(role)
    if not groups:
        role_key = re.sub(r"\W+", "_", (role or "").strip().upper()).strip("_")
        return (
            f"needs-confirmation: no group mapping for role {role!r} — "
            f"set ADOBE_INVITE_GROUPS_{role_key or '<ROLE>'} (or ADOBE_INVITE_GROUPS) in .env"
        )

    try:
        token = _get_token(context, client_id, client_secret)
    except Exception as e:  # noqa: BLE001
        return f"failed: auth ({type(e).__name__}: {e})"

    user, err = _get_user(token, client_id, org_id, target_user)
    if err:
        return err

    if user:
        have = {g.lower() for g in (user.get("groups") or [])}
        missing = [g for g in groups if g.lower() not in have]
        if not missing:
            # In the org and in every requested group already.
            return "already-member"
        do = [{"add": {"group": missing}}]
        command: dict = {"user": target_user, "requestID": "invite_1", "do": do}
        if str(user.get("type", "")).lower() == "adobeid":
            command["useAdobeID"] = True
        added_note = f"added groups: {', '.join(missing)}"
    else:
        step, identity = _create_step(target_user)
        if step is None:
            return identity  # 'failed: …'
        do = [step, {"add": {"group": groups}}]
        command = {"user": target_user, "requestID": "invite_1", "do": do}
        if identity == "adobeid":
            command["useAdobeID"] = True
        added_note = f"added groups: {', '.join(groups)}"

    body, err = _post_action(token, client_id, org_id, [command])
    if err:
        return err
    outcome = _action_outcome(body, "invited")
    if outcome != "invited":
        return outcome
    return f"invited ({added_note})"
