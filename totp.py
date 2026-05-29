"""TOTP (RFC 6238) code generator — stdlib only.

Defaults match Google Authenticator / Authy / Figma / Maxon / virtually every
real-world TOTP setup (HMAC-SHA1, 30-second step, 6 digits).

Usage:
    from totp import generate
    code = generate(os.environ["FIGMA_2FA_SECRET"])
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time


def _extract_secret(secret_or_url: str) -> str:
    """Accept either a raw base32 secret or a full `otpauth://...?secret=…` URL."""
    s = secret_or_url.strip()
    if s.lower().startswith("otpauth://"):
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(s).query)
        secret = (q.get("secret") or [""])[0]
        if not secret:
            raise ValueError("otpauth URL has no `secret` query param")
        return secret
    return s


def generate(
    secret: str,
    *,
    time_step: int = 30,
    digits: int = 6,
    digest: str = "sha1",
    when: float | None = None,
) -> str:
    """Return the current TOTP code for `secret` (a base32 string).

    `secret` may include spaces and lower-case letters; missing padding is added.
    Also accepts a full `otpauth://...?secret=…` URL.
    """
    raw = _extract_secret(secret)
    cleaned = raw.replace(" ", "").replace("-", "").upper()
    pad = (-len(cleaned)) % 8
    key = base64.b32decode(cleaned + "=" * pad)

    moment = time.time() if when is None else when
    counter = int(moment // time_step)
    msg = struct.pack(">Q", counter)
    mac = hmac.new(key, msg, getattr(hashlib, digest)).digest()
    offset = mac[-1] & 0x0F
    code_int = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code_int % (10 ** digits)).zfill(digits)


def seconds_remaining(time_step: int = 30, when: float | None = None) -> int:
    """Seconds until the current code expires. Useful before submitting a code."""
    moment = time.time() if when is None else when
    return time_step - int(moment % time_step)


if __name__ == "__main__":  # quick self-check
    import sys
    if len(sys.argv) > 1:
        print(generate(sys.argv[1]))
    else:
        # Smoke-test against the canonical RFC 6238 example secret.
        # Secret bytes are ASCII "12345678901234567890" → base32 below.
        secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
        # At T=59 (counter=1) the SHA-1 reference code is 94287082; we take
        # the last 6 digits because we're in 6-digit mode.
        code = generate(secret, when=59)
        assert code == "287082", f"TOTP self-check failed: got {code}"
        print("TOTP self-check ok")
