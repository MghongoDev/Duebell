"""Sign-in, sessions and password hashing (issue #24, ADR 0008).

Standard library only. Passwords use scrypt with a per-user salt. The session
is an HMAC-signed cookie holding the user id and an expiry; the server keeps
no session table. The cookie is HttpOnly and SameSite=Lax, which also stops
other sites from posting forms with it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from threading import Lock

from sqlmodel import Session, select

from app.models import User

COOKIE_NAME = "duebell_session"
SESSION_SECONDS = 12 * 60 * 60
MIN_PASSWORD_CHARS = 10
SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}
MAX_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60


class AuthError(ValueError):
    """A sign-in or account problem with a message safe to show."""


def hash_password(password: str) -> str:
    """scrypt$n$r$p$salt$digest, so the cost can rise later without breaking old hashes."""
    salt = secrets.token_bytes(16)
    n, r, p = SCRYPT["n"], SCRYPT["r"], SCRYPT["p"]
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=SCRYPT["dklen"])
    encoded = base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()
    return f"scrypt${n}${r}${p}${encoded}"


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        expected = base64.b64decode(digest_b64)
        digest = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
            maxmem=64 * 1024 * 1024,
        )
    except (ValueError, TypeError):
        return False
    return scheme == "scrypt" and hmac.compare_digest(digest, expected)


def create_user(session: Session, email: str, password: str) -> User:
    email = email.strip().lower()
    if "@" not in email or len(email) > 254:
        raise AuthError("Enter a valid email address.")
    if len(password) < MIN_PASSWORD_CHARS:
        raise AuthError(f"Use a password of at least {MIN_PASSWORD_CHARS} characters.")
    if session.exec(select(User).where(User.email == email)).first():
        raise AuthError("A user with that email already exists.")
    user = User(email=email, password_hash=hash_password(password))
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


# A dummy hash so an unknown email costs the same scrypt work as a wrong password.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


class Lockout:
    """Count recent failures per email, in memory, for one process."""

    def __init__(self) -> None:
        self._failures: dict[str, list[float]] = {}
        self._lock = Lock()

    def _recent(self, email: str, now: float) -> list[float]:
        return [at for at in self._failures.get(email, []) if now - at < LOCKOUT_SECONDS]

    def locked(self, email: str) -> bool:
        with self._lock:
            return len(self._recent(email, time.time())) >= MAX_FAILURES

    def fail(self, email: str) -> None:
        with self._lock:
            now = time.time()
            self._failures[email] = self._recent(email, now) + [now]

    def clear(self, email: str) -> None:
        with self._lock:
            self._failures.pop(email, None)


def authenticate(session: Session, email: str, password: str, lockout: Lockout) -> User:
    email = email.strip().lower()
    if lockout.locked(email):
        raise AuthError("Too many failed attempts. Wait 15 minutes and try again.")
    user = session.exec(select(User).where(User.email == email)).first()
    valid = check_password(password, user.password_hash if user else _DUMMY_HASH)
    if not user or not valid:
        lockout.fail(email)
        raise AuthError("That email and password do not match.")
    lockout.clear(email)
    return user


def _sign(secret: str, payload: bytes) -> str:
    return base64.urlsafe_b64encode(hmac.new(secret.encode(), payload, hashlib.sha256).digest()).decode().rstrip("=")


def make_session_cookie(secret: str, user_id: int, now: float | None = None) -> str:
    body = json.dumps({"uid": user_id, "exp": int((now or time.time()) + SESSION_SECONDS)}).encode()
    encoded = base64.urlsafe_b64encode(body).decode().rstrip("=")
    return f"{encoded}.{_sign(secret, body)}"


def read_session_cookie(secret: str, value: str | None, now: float | None = None) -> int | None:
    if not value or "." not in value:
        return None
    encoded, signature = value.rsplit(".", 1)
    try:
        body = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(signature, _sign(secret, body)):
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("exp", 0) < (now or time.time()):
        return None
    uid = data.get("uid")
    return uid if isinstance(uid, int) else None
