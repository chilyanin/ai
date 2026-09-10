#!/usr/bin/env python3
"""Load runtime secrets from HashiCorp Vault into the process environment.

Replaces the plaintext `.env` as the source of credentials. The Vault path
holds one flat key/value blob — effectively the old `.env` — so a single read
populates everything and the rest of the codebase keeps using `os.environ`
unchanged (`load_creds()`, `svc_adobe`'s ADOBE_*, `asana_client._token()`).

Design notes
------------
* The LDAP password never enters this process. On Windows the DPAPI-protected
  PSCredential is decrypted by PowerShell, which performs the LDAP login and
  prints ONLY the resulting client token. Python then reads the secret over
  HTTPS itself, so the fetch path is identical on every platform.
* Nothing here logs a secret value — only key NAMES and counts.
* No token is written to disk; it lives in memory for the one read.
* `.env` is still read for NON-SECRET configuration (VAULT_ADDR and friends).
  That is a chicken-and-egg necessity: we need the address before we can talk
  to Vault. Keep credentials out of it.

Configuration (env or .env, none of it secret)
----------------------------------------------
    VAULT_ADDR          https://vault.example.com          (required)
    VAULT_SECRET_PATH   secrets/itsup/itsup-vacancies-audit-dou/creds
    VAULT_LDAP_MOUNT    ldap                                (default)
    VAULT_CRED_FILE     %LOCALAPPDATA%\\vault-cred.xml      (default)
    VAULT_TOKEN         short-circuits auth entirely        (optional)
    SECRETS_BACKEND     vault | env | auto                  (default: auto)
    VAULT_CACERT        path to the internal root CA .pem   (optional)
    VAULT_SKIP_VERIFY   1 to disable TLS verification       (NOT recommended)

TLS: Vault sits behind an internal CA. `requests` trusts only certifi's public
roots, so verification fails where PowerShell succeeds (Windows already trusts
the corporate root). Install `truststore` and the OS trust store is used
automatically; otherwise point VAULT_CACERT at the root certificate. Disabling
verification exposes the LDAP token and every secret to anyone who can
intercept the connection, so it stays an explicit, noisy opt-in.

`auto` uses Vault when VAULT_ADDR is set and falls back to `.env` otherwise —
and says out loud which one it used, so a silent regression to the plaintext
file can't masquerade as a working Vault setup.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

DEFAULT_SECRET_PATH = "secrets/itsup/itsup-vacancies-audit-dou/creds"
DEFAULT_LDAP_MOUNT = "ldap"
DEFAULT_CRED_FILE = r"%LOCALAPPDATA%\vault-cred.xml"
HTTP_TIMEOUT = 30
PS_TIMEOUT = 60


class SecretsError(RuntimeError):
    pass


def _use_os_trust_store() -> bool:
    """Make TLS verification use the OS trust store instead of certifi's bundle.

    Vault here is fronted by an internal CA. PowerShell succeeds because
    Windows already trusts that root; `requests` fails because certifi ships
    only public roots. `truststore` bridges the two, so the corporate CA is
    honoured with no extra configuration and verification stays ON.

    Optional dependency: absent, we fall back to VAULT_CACERT.
    """
    try:
        import truststore  # type: ignore
    except ImportError:
        return False
    try:
        truststore.inject_into_ssl()
        return True
    except Exception:  # noqa: BLE001
        return False


def _tls_verify() -> bool | str:
    """Resolve the `verify=` argument for Vault requests.

    Order: explicit CA bundle (VAULT_CACERT) > OS trust store > certifi.
    VAULT_SKIP_VERIFY is an escape hatch and says so, loudly, every time.
    """
    if (os.environ.get("VAULT_SKIP_VERIFY") or "").lower() in ("1", "true", "yes"):
        print(
            "[secrets] WARNING: VAULT_SKIP_VERIFY is set - the Vault TLS "
            "certificate is NOT verified. Anyone able to intercept this "
            "connection can read the LDAP token and every secret. Use "
            "VAULT_CACERT instead.",
            file=sys.stderr,
        )
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return False

    cacert = os.environ.get("VAULT_CACERT")
    if cacert:
        if not Path(cacert).is_file():
            raise SecretsError(f"VAULT_CACERT points at a missing file: {cacert}")
        return cacert

    _use_os_trust_store()
    return True


# PowerShell does the DPAPI decrypt and the LDAP login, then prints just the
# token. Interpolated values are a URL, a mount name and a file path — never a
# secret — so nothing sensitive lands in the process command line.
_PS_TOKEN_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$credFile = [Environment]::ExpandEnvironmentVariables('{cred_file}')
if (-not (Test-Path $credFile)) {{ throw "credential file not found: $credFile" }}
$cred = Import-Clixml $credFile
$user = $cred.UserName.Split('\')[-1]
$uri  = '{addr}/v1/auth/{mount}/login/' + [uri]::EscapeDataString($user)
$body = @{{ password = $cred.GetNetworkCredential().Password }} | ConvertTo-Json
$login = Invoke-RestMethod -Uri $uri -Method Post -Body $body -ContentType 'application/json'
if (-not $login.auth.client_token) {{ throw 'no client_token in LDAP login response' }}
Write-Output $login.auth.client_token
"""


def _powershell_exe() -> str | None:
    for exe in ("pwsh", "powershell.exe", "powershell"):
        from shutil import which

        if which(exe):
            return exe
    return None


def _token_via_powershell(addr: str, mount: str, cred_file: str) -> str:
    """Decrypt the DPAPI credential and LDAP-login, returning a Vault token.

    Windows-only in practice: Import-Clixml's protection is bound to the
    current Windows user on the current machine, which is exactly what makes
    it safe to leave on disk — and exactly why it cannot work elsewhere.
    """
    exe = _powershell_exe()
    if not exe:
        raise SecretsError(
            "PowerShell not found — cannot decrypt the Vault credential. "
            "Set VAULT_TOKEN instead (see module docstring)."
        )
    script = _PS_TOKEN_SCRIPT.format(
        addr=addr.rstrip("/"), mount=mount, cred_file=cred_file
    )
    try:
        proc = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise SecretsError(f"PowerShell Vault login timed out after {PS_TIMEOUT}s") from None
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = err[-1][:300] if err else f"exit {proc.returncode}"
        raise SecretsError(f"Vault LDAP login failed: {detail}")
    token = (proc.stdout or "").strip()
    if not token:
        raise SecretsError("Vault LDAP login produced no token")
    return token


def _vault_token(addr: str, mount: str, cred_file: str) -> str:
    """Obtain a Vault token, cheapest source first."""
    direct = os.environ.get("VAULT_TOKEN")
    if direct:
        return direct
    home_token = Path.home() / ".vault-token"
    if home_token.is_file():
        val = home_token.read_text(encoding="utf-8").strip()
        if val:
            return val
    return _token_via_powershell(addr, mount, cred_file)


def _unwrap(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the key/value map, tolerating KV v1 and v2 response shapes.

    v1: {"data": {k: v}}
    v2: {"data": {"data": {k: v}, "metadata": {...}}}
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        raise SecretsError("unexpected Vault response: no 'data' object")
    inner = data.get("data")
    if isinstance(inner, dict) and "metadata" in data:
        return inner  # KV v2
    return data  # KV v1


def fetch_vault_secrets() -> dict[str, str]:
    """Read the configured Vault path and return its flat key/value map."""
    addr = os.environ.get("VAULT_ADDR", "").rstrip("/")
    if not addr:
        raise SecretsError("VAULT_ADDR is not set")
    path = os.environ.get("VAULT_SECRET_PATH", DEFAULT_SECRET_PATH).strip("/")
    mount = os.environ.get("VAULT_LDAP_MOUNT", DEFAULT_LDAP_MOUNT)
    cred_file = os.environ.get("VAULT_CRED_FILE", DEFAULT_CRED_FILE)

    verify = _tls_verify()
    token = _vault_token(addr, mount, cred_file)
    try:
        r = requests.get(
            f"{addr}/v1/{path}",
            headers={"X-Vault-Token": token},
            timeout=HTTP_TIMEOUT,
            verify=verify,
        )
    except requests.exceptions.SSLError as e:
        raise SecretsError(
            f"Vault TLS verification failed: {e}\n"
            "  Vault is behind an internal CA that Python does not trust "
            "(PowerShell works because Windows already trusts it).\n"
            "  Fix, best first:\n"
            "    1. pip install truststore   - uses the OS trust store, no config\n"
            "    2. VAULT_CACERT=<path to the corporate root CA .pem>\n"
            "    3. VAULT_SKIP_VERIFY=1      - last resort, disables verification"
        ) from None
    except requests.RequestException as e:
        raise SecretsError(f"Vault request failed: {type(e).__name__}: {e}") from None
    if r.status_code == 403:
        raise SecretsError(f"Vault denied access to {path} (403) — check the policy")
    if r.status_code == 404:
        raise SecretsError(f"Vault path not found: {path} (404)")
    if not r.ok:
        raise SecretsError(f"Vault read failed: {r.status_code} {r.reason}")

    values = _unwrap(r.json())
    # Vault values may be non-strings (numbers, bools); the environment needs str.
    return {str(k): ("" if v is None else str(v)) for k, v in values.items()}


def load_secrets(*, quiet: bool = False) -> str:
    """Populate os.environ from the configured backend. Returns the backend used.

    Always loads `.env` first for non-secret configuration (VAULT_ADDR etc.),
    then overlays Vault values so Vault is authoritative.
    """
    here = Path(__file__).resolve().parent
    load_dotenv(dotenv_path=here / ".env")

    backend = (os.environ.get("SECRETS_BACKEND") or "auto").lower()
    if backend not in ("vault", "env", "auto"):
        raise SecretsError(f"SECRETS_BACKEND must be vault|env|auto, got {backend!r}")

    if backend == "env":
        if not quiet:
            print("[secrets] backend=env (.env only) - credentials are on disk in plaintext")
        return "env"

    if backend == "auto" and not os.environ.get("VAULT_ADDR"):
        if not quiet:
            print("[secrets] backend=env (VAULT_ADDR unset) - credentials are on disk in plaintext")
        return "env"

    try:
        values = fetch_vault_secrets()
    except SecretsError as e:
        if backend == "vault":
            # Explicitly asked for Vault: never fall back silently.
            raise
        print(f"[secrets] WARNING: Vault unavailable ({e})", file=sys.stderr)
        print("[secrets] backend=env (fallback) - credentials are on disk in plaintext",
              file=sys.stderr)
        return "env"

    for k, v in values.items():
        os.environ[k] = v
    if not quiet:
        print(f"[secrets] backend=vault - loaded {len(values)} key(s)")
    return "vault"


REQUIRED_KEYS_FILE = "vault-required-keys.txt"


def required_keys() -> list[str]:
    """Key names the runners need from Vault (names only, no values)."""
    p = Path(__file__).resolve().parent / REQUIRED_KEYS_FILE
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def main() -> int:
    """Diagnostic: show which backend answers and whether Vault is complete."""
    import argparse

    ap = argparse.ArgumentParser(
        description="Check secret loading. Never prints a secret value."
    )
    ap.add_argument("--show-keys", action="store_true", help="List the key names retrieved")
    ap.add_argument(
        "--check",
        action="store_true",
        help=f"Compare Vault against {REQUIRED_KEYS_FILE} and report anything missing",
    )
    args = ap.parse_args()

    try:
        backend = load_secrets()
    except SecretsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"backend: {backend}")

    if not (args.show_keys or args.check):
        return 0
    if backend != "vault":
        print("(not using Vault - nothing to inspect)")
        return 0

    try:
        vault_keys = set(fetch_vault_secrets())
    except SecretsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.show_keys:
        print(f"keys in Vault ({len(vault_keys)}):")
        for k in sorted(vault_keys):
            print(f"  {k}")

    if args.check:
        need = required_keys()
        if not need:
            print(f"warning: {REQUIRED_KEYS_FILE} not found - nothing to check")
            return 0
        missing = [k for k in need if k not in vault_keys]
        print(f"\nrequired: {len(need)}   present: {len(need) - len(missing)}")
        if missing:
            print(f"MISSING FROM VAULT ({len(missing)}):")
            for k in missing:
                print(f"  {k}")
            print("\nDo NOT delete .env.backup-pre-vault until these are migrated.")
            return 1
        print("OK - every required key is in Vault. Safe to delete the .env backup.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
