"""Email + password auth with signed session cookies."""

from __future__ import annotations

import re
import sqlite3

import bcrypt
from fastapi import Cookie, HTTPException, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import db
from config import COOKIE_SECURE, SECRET_KEY, SESSION_COOKIE, SESSION_MAX_AGE

_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="jcs-session")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LEN = 8
# bcrypt silently truncates beyond 72 bytes; reject rather than mislead.
MAX_PASSWORD_BYTES = 72


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_credentials(email: str, password: str) -> None:
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="That doesn't look like a valid email address.")
    if len(password) < MIN_PASSWORD_LEN:
        raise HTTPException(
            status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LEN} characters."
        )
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise HTTPException(status_code=400, detail="Password is too long (max 72 bytes).")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def issue_session(response: Response, user_id: int) -> None:
    token = _serializer.dumps({"uid": user_id})
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,  # True automatically when PUBLIC_BASE_URL is https
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def _user_from_token(token: str | None) -> sqlite3.Row | None:
    if not token:
        return None
    try:
        payload = _serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    user_id = payload.get("uid")
    return db.get_user(user_id) if isinstance(user_id, int) else None


async def optional_user(jcs_session: str | None = Cookie(default=None)) -> sqlite3.Row | None:
    """FastAPI dependency: the signed-in user, or None."""
    return _user_from_token(jcs_session)


async def current_user(jcs_session: str | None = Cookie(default=None)) -> sqlite3.Row:
    """FastAPI dependency: the signed-in user, or 401."""
    user = _user_from_token(jcs_session)
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    return user
