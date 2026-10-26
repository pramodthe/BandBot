from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import timedelta

import bcrypt
from cryptography.fernet import Fernet
from fastapi import Depends, Header, HTTPException
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlmodel import Session, select

from app.config import settings
from app.persistence.models import AuthSession, User, as_utc, db, get_session, utcnow

TOKEN_MAX_AGE = 60 * 60 * 24 * 30
_PASSWORD_HASH_PREFIX = "bcrypt-sha256$"
_UNSAFE_SECRETS = {"", "dev-secret-change-me", "changeme", "secret", "password"}

_serializer = URLSafeTimedSerializer(settings.app_secret, salt="band-slack-session")
_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(settings.app_secret.encode()).digest()))


def require_production_secret() -> None:
    """Stop a production process that is still using a known or missing application secret."""
    if settings.band_env != "production":
        return
    if settings.app_secret in _UNSAFE_SECRETS or len(settings.app_secret) < 32:
        raise SystemExit("Refusing to start: APP_SECRET is missing or unsafe for production.")
    if settings.recovery_delivery == "memory":
        raise SystemExit("Refusing to start: recovery email cannot use memory delivery in production.")


def hash_password(password: str) -> str:
    # bcrypt accepts at most 72 bytes. Hash the complete UTF-8 password first,
    # then encode the digest as recommended by bcrypt's maximum-length guidance.
    digest = base64.b64encode(hashlib.sha256(password.encode()).digest())
    return _PASSWORD_HASH_PREFIX + bcrypt.hashpw(digest, bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    encoded = password.encode()
    if password_hash.startswith(_PASSWORD_HASH_PREFIX):
        encoded = base64.b64encode(hashlib.sha256(encoded).digest())
        password_hash = password_hash.removeprefix(_PASSWORD_HASH_PREFIX)
    elif len(encoded) > 72:
        # Existing hashes use raw bcrypt; never truncate a submitted password.
        return False
    try:
        return bcrypt.checkpw(encoded, password_hash.encode())
    except ValueError:
        return False


def issue_token(user_id: int) -> str:
    """Create a server-side session and return the browser token that names it."""
    session_id = secrets.token_urlsafe(18)
    with db() as session:
        session.add(AuthSession(id=session_id, user_id=user_id, expires_at=utcnow() + timedelta(seconds=TOKEN_MAX_AGE)))
        session.commit()
    return _serializer.dumps({"uid": user_id, "sid": session_id})


def token_payload(token: str) -> dict | None:
    try:
        data = _serializer.loads(token, max_age=TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    return data if isinstance(data, dict) else None


def user_id_from_token(token: str) -> int | None:
    """Signature check used to keep workspace credentials out of proxied requests."""
    data = token_payload(token)
    uid = data.get("uid") if data else None
    return uid if isinstance(uid, int) else None


def authenticate(token: str, session: Session) -> tuple[User, str] | None:
    data = token_payload(token)
    if not data:
        return None
    uid, sid = data.get("uid"), data.get("sid")
    if not isinstance(uid, int) or not isinstance(sid, str):
        return None
    user = session.get(User, uid)
    row = session.get(AuthSession, sid)
    if user is None or row is None or row.user_id != user.id or row.revoked_at is not None:
        return None
    if as_utc(row.expires_at) <= utcnow():
        return None
    return user, sid


def revoke_session(session_id: str) -> None:
    with db() as session:
        row = session.get(AuthSession, session_id)
        if row and row.revoked_at is None:
            row.revoked_at = utcnow()
            session.add(row)
            session.commit()


def revoke_user_sessions(user_id: int) -> None:
    with db() as session:
        rows = session.exec(select(AuthSession).where(AuthSession.user_id == user_id, AuthSession.revoked_at == None)).all()  # noqa: E711
        now = utcnow()
        for row in rows:
            row.revoked_at = now
            session.add(row)
        session.commit()


def encrypt_secret(value: str) -> str:
    return _fernet.encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    return _fernet.decrypt(value.encode()).decode()


def _principal(authorization: str, session: Session) -> tuple[User, str]:
    token = authorization.removeprefix("Bearer ").strip()
    principal = authenticate(token, session) if token else None
    if principal is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    return principal


def current_user(
    authorization: str = Header(default=""),
    session: Session = Depends(get_session),
) -> User:
    return _principal(authorization, session)[0]


def current_session_id(
    authorization: str = Header(default=""),
    session: Session = Depends(get_session),
) -> str:
    return _principal(authorization, session)[1]
