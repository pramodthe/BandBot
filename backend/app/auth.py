from __future__ import annotations

import base64
import hashlib

import bcrypt
from cryptography.fernet import Fernet
from fastapi import Depends, Header, HTTPException
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlmodel import Session

from app.config import settings
from app.persistence.models import User, get_session

TOKEN_MAX_AGE = 60 * 60 * 24 * 30
_PASSWORD_HASH_PREFIX = "bcrypt-sha256$"

_serializer = URLSafeTimedSerializer(settings.app_secret, salt="band-slack-session")
_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(settings.app_secret.encode()).digest()))


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
    return _serializer.dumps({"uid": user_id})


def user_id_from_token(token: str) -> int | None:
    try:
        data = _serializer.loads(token, max_age=TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    return data.get("uid")


def encrypt_secret(value: str) -> str:
    return _fernet.encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    return _fernet.decrypt(value.encode()).decode()


def current_user(
    authorization: str = Header(default=""),
    session: Session = Depends(get_session),
) -> User:
    token = authorization.removeprefix("Bearer ").strip()
    uid = user_id_from_token(token) if token else None
    user = session.get(User, uid) if uid else None
    if user is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    return user
