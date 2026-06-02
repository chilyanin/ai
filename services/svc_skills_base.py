"""Skills Base (org "playrix") deactivation plugin.

Matches the Asana service name "Skills Base". Shares its entire implementation
with svc_skills_base_programmers (login, Cloudflare-Turnstile solving via
solvecaptcha, MFA-setup skip, users-tab discovery + browser_use fallback,
find-user) — only the org URL and per-service session/page identifiers differ.

Required env:
    SKILLS_BASE_URL        users/admin URL (https://app.skills-base.com/o/playrix)
    SKILLS_BASE_LOGIN      admin email/login
    SKILLS_BASE_PASSWORD   admin password

Captcha solving requires SOLVECAPTCHA_API_KEY in env.
"""
from __future__ import annotations

from services.svc_skills_base_programmers import run

SESSION_KEY = "_skills_base_signed_in"
SERVICE_SLUG = "skills_base"


def deactivate(context, creds: dict, target_user: str) -> str:
    return run(context, creds, target_user, session_key=SESSION_KEY, slug=SERVICE_SLUG)
