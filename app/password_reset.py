"""Password reset by email (issue #151, ADR 0008).

A user who forgot their password asks for a link by email. The link holds a
random token; the database keeps only its SHA-256 hash. The link works once,
for RESET_SECONDS. Setting a new password replaces the user's session key, so
every older session ends, and deletes every link the user has.

The request route answers at once and runs send_reset_link afterwards, so a
known and an unknown email get the same answer in the same time. In mock
email mode the link goes to the server log, never to the page.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import timedelta

from sqlalchemy import delete
from sqlmodel import Session, select

from app.auth import AuthError, hash_password, normalize_email, validate_password
from app.config import Settings
from app.mailer import Mailer, MailError
from app.models import PasswordReset, User, as_utc, new_session_key, utcnow

log = logging.getLogger("duebell.password_reset")

RESET_SECONDS = 30 * 60
# A token from new_token is 43 characters. Anything far longer is junk.
MAX_TOKEN_CHARS = 128
BAD_LINK = "This link has expired or has already been used. Ask for a new one."


class BadResetLink(AuthError):
    """A reset link that is unknown, used or expired."""


def hash_token(token: str) -> str:
    """The token is 32 random bytes, so a plain SHA-256 is enough to hide it."""
    return hashlib.sha256(token.encode()).hexdigest()


def start_reset(session: Session, user: User) -> str:
    """Store a new link for `user` and return its token. Only the hash is kept."""
    token = secrets.token_urlsafe(32)
    session.add(
        PasswordReset(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=utcnow() + timedelta(seconds=RESET_SECONDS),
        )
    )
    session.commit()
    return token


def find_reset(session: Session, token: str) -> PasswordReset | None:
    """The unexpired link `token` belongs to, or None."""
    if not token or len(token) > MAX_TOKEN_CHARS:
        return None
    reset = session.exec(select(PasswordReset).where(PasswordReset.token_hash == hash_token(token))).first()
    if reset is None or as_utc(reset.expires_at) <= utcnow():
        return None
    return reset


def reset_password(session: Session, token: str, password: str) -> User:
    """Set a new password from a link, or raise AuthError.

    A bad link is refused before any password work. The password is checked
    and hashed before the link is used, so a short password leaves the link
    working. Deleting the link claims it: of two
    posts at once, only one deletes it and goes on. The same transaction sets
    the password, gives the user a new session key and deletes the user's
    other links.
    """
    reset = find_reset(session, token)
    if reset is None:
        raise BadResetLink(BAD_LINK)
    validate_password(password)
    password_hash = hash_password(password)
    claimed = session.exec(delete(PasswordReset).where(PasswordReset.id == reset.id))
    user = session.get(User, reset.user_id)
    if claimed.rowcount != 1 or user is None:
        session.rollback()
        raise BadResetLink(BAD_LINK)
    user.password_hash = password_hash
    user.session_key = new_session_key()
    session.add(user)
    session.exec(delete(PasswordReset).where(PasswordReset.user_id == user.id))
    session.commit()
    session.refresh(user)
    return user


def reset_link_base(settings: Settings, request_base_url: str) -> str:
    """Where reset links point, or "" if no safe address is known.

    PUBLIC_URL wins. Without it, mock mode uses the address the request came
    to, since its link only reaches the server log. SMTP mode never does: the
    Host header is the sender's to choose, and a link to their host would
    hand them the token.
    """
    if settings.public_url:
        return settings.public_url.rstrip("/")
    if settings.email_mode == "smtp":
        return ""
    return request_base_url.rstrip("/")


def send_reset_link(engine, mailer: Mailer, settings: Settings, email: str, base_url: str) -> None:
    """Email a reset link if `email` belongs to a user. Runs after the answer is sent.

    Nothing here reaches the person who asked: an unknown email, a missing
    PUBLIC_URL and a failed send all end in the log.
    """
    email = normalize_email(email)
    with Session(engine) as session:
        user = session.exec(select(User).where(User.email == email)).first()
        if user is None:
            return
        user_id, to = user.id, user.email
        if not base_url:
            log.warning("Password reset for user %s not sent: set PUBLIC_URL to send reset links by email.", user_id)
            return
        token = start_reset(session, user)
    link = f"{base_url}/reset-password?token={token}"
    minutes = RESET_SECONDS // 60
    body = (
        "Someone asked to reset the password for your Duebell account.\n\n"
        f"To choose a new password, open this link within {minutes} minutes. It works once:\n\n"
        f"{link}\n\n"
        "If you did not ask, ignore this email. Your password stays the same."
    )
    if settings.email_mode != "smtp":
        # Mock mode sends no email, so the operator reads the link here.
        log.warning("Mock email: password reset link for %s: %s", to, link)
    try:
        mailer.send(to, "Reset your Duebell password", body)
    except MailError as exc:
        log.warning("Password reset email for user %s failed: %s", user_id, exc)
