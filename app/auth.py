"""
Auth helpers.

Single-user admin auth:
  - Password is bcrypt-hashed, stored in ADMIN_PASSWORD_HASH env var.
  - On successful login, we set a signed session cookie (itsdangerous).
  - Cookie expires after 24 hours.

Tenant fix-links:
  - Signed, time-limited tokens bound to a submission id.
  - Used in /resubmit/{id}?t=TOKEN so even if a tenant forwards the link,
    it expires.

Two separate secrets (ADMIN_SESSION_SECRET and ADMIN_TOKEN_SECRET) so
compromise of one doesn't let an attacker forge the other.
"""

from __future__ import annotations

import os
import secrets
from typing import Any

import bcrypt
from fastapi import Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# These MUST be set in production. Default to random-per-boot so the app
# doesn't silently run with a known secret — it just invalidates sessions
# on every restart, which will be obvious quickly.
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "")
ADMIN_SESSION_SECRET = os.environ.get("ADMIN_SESSION_SECRET") or secrets.token_hex(32)
ADMIN_TOKEN_SECRET = os.environ.get("ADMIN_TOKEN_SECRET") or secrets.token_hex(32)

SESSION_COOKIE = "lease_admin"
SESSION_MAX_AGE = 60 * 60 * 24  # 24 hours
TOKEN_MAX_AGE = 60 * 60 * 24 * 7  # 7 days

_session_serializer = URLSafeTimedSerializer(ADMIN_SESSION_SECRET, salt="session")
_token_serializer = URLSafeTimedSerializer(ADMIN_TOKEN_SECRET, salt="fix-link")


# ---------------------------------------------------------------------------
# Password
# ---------------------------------------------------------------------------


def verify_password(plaintext: str) -> bool:
    """Constant-time check against the configured bcrypt hash."""
    if not ADMIN_PASSWORD_HASH:
        return False
    try:
        return bcrypt.checkpw(plaintext.encode("utf-8"),
                              ADMIN_PASSWORD_HASH.encode("utf-8"))
    except ValueError:
        # Malformed hash — don't blow up, just refuse.
        return False


def hash_password(plaintext: str) -> str:
    """For use from a one-off CLI when generating the admin hash."""
    return bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


def issue_session_cookie() -> str:
    """Return a signed session cookie value for a freshly-authenticated admin."""
    return _session_serializer.dumps({"admin": True})


def is_authenticated(request: Request) -> bool:
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return False
    try:
        data = _session_serializer.loads(cookie, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    return bool(data.get("admin"))


# ---------------------------------------------------------------------------
# Fix-link tokens
# ---------------------------------------------------------------------------


def make_fix_token(submission_id: str) -> str:
    return _token_serializer.dumps({"sid": submission_id})


def verify_fix_token(token: str, submission_id: str) -> bool:
    try:
        data = _token_serializer.loads(token, max_age=TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    return data.get("sid") == submission_id
