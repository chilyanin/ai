"""Alias for the Asana service name "Adobe Creative Cloud (RedBark)".

The implementation lives in `svc_adobe.py` (Adobe User Management API). This
thin module exists only so the dispatcher's slug for that exact service title
(`svc_adobe_creative_cloud_redbark`) resolves to the real handler. Credentials
resolve via the ADOBE_* alias in deactivate.py's `_env_key_candidates`.
"""
from services.svc_adobe import deactivate  # noqa: F401
