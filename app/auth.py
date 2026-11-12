"""Sign-in, sessions and password hashing (issue #24, ADR 0008).

Standard library only. Passwords use scrypt with a per-user salt. The session
is an HMAC-signed cookie holding the user id, the user's session key and an
expiry; the server keeps no session table. A cookie counts only while its key
matches the user's, so it never signs in a later user who reuses the id
(issue #142). The cookie is HttpOnly and SameSite=Lax, which also stops
other sites from posting forms with it. Sign-in itself needs no cookie, so the
route also refuses a post whose Origin names another site (issue #45).
Sign-up, when on, applies create_user's limits and answers a taken email
like a new one (issue #150).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import deque
from threading import Lock

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models import User, new_session_key

COOKIE_NAME = "duebell_session"
SESSION_SECONDS = 12 * 60 * 60
MIN_PASSWORD_CHARS = 10
# Sign-in refuses longer input before the lockout or scrypt sees it (issues #82, #122).
MAX_EMAIL_CHARS = 254
MAX_PASSWORD_CHARS = 1024
SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}
MAX_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60
MAX_LOCKOUT_ENTRIES = 10_000
SWEEP_SECONDS = 60
# Sign-up attempts the whole process accepts per window (issue #150).
SIGNUP_MAX_ATTEMPTS = 20
SIGNUP_WINDOW_SECONDS = 15 * 60


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


def valid_email(email: str) -> bool:
    """True if `email`, already stripped and lowercased, is one create_user accepts."""
    return "@" in email and len(email) <= MAX_EMAIL_CHARS


def validate_password(password: str) -> None:
    """Raise AuthError if create_user would refuse `password`, so a caller can check it first (issue #102)."""
    if len(password) < MIN_PASSWORD_CHARS:
        raise AuthError(f"Use a password of at least {MIN_PASSWORD_CHARS} characters.")
    if len(password) > MAX_PASSWORD_CHARS:
        raise AuthError(f"Use a password of at most {MAX_PASSWORD_CHARS} characters.")


def normalize_email(email: str) -> str:
    """The form every user's email is stored and looked up in (issue #123)."""
    return email.strip().lower()


def create_user(session: Session, email: str, password: str) -> User:
    email = normalize_email(email)
    if not valid_email(email):
        raise AuthError("Enter a valid email address.")
    validate_password(password)
    if session.exec(select(User).where(User.email == email)).first():
        raise AuthError("A user with that email already exists.")
    user = User(email=email, password_hash=hash_password(password), session_key=new_session_key())
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


# A dummy hash so an unknown email costs the same scrypt work as a wrong password.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


class TooManySignups(AuthError):
    """The process-wide sign-up limit is reached (issue #150)."""


class SignupLimit:
    """Count sign-up attempts for the whole process, in memory (issue #150).

    Each sign-up names a new email, so a count per email, like the sign-in
    lockout, would never fill. This one counts every attempt together: at most
    `max_attempts` within SIGNUP_WINDOW_SECONDS. That caps how fast anyone can
    add accounts or test emails, at the cost that many attempts in a short
    time also turn honest visitors away until the window passes.
    """

    def __init__(self, max_attempts: int = SIGNUP_MAX_ATTEMPTS) -> None:
        self._times: deque[float] = deque()
        self._lock = Lock()
        self._max_attempts = max_attempts

    def attempt(self) -> bool:
        """Count one attempt. False, and nothing counted, if the limit is reached."""
        with self._lock:
            now = time.time()
            while self._times and now - self._times[0] >= SIGNUP_WINDOW_SECONDS:
                self._times.popleft()
            if len(self._times) >= self._max_attempts:
                return False
            self._times.append(now)
            return True


def sign_up(session: Session, email: str, password: str, limit: SignupLimit) -> User | None:
    """Create an account from the sign-up page, or return None if the email is taken (issue #150).

    It refuses the same emails and passwords as create_user, with the same
    messages, before the limit counts the attempt. A taken email changes
    nothing and costs the same password hash as a new account, so the caller
    can answer both alike and not reveal that the email is registered.
    """
    email = normalize_email(email)
    if not valid_email(email):
        raise AuthError("Enter a valid email address.")
    validate_password(password)
    if not limit.attempt():
        raise TooManySignups("Too many sign-ups just now. Wait 15 minutes and try again.")
    if session.exec(select(User).where(User.email == email)).first():
        hash_password(password)
        return None
    try:
        return create_user(session, email, password)
    except (AuthError, IntegrityError):
        # Another request took the email since the check above.
        session.rollback()
        return None


class Lockout:
    """Count recent attempts per email, in memory, for one process.

    An attempt is counted before its password is checked, so attempts that
    arrive together cannot all pass the check (issue #45). A success clears
    the count. Entries older than LOCKOUT_SECONDS are swept out, and the table
    holds at most `max_entries` emails. A locked email is never evicted to make
    room; when every entry is locked, a new email is refused until the sweep
    frees one (issue #122).
    """

    def __init__(self, max_entries: int = MAX_LOCKOUT_ENTRIES) -> None:
        self._attempts: dict[str, list[float]] = {}
        self._lock = Lock()
        self._max_entries = max_entries
        self._last_sweep = 0.0

    def __len__(self) -> int:
        with self._lock:
            return len(self._attempts)

    def _recent(self, email: str, now: float) -> list[float]:
        return [at for at in self._attempts.get(email, []) if now - at < LOCKOUT_SECONDS]

    def _sweep(self, now: float) -> None:
        """Drop expired entries, then make room for one more email by evicting unlocked ones.

        A locked email is never evicted, so a flood of new emails cannot free
        one (issue #122). If every entry is locked, the table stays full.
        """
        self._last_sweep = now
        for email in list(self._attempts):
            recent = self._recent(email, now)
            if recent:
                self._attempts[email] = recent
            else:
                del self._attempts[email]
        if len(self._attempts) < self._max_entries:
            return
        unlocked = [email for email, times in self._attempts.items() if len(times) < MAX_FAILURES]
        unlocked.sort(key=lambda email: self._attempts[email][-1])
        for email in unlocked[: len(self._attempts) - self._max_entries + 1]:
            del self._attempts[email]

    def attempt(self, email: str) -> bool:
        """Count one attempt for `email`. False, and nothing counted, if it is locked.

        Also False, and nothing stored, for a new email while the table is full
        of locked ones (issue #122).
        """
        with self._lock:
            now = time.time()
            if now - self._last_sweep >= SWEEP_SECONDS or (
                email not in self._attempts and len(self._attempts) >= self._max_entries
            ):
                self._sweep(now)
            if email not in self._attempts and len(self._attempts) >= self._max_entries:
                return False
            recent = self._recent(email, now)
            if len(recent) >= MAX_FAILURES:
                self._attempts[email] = recent
                return False
            self._attempts[email] = recent + [now]
            return True

    def clear(self, email: str) -> None:
        with self._lock:
            self._attempts.pop(email, None)


def authenticate(session: Session, email: str, password: str, lockout: Lockout) -> User:
    """Sign a user in, or raise AuthError.

    An email no user can have, or a password too long for any user, is
    refused before the lockout counts it or scrypt hashes it (issues #82,
    #122), so junk cannot fill the table or cheaply lock an email. Both get
    the same message as a wrong password.
    """
    mismatch = AuthError("That email and password do not match.")
    email = normalize_email(email)
    if not valid_email(email) or len(password) > MAX_PASSWORD_CHARS:
        raise mismatch
    if not lockout.attempt(email):
        raise AuthError("Too many failed attempts. Wait 15 minutes and try again.")
    user = session.exec(select(User).where(User.email == email)).first()
    valid = check_password(password, user.password_hash if user else _DUMMY_HASH)
    if not user or not valid:
        raise mismatch
    lockout.clear(email)
    return user


def _sign(secret: str, payload: bytes) -> str:
    return base64.urlsafe_b64encode(hmac.new(secret.encode(), payload, hashlib.sha256).digest()).decode().rstrip("=")


def make_session_cookie(secret: str, user_id: int, session_key: str, now: float | None = None) -> str:
    expires = int((now or time.time()) + SESSION_SECONDS)
    body = json.dumps({"uid": user_id, "key": session_key, "exp": expires}).encode()
    encoded = base64.urlsafe_b64encode(body).decode().rstrip("=")
    return f"{encoded}.{_sign(secret, body)}"


def read_session_cookie(secret: str, value: str | None, now: float | None = None) -> tuple[int, str] | None:
    """The (user id, session key) a valid, unexpired cookie holds, else None.

    A cookie with no key, from before issue #142, is refused.
    """
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
    uid, key = data.get("uid"), data.get("key")
    if not isinstance(uid, int) or not isinstance(key, str) or not key:
        return None
    return uid, key


def session_key_matches(user: User, key: str) -> bool:
    """True if `key` is the user's current session key (issue #142)."""
    if not user.session_key:
        return False
    return hmac.compare_digest(key.encode(), user.session_key.encode())
