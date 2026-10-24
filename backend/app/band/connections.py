"""Per-user Agent API credentials for the Band platform connection."""
import asyncio
from contextlib import asynccontextmanager

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select
from band.client.rest import AsyncRestClient, aclose_rest_client

from app.auth import encrypt_secret, decrypt_secret
from app.band.listener import BandIdentity, fetch_identity
from app.config import settings
from app.persistence.models import UserBandConnection, db, utcnow


async def validate_key(key: str) -> BandIdentity:
    key = key.strip()
    if not key.startswith("band_a_"):
        raise HTTPException(422, "Use a Band Agent API key beginning with band_a_. Human API keys (band_u_) and model-provider keys cannot be used here.")
    try:
        async with asyncio.timeout(25):
            return await fetch_identity(key)
    except Exception:
        raise HTTPException(422, "Band could not verify this key. Check that you copied the complete Agent API key and try again. If it is correct, Band may be temporarily unavailable.") from None


def save_connection(session: Session, user_id: int, key: str, identity: BandIdentity) -> UserBandConnection:
    row = session.get(UserBandConnection, user_id)
    if row and row.band_agent_id != identity.id:
        raise HTTPException(409, "This account already has a different Band connection. Use a key for the same Band identity.")
    duplicate = session.exec(select(UserBandConnection).where(UserBandConnection.band_agent_id == identity.id,
                                                              UserBandConnection.user_id != user_id)).first()
    if duplicate:
        raise HTTPException(409, "This Band connection is already linked to another app account. Use your own Band Agent API key.")
    if row is None:
        row = UserBandConnection(user_id=user_id, band_agent_id=identity.id, secret_enc="")
    row.secret_enc = encrypt_secret(key.strip())
    row.name, row.handle, row.verified_at = identity.name, identity.handle, utcnow()
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "This Band connection is already linked to another app account. Use your own Band Agent API key.") from None
    return row


def connection_status(row: UserBandConnection | None) -> dict:
    return {"connected": bool(row and row.secret_enc), "name": row.name if row else "",
            "handle": row.handle if row else "", "api": "agent", "verified_at": row.verified_at.isoformat() if row else None}


@asynccontextmanager
async def platform_client(user_id: int, *, identity: str | None = None):
    """Band operations use the requesting user's key, never another user's or the workspace key."""
    with db() as session:
        row = session.get(UserBandConnection, user_id)
    if not row:
        raise HTTPException(409, "Add your Band platform connection before using Band services.")
    if identity and row.band_agent_id != identity:
        raise HTTPException(409, "The original Band connection for this work must be reconnected.")
    try:
        key = decrypt_secret(row.secret_enc)
    except Exception:
        raise HTTPException(409, "Your Band key needs to be updated in People.") from None
    client = AsyncRestClient(api_key=key, base_url=settings.band_rest_url)
    try:
        yield client
    finally:
        await aclose_rest_client(client)
