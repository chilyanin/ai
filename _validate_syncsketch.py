#!/usr/bin/env python3
"""Throwaway validation harness for the SyncSketch find-only flow.

Loads .env and calls the SyncSketch handler in DEACTIVATE_FIND_ONLY mode.
Touches NO Asana and performs NO destructive action — it authenticates, lists
the workspaces the admin can see, resolves the target's user record, and stops
at "found" / "already-deactivated" / "user-not-found".

    python3 _validate_syncsketch.py tatyana.panasik@playrix.com

No browser is involved: SyncSketch membership is managed over the REST API, so
there is no screenshot to save (deactivate.py notes that in its Asana comment).
"""
from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

load_dotenv()
os.environ["DEACTIVATE_FIND_ONLY"] = "1"

from services import svc_syncsketch as svc  # noqa: E402

TARGET = sys.argv[1] if len(sys.argv) > 1 else "tatyana.panasik@playrix.com"

creds = {
    "login": os.environ.get("SYNCSKETCH_LOGIN"),
    "token": os.environ.get("SYNCSKETCH_TOKEN"),
    "team_id": os.environ.get("SYNCSKETCH_TEAM_ID"),
}
missing = [f"SYNCSKETCH_{k.upper()}" for k, v in creds.items() if not v]
if missing:
    print(f"fill {', '.join(missing)} in .env first", file=sys.stderr)
    print("(run `python3 -m services.svc_syncsketch --accounts` to find the "
          "workspace id once LOGIN/TOKEN are set)", file=sys.stderr)
    raise SystemExit(2)

client = svc._Client(creds["login"], creds["token"],
                     os.environ.get("SYNCSKETCH_HOST") or svc.DEFAULT_HOST)
auth_error = svc._check_auth(client)
if auth_error:
    print(auth_error, file=sys.stderr)
    raise SystemExit(1)
print(f"auth ok (mode={'header' if client.header_auth else 'query-param'})")

try:
    for acc in svc._accounts(client):
        marker = " <- SYNCSKETCH_TEAM_ID" if str(acc.get("id")) == str(creds["team_id"]) else ""
        print(f"  workspace {str(acc.get('id')):<12} {acc.get('name', '<unnamed>')}{marker}")
except RuntimeError as e:
    print(f"(workspace list unavailable: {e})")

# context=None — the plugin only uses it to cache the client between tasks.
status = svc.deactivate(None, creds, TARGET)
print(f"\n=== find-only status for {TARGET}: {status} ===")
