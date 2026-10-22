"""Verified-email password recovery. Responses never reveal whether an account exists."""
from __future__ import annotations

import hashlib
import re
import secrets
import smtplib
from datetime import timedelta
from email.message import EmailMessage
from urllib.parse import urlparse

from sqlmodel import Session, select

from app.auth import _aware, hash_password, revoke_user_sessions
from app.config import settings
from app.persistence.models import PasswordReset, RecoveryAddress, User, db, utcnow

RESET_MINUTES = 15
VERIFY_MINUTES = 60
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
GENERIC = "If that account has a verified recovery email, a reset message is on its way."
_outbox: list[dict[str, str]] = []


def clear_outbox() -> None:
    _outbox.clear()


def outbox() -> list[dict[str, str]]:
    return list(_outbox)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _current(value) -> bool:
    """True while a stored expiry is still in the future. Naive database values are UTC."""
    return value is not None and _aware(value) > utcnow()


def _deliver(to: str, subject: str, text: str) -> None:
    mode = settings.recovery_delivery
    if mode == "memory":
        _outbox.append({"to": to, "subject": subject, "text": text})
        return
    if mode != "smtp" or not settings.smtp_url:
        return
    parsed = urlparse(settings.smtp_url)
    message = EmailMessage()
    message["To"] = to
    message["From"] = parsed.username or "noreply@localhost"
    message["Subject"] = subject
    message.set_content(text)
    host = parsed.hostname or "localhost"
    port = parsed.port or 587
    with smtplib.SMTP(host, port, timeout=10) as smtp:
        if parsed.username and parsed.password:
            smtp.starttls()
            smtp.login(parsed.username, parsed.password)
        smtp.send_message(message)


def start_verification(user: User, email: str) -> None:
    address = email.strip().lower()
    if not EMAIL.fullmatch(address):
        raise ValueError("Enter a valid email address")
    token = secrets.token_urlsafe(32)
    with db() as session:
        row = session.get(RecoveryAddress, user.id) or RecoveryAddress(user_id=user.id, email=address)
        row.email = address
        row.verified_at = None
        row.verify_hash = _hash(token)
        row.verify_expires_at = utcnow() + timedelta(minutes=VERIFY_MINUTES)
        session.add(row)
        session.commit()
    _deliver(address, "Verify your BAND_BOTS recovery email", f"Verification code: {token}")


def confirm_verification(token: str) -> bool:
    digest = _hash(token)
    with db() as session:
        row = session.exec(select(RecoveryAddress).where(RecoveryAddress.verify_hash == digest)).first()
        if row is None or not _current(row.verify_expires_at):
            return False
        row.verified_at = utcnow()
        row.verify_hash = None
        row.verify_expires_at = None
        session.add(row)
        session.commit()
    return True


def request_reset(username: str) -> None:
    """Always return the same outcome to the caller. Mail is sent only for a verified address."""
    with db() as session:
        user = session.exec(select(User).where(User.username == username.strip().lower())).first()
        address = session.get(RecoveryAddress, user.id) if user else None
        if user is None or address is None or address.verified_at is None:
            return
        token = secrets.token_urlsafe(32)
        session.add(PasswordReset(id=secrets.token_urlsafe(12), user_id=user.id, token_hash=_hash(token),
                                   expires_at=utcnow() + timedelta(minutes=RESET_MINUTES)))
        session.commit()
        email = address.email
    _deliver(email, "Reset your BAND_BOTS password", f"Reset code: {token}")


def reset_password(token: str, password: str) -> bool:
    if len(password) < 6:
        return False
    digest = _hash(token)
    with db() as session:
        row = session.exec(select(PasswordReset).where(PasswordReset.token_hash == digest)).first()
        if row is None or row.used_at is not None or not _current(row.expires_at):
            return False
        user = session.get(User, row.user_id)
        if user is None:
            return False
        user.password_hash = hash_password(password)
        row.used_at = utcnow()
        session.add(user)
        session.add(row)
        session.commit()
        user_id = user.id
    revoke_user_sessions(user_id)
    return True


def verified_email(session: Session, user_id: int) -> str | None:
    row = session.get(RecoveryAddress, user_id)
    if row is None or row.verified_at is None:
        return None
    return row.email
