"""Adobe (Admin Console) deactivation — User Management API, no browser.

Adobe exposes a first-class REST API for offboarding (UMAPI), so this plugin
never touches the Playwright `context`. It authenticates with an OAuth
Server-to-Server credential (the JWT/Service-Account flow reached end-of-life
2025-06-30) and issues a `removeFromOrg` action, which is the API equivalent of
removing a user from the Admin Console **Users** menu: it revokes all product
profiles / user-group memberships and reclaims the licenses.

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

`removeFromOrg` always reports success even for a non-existent user, so we do a
GET first to distinguish `user-not-found` from a real deactivation.

Statuses:
    found / deactivated / user-not-found / needs-confirmation: … / failed: …
"""
from __future__ import annotations

import json
import os
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


def _lookup_user(token: str, client_id: str, org_id: str, email: str) -> str:
    """Return 'found', 'user-not-found', or 'failed: …' for `email`."""
    url = f"{UMAPI_BASE}/organizations/{org_id}/users/{urllib.parse.quote(email)}"
    resp = requests.get(url, headers=_headers(token, client_id), timeout=TIMEOUT)
    if resp.status_code == 404:
        return "user-not-found"
    if resp.status_code != 200:
        return f"failed: lookup {resp.status_code}: {resp.text[:200]}"
    try:
        body = resp.json()
    except ValueError:
        return f"failed: lookup non-json ({resp.text[:120]})"
    result = str(body.get("result", "")).lower()
    if result == "success" and body.get("user"):
        return "found"
    # Adobe returns a 200 with result="error.*" for some not-found / bad-id cases.
    if "not" in result or "nonexistent" in result or "invalid" in result:
        return "user-not-found"
    return f"failed: lookup result={body.get('result')!r}"


def _remove_from_org(
    token: str, client_id: str, org_id: str, email: str, delete_account: bool
) -> str:
    url = f"{UMAPI_BASE}/action/{org_id}"
    payload = [
        {
            "user": email,
            "requestID": "deactivate_1",
            "do": [{"removeFromOrg": {"deleteAccount": delete_account}}],
        }
    ]
    resp = requests.post(
        url,
        headers=_headers(token, client_id, json_body=True),
        data=json.dumps(payload),
        timeout=TIMEOUT,
    )
    if resp.status_code == 429:
        return "failed: rate-limited (429)"
    if resp.status_code != 200:
        return f"failed: action {resp.status_code}: {resp.text[:200]}"
    try:
        body = resp.json()
    except ValueError:
        return f"failed: action non-json ({resp.text[:120]})"

    result = str(body.get("result", "")).lower()
    if result == "success" and body.get("completed", 0) >= 1:
        return "deactivated"
    # Surface the first error message if the action didn't complete.
    errors = body.get("errors") or []
    if errors:
        first = errors[0]
        return f"failed: {first.get('errorCode', 'error')}: {first.get('message', '')[:160]}"
    return f"needs-confirmation: unexpected-action-result {json.dumps(body)[:200]}"


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
